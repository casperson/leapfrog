"""Match VidAngel export artifacts to Plex titles and write adjacent sidecars."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import database as db
from .logger import get_logger, setup_logging
from .paths import get_data_dir
from .segment_export import write_sidecar_file
from .vidangel_export import RawFilterEvent, build_sidecar_payload, get_vidangel_export_dir
import leapfrog.plex_client as plex_mod

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SidecarGenerationResult:
    """Outcome for one matched or skipped Plex job."""

    plex_guid: str
    file_path: str
    media_type: str
    title: str
    status: str
    detail: str
    matched_media_id: str | None = None
    matched_slug: str | None = None
    sidecar_path: str | None = None
    event_count: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "plex_guid": self.plex_guid,
            "file_path": self.file_path,
            "media_type": self.media_type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "matched_media_id": self.matched_media_id,
            "matched_slug": self.matched_slug,
            "sidecar_path": self.sidecar_path,
            "event_count": self.event_count,
        }


def _normalize_title(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return " ".join(cleaned.split())


def _parse_json_file(path: Path, list_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get(list_key)
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _load_movie_catalog(export_dir: Path) -> list[dict[str, Any]]:
    return _parse_json_file(export_dir / "movies_catalog.json", "titles")


def _load_tv_catalog(export_dir: Path) -> list[dict[str, Any]]:
    return _parse_json_file(export_dir / "tv_catalog.json", "titles")


def _load_raw_events(export_dir: Path) -> dict[tuple[str, str], list[RawFilterEvent]]:
    source = export_dir / "raw_filter_events.csv"
    if not source.exists():
        raise FileNotFoundError(source)
    rows = source.read_text(encoding="utf-8").splitlines()
    if not rows:
        return {}
    import csv

    grouped: dict[tuple[str, str], list[RawFilterEvent]] = {}
    for row in csv.DictReader(rows):
        media_type = str(row.get("media_type") or "")
        media_id = str(row.get("media_id") or "")
        event = RawFilterEvent(
            media_id=media_id,
            media_type=media_type,
            title=str(row.get("title") or ""),
            slug=str(row.get("slug") or ""),
            service_slug=str(row.get("service_slug") or ""),
            tag_set_id=int(row.get("tag_set_id") or 0),
            season_number=int(row["season_number"]) if str(row.get("season_number") or "").strip() else None,
            episode_number=int(row["episode_number"]) if str(row.get("episode_number") or "").strip() else None,
            path_keys=tuple(part for part in str(row.get("path_keys") or "").split("/") if part),
            path_titles=tuple(part for part in str(row.get("path_titles") or "").split(" > ") if part),
            display_title=str(row.get("display_title") or ""),
            description=str(row.get("description") or "").strip() or None,
            tag_type=str(row.get("tag_type") or ""),
            start_ms=int(row.get("start_ms") or 0),
            end_ms=int(row.get("end_ms") or 0),
            mapped_category=str(row.get("mapped_category") or "").strip() or None,
            leaf_key=str(row.get("leaf_key") or ""),
        )
        grouped.setdefault((media_type, media_id), []).append(event)
    return grouped


def _build_movie_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int | None], list[dict[str, Any]]]:
    indexed: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_normalize_title(str(row.get("title") or "")), int(row["extra_metadata"]["year"]) if isinstance(row.get("extra_metadata"), dict) and row["extra_metadata"].get("year") is not None else None)
        indexed.setdefault(key, []).append(row)
    return indexed


def _build_episode_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    indexed: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        extra = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        show_title = _normalize_title(str(extra.get("show_title") or row.get("show_title") or ""))
        season_number = row.get("season_number")
        episode_number = row.get("episode_number")
        if not show_title or season_number in (None, "") or episode_number in (None, ""):
            continue
        key = (show_title, int(season_number), int(episode_number))
        indexed.setdefault(key, []).append(row)
    return indexed


def match_movie_catalog_row(job: dict[str, Any], movie_index: dict[tuple[str, int | None], list[dict[str, Any]]]) -> tuple[dict[str, Any] | None, str]:
    title_key = _normalize_title(str(job.get("title") or ""))
    year = int(job["year"]) if job.get("year") is not None else None
    exact = movie_index.get((title_key, year), []) if title_key else []
    if len(exact) == 1:
        return exact[0], "exact title/year match"
    if len(exact) > 1:
        return None, "ambiguous movie title/year match"
    title_only_matches = [
        row
        for (candidate_title, _candidate_year), values in movie_index.items()
        if candidate_title == title_key
        for row in values
    ]
    if len(title_only_matches) == 1:
        return title_only_matches[0], "title-only movie match"
    if len(title_only_matches) > 1:
        return None, "ambiguous movie title match"
    return None, "no movie match"


def match_episode_catalog_row(
    *,
    show_title: str,
    season_number: int | None,
    episode_number: int | None,
    episode_index: dict[tuple[str, int, int], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    if not show_title or season_number is None or episode_number is None:
        return None, "missing episode match metadata"
    key = (_normalize_title(show_title), season_number, episode_number)
    matches = episode_index.get(key, [])
    if len(matches) == 1:
        return matches[0], "exact show/season/episode match"
    if len(matches) > 1:
        return None, "ambiguous episode match"
    return None, "no episode match"


async def generate_vidangel_sidecars(
    *,
    library_id: str | None = None,
    export_dir: Path | None = None,
    only_missing: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Match VidAngel export artifacts to Plex titles and write adjacent sidecars."""
    source_dir = export_dir or get_vidangel_export_dir()
    movie_index = _build_movie_index(_load_movie_catalog(source_dir))
    episode_index = _build_episode_index(_load_tv_catalog(source_dir))
    event_index = _load_raw_events(source_dir)

    jobs = await db.get_scan_jobs_by_library(library_id) if library_id else await db.get_scan_jobs()
    if not jobs:
        return {"library_id": library_id, "results": [], "summary": {"total": 0, "written": 0, "matched": 0}}

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    results: list[SidecarGenerationResult] = []
    for job in jobs:
        file_path = str(job.get("file_path") or "")
        media_type = str(job.get("media_type") or "movie")
        title = str(job.get("title") or "")
        if not file_path:
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path="",
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="missing file path",
                )
            )
            continue
        sidecar_path = str(Path(file_path).with_suffix(".leapfrog.json"))
        if only_missing and Path(sidecar_path).exists():
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="sidecar already exists",
                    sidecar_path=sidecar_path,
                )
            )
            continue

        match_row: dict[str, Any] | None = None
        match_detail = ""
        if media_type == "episode":
            show_title = ""
            season_number = None
            episode_number = None
            if client and job.get("rating_key"):
                show_title, season_number, episode_number = await client.get_episode_match_info(str(job["rating_key"]))
            match_row, match_detail = match_episode_catalog_row(
                show_title=show_title,
                season_number=season_number,
                episode_number=episode_number,
                episode_index=episode_index,
            )
        else:
            match_row, match_detail = match_movie_catalog_row(job, movie_index)

        if not match_row:
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="unmatched",
                    detail=match_detail,
                )
            )
            continue

        matched_media_id = str(match_row.get("media_id") or "")
        event_key = (str(match_row.get("media_type") or media_type), matched_media_id)
        events = event_index.get(event_key, [])
        payload = build_sidecar_payload(
            media_id=matched_media_id,
            title=str(match_row.get("title") or title),
            slug=str(match_row.get("slug") or ""),
            events=events,
        )
        if not dry_run:
            await write_sidecar_file(sidecar_path, payload)
        results.append(
            SidecarGenerationResult(
                plex_guid=str(job.get("plex_guid") or ""),
                file_path=file_path,
                media_type=media_type,
                title=title,
                status="written" if not dry_run else "matched",
                detail=match_detail,
                matched_media_id=matched_media_id,
                matched_slug=str(match_row.get("slug") or ""),
                sidecar_path=sidecar_path,
                event_count=len(events),
            )
        )

    summary = {
        "total": len(results),
        "written": sum(1 for result in results if result.status == "written"),
        "matched": sum(1 for result in results if result.status in {"written", "matched"}),
        "unmatched": sum(1 for result in results if result.status == "unmatched"),
        "skipped": sum(1 for result in results if result.status == "skipped"),
    }
    return {
        "library_id": library_id,
        "export_dir": str(source_dir),
        "only_missing": only_missing,
        "dry_run": dry_run,
        "summary": summary,
        "results": [result.to_record() for result in results],
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", help="Optional Plex library id to limit sidecar generation.")
    parser.add_argument(
        "--export-dir",
        default=str(get_vidangel_export_dir()),
        help="Directory containing the VidAngel export database files.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Match titles without writing sidecars.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rewrite sidecars even when a .leapfrog.json file already exists.",
    )
    return parser


async def _amain() -> int:
    setup_logging(level="INFO")
    args = _build_arg_parser().parse_args()
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()
    result = await generate_vidangel_sidecars(
        library_id=args.library_id,
        export_dir=Path(args.export_dir),
        only_missing=not args.overwrite,
        dry_run=bool(args.dry_run),
    )
    logger.info("VidAngel sidecar generation completed: %s", result["summary"])
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
