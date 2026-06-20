"""Background job processing for long-running operations."""

import asyncio
import json
from .logger import get_logger
from . import database as db
from . import media_rewriter
from .sync import prepare_segments_for_upload, push_segments_to_library, mark_sync_complete, get_sync_config

# db.get_connection is imported directly by recover_stale_jobs for a raw UPDATE.

logger = get_logger(__name__)

# Global task tracking
_running_tasks = {}


async def process_upload_job(job_id: int) -> None:
    """
    Process an upload job in the background.
    Updates job status and result in the database as it progresses.
    """
    try:
        await db.update_bg_job(job_id, status='running', progress=0)
        
        # Get sync config
        config = await get_sync_config()
        if not config:
            raise Exception("Sync not configured")
        
        instance_name = config.get("instance_name", "unknown")
        
        # Step 1: Gather segments (20% progress)
        logger.info(f"[Job {job_id}] Gathering segments for upload...")
        await db.update_bg_job(job_id, progress=20)
        
        upload_data = await prepare_segments_for_upload(instance_name)
        
        if not upload_data:
            logger.warning(f"[Job {job_id}] No segments to upload")
            result = {
                "status": "no_data",
                "files_processed": 0,
                "entries_updated": 0,
                "message": "No segments to upload",
            }
            await db.update_bg_job(
                job_id,
                status='completed',
                progress=100,
                result=json.dumps(result),
            )
            return
        
        # Step 2: Upload to GitHub (60% progress)
        logger.info(f"[Job {job_id}] Uploading {len(upload_data)} files to GitHub...")
        await db.update_bg_job(job_id, progress=60)
        
        entries_count = await push_segments_to_library(instance_name, upload_data)
        
        # Step 3: Mark complete (90% progress)
        await db.update_bg_job(job_id, progress=90)
        logger.info(f"[Job {job_id}] Marking sync complete...")
        
        await mark_sync_complete()
        
        # Success!
        result = {
            "status": "success",
            "files_processed": len(upload_data),
            "entries_updated": entries_count,
            "message": f"Uploaded {len(upload_data)} files to GitHub segment library",
        }
        
        logger.info(f"[Job {job_id}] Upload complete: {entries_count} entries")
        await db.update_bg_job(
            job_id,
            status='completed',
            progress=100,
            result=json.dumps(result),
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[Job {job_id}] Upload failed: {error_msg}")
        await db.update_bg_job(
            job_id,
            status='failed',
            progress=0,
            error=error_msg,
        )
    finally:
        # Clean up task reference
        _running_tasks.pop(job_id, None)


async def enqueue_upload_job() -> int:
    """
    Enqueue an upload job and return the job ID.
    The job will be processed in the background without blocking the request.
    """
    # Create job record
    job_id = await db.create_bg_job('upload')
    
    # Create async task (won't block the request)
    task = asyncio.create_task(process_upload_job(job_id))
    _running_tasks[job_id] = task
    
    logger.info(f"Enqueued upload job {job_id}")
    return job_id


async def process_rewrite_job(job_id: int, request: dict) -> None:
    """Process one or more media rewrite exports in the background."""
    try:
        await db.update_bg_job(job_id, status='running', progress=0)
        media_ids = [str(value).strip() for value in request.get("media_ids", []) if str(value).strip()]
        if not media_ids:
            raise Exception("No media ids were provided")

        explicit_leaf_keys = [str(value).strip() for value in request.get("selected_leaf_keys", []) if str(value).strip()]
        profile_user = str(request.get("profile_user") or "").strip()
        selected_leaf_keys = explicit_leaf_keys or (
            await media_rewriter.get_leaf_keys_for_user_profile(profile_user) if profile_user else []
        )
        if not selected_leaf_keys:
            raise Exception("No VidAngel leaf filters were selected")

        language_mode = str(request.get("language_mode") or media_rewriter.REWRITE_LANGUAGE_MODE_DEFAULT)
        per_media_language_mode = {
            str(key): str(value)
            for key, value in dict(request.get("per_media_language_mode") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        size_limit_percent = int(request.get("size_limit_percent") or media_rewriter.REWRITE_SIZE_LIMIT_PERCENT)

        results = []
        total = len(media_ids)
        for index, media_id in enumerate(media_ids):
            job_language_mode = per_media_language_mode.get(media_id, language_mode)
            plan = await media_rewriter.build_rewrite_plan(
                media_id,
                selected_leaf_keys=selected_leaf_keys,
                language_mode=job_language_mode,
            )
            result = await media_rewriter.execute_rewrite_plan(
                plan,
                size_limit_percent=size_limit_percent,
            )
            results.append(result)
            await db.update_bg_job(job_id, progress=int(((index + 1) / total) * 100))

        await db.update_bg_job(
            job_id,
            status='completed',
            progress=100,
            result=json.dumps(
                {
                    "status": "success",
                    "count": len(results),
                    "results": results,
                }
            ),
        )
    except Exception as exc:
        await db.update_bg_job(
            job_id,
            status='failed',
            progress=0,
            error=str(exc),
        )
    finally:
        _running_tasks.pop(job_id, None)


async def enqueue_rewrite_job(request: dict) -> int:
    """Enqueue a media rewrite export job and return its id."""
    job_id = await db.create_bg_job('rewrite')
    task = asyncio.create_task(process_rewrite_job(job_id, request))
    _running_tasks[job_id] = task
    logger.info(f"Enqueued rewrite job {job_id}")
    return job_id


async def recover_stale_jobs() -> None:
    """Mark any jobs still in 'running' state as failed on startup.

    A job stuck in 'running' means the process was killed mid-flight; it can
    never complete now, so we surface it as failed rather than leaving it orphaned.
    """
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE bg_jobs SET status='failed', error_message='Process restarted while job was running' "
            "WHERE status IN ('running', 'queued')"
        )
        await conn.commit()
        if cursor.rowcount:
            logger.warning(
                "Recovered %d stale background job(s) left in running/queued state",
                cursor.rowcount,
            )


async def get_job_status(job_id: int) -> dict | None:
    """Get the status of a background job."""
    job = await db.get_bg_job(job_id)
    if not job:
        return None
    
    # Parse result data if present
    result_data = None
    if job.get('result_data'):
        try:
            result_data = json.loads(job['result_data'])
        except:
            pass
    
    return {
        'id': job['id'],
        'job_type': job['job_type'],
        'status': job['status'],
        'progress': job['progress_percent'],
        'error': job.get('error_message'),
        'result': result_data,
        'created_at': job.get('created_at'),
        'completed_at': job.get('completed_at'),
    }
