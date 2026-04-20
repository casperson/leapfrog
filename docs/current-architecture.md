# Leapfrog Current Architecture

Leapfrog is organized around a server-side media-filtering execution loop: Plex discovery populates `scan_jobs`, background workers scan media files offline, detected segments are stored in SQLite, and playback enforcement happens server-side by polling active sessions and issuing seeks through Plex.

## Runtime flow

1. `leapfrog/main.py` bootstraps the SQLite database, loads settings into `Config`, initializes the Plex client, and runs the FastAPI app plus the watcher and scanner loops.
2. `leapfrog/watcher.py` polls Plex for active sessions and new library items.
3. `leapfrog/scanner.py` manages the scan queue and runs category detectors against each queued media file.
4. `leapfrog/filter_engine.py` performs playback-time segment selection and seeks the active Plex client when the user’s enabled categories match the current position.

## Core backend modules

- `leapfrog/database.py`
  Owns the SQLite schema and all SQL access. Segments, user preferences, scan jobs, queue state, scan status rows, settings, sync metadata, and background jobs are all persisted here.
- `leapfrog/plex_client.py`
  Wraps all Plex API access, including active sessions, library enumeration, image proxying, and server-side seek commands.
- `leapfrog/scanner.py`
  Owns queueing, worker lifecycle, pause/restart/force-scan behavior, shared-frame extraction, and detector orchestration.
- `leapfrog/detectors/nudity.py`
  Runs the existing frame extraction plus NudeNet path and emits `nudity` segments.
- `leapfrog/detectors/semantic.py`
  Reuses shared sampled frames for `sexual_content`, `violence`, and `drugs` with the semantic prompt bank and detector orchestration.
- `leapfrog/detectors/clip_onnx.py`
  Runs the local ONNX CLIP text and vision encoders, manages model downloads into app storage, and caches prompt plus frame embeddings.
- `leapfrog/detectors/profanity.py`
  Loads subtitles first, falls back to Whisper when available, performs deterministic profanity matching, and emits `profanity` segments.
- `leapfrog/subtitles.py`
  Discovers external subtitle files, extracts embedded subtitles through `ffmpeg`, and parses SRT/WebVTT cues.
- `leapfrog/transcription.py`
  Wraps optional local Whisper transcription for subtitle fallback.
- `leapfrog/logger.py`
  Mirrors process logs into a bounded in-memory replay buffer for the web log console and SSE stream.
- `leapfrog/preferences.py`
  Resolves effective user category settings and filters stored segments before playback enforcement.
- `leapfrog/adapters/runtime_common.py`
  Owns shared runtime adapter types, sidecar precedence, DB fallback, and effective segment filtering for Plex, Emby, and Jellyfin.

## Data model

The canonical segment record now keeps the original `plex_guid`-based storage for compatibility while layering a richer domain model on top:

- `segments`
  Stores `media_id`, `start_ms`, `end_ms`, `category`, `source`, `confidence`, `text_excerpt`, `thumbnail_path`, `labels`, and `review_status`.
- `user_filters`
  Preserves the legacy per-user master on/off switch.
- `user_preferences`
  Adds per-user toggles and thresholds for the canonical categories `nudity`, `sexual_content`, `profanity`, `violence`, and `drugs`.
- `scan_jobs`
  Stores job lifecycle plus durable queue state (`queue_state`, `queue_position`, `queue_priority`, `cancel_requested`, `queued_at`).
- `media_scan_status`
  Tracks per-title, per-category status, source, detail, progress, and persisted segment counts.
- `media_scan_stage_status`
  Tracks the ordered stage timeline for `prepare`, the five category stages, and `finalize`.

## Current detector pipeline

- `scanner.py` extracts sampled JPEG frames once per title and fans them out to all image-based detectors.
- Nudity detection still uses local NudeNet and emits thumbnail-backed segments with `category="nudity"` and `source="nudenet"`.
- Sexual-content, violence, and drugs detection reuse the same sampled frames through `leapfrog/detectors/semantic.py` and `leapfrog/detectors/clip_onnx.py`. The current backend is a real local ONNX CLIP zero-shot scorer using cached text embeddings and shared frame embeddings.
- Profanity detection runs separately from the video frame classifier path. It prefers external or embedded subtitles, then falls back to Whisper transcription if enabled and available. It stores matched caption/transcript excerpts in the segment row.

## Playback enforcement

Playback remains server-side. The watcher loads a user’s effective category settings, resolves a runtime adapter context, filters stored or sidecar-backed segments by enabled category and threshold, then asks `PlexClient.seek(...)` to jump over matching content. No ML inference happens in the playback hot path.

## Queue, status, and logs

- The scan queue is explicit and mutable through persisted `scan_jobs` ordering instead of an in-memory-only FIFO.
- Title detail is built from both `media_scan_status` and `media_scan_stage_status`, which means partially scanned titles remain visible even when they currently have zero saved segments.
- The web UI consumes `/api/scan/queue`, `/api/titles/{guid}/scan-details`, and `/api/logs/*` to expose queue mutation, scan timelines, and live logs without adding any cloud dependency.

## Web UI

- `frontend/src/pages/Users.tsx` manages overall profile filtering plus canonical category toggles and thresholds.
- `frontend/src/pages/Library.tsx` shows library titles, partial scan visibility, inline segment review, and per-title scan detail.
- `frontend/src/pages/Segments.tsx` shows segment timing, category, source, labels, text excerpts, thumbnails, and effective skip decisions for the selected profile.
- `frontend/src/pages/Dashboard.tsx` includes the queue manager and active-scan controls.
- `frontend/src/pages/Logs.tsx` exposes the live logging console.
- `frontend/src/pages/Settings.tsx` exposes detector settings, profanity term configuration, and Whisper fallback controls.

## Compatibility notes

- Internal module paths still use the historical `leapfrog` package name to avoid a destabilizing rewrite.
- User-facing branding, docs, and metadata are moving to `Leapfrog`.
- The schema migration is additive, so existing nudity/profanity scans remain readable after upgrade while new queue and scan-status fields are added in place.
- Runtime adapters for Emby and Jellyfin currently resolve media and effective segments, but live native playback hooks for those servers are still future work.
