# Leapfrog Current Architecture

Leapfrog is organized around a server-side media-filtering execution loop: Plex discovery populates `scan_jobs`, background workers scan media files offline, detected segments are stored in SQLite, and playback enforcement happens server-side by polling active sessions and issuing seeks through Plex.

## Runtime flow

1. `leapfrog/main.py` bootstraps the SQLite database, loads settings into `Config`, initializes the Plex client, and runs the FastAPI app plus the watcher and scanner loops.
2. `leapfrog/watcher.py` polls Plex for active sessions and new library items.
3. `leapfrog/scanner.py` manages the scan queue and runs category detectors against each queued media file.
4. `leapfrog/filter_engine.py` performs playback-time segment selection and seeks the active Plex client when the user’s enabled categories match the current position.

## Core backend modules

- `leapfrog/database.py`
  Owns the SQLite schema and all SQL access. Segments, user preferences, scan jobs, settings, sync metadata, and background jobs are all persisted here.
- `leapfrog/plex_client.py`
  Wraps all Plex API access, including active sessions, library enumeration, image proxying, and server-side seek commands.
- `leapfrog/scanner.py`
  Owns queueing, worker lifecycle, pause/restart/force-scan behavior, and detector orchestration.
- `leapfrog/detectors/nudity.py`
  Runs the existing frame extraction plus NudeNet path and emits `nudity` segments.
- `leapfrog/detectors/profanity.py`
  Loads subtitles first, falls back to Whisper when available, performs deterministic profanity matching, and emits `profanity` segments.
- `leapfrog/subtitles.py`
  Discovers external subtitle files, extracts embedded subtitles through `ffmpeg`, and parses SRT/WebVTT cues.
- `leapfrog/transcription.py`
  Wraps optional local Whisper transcription for subtitle fallback.
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
- `user_category_preferences`
  Adds per-user toggles and thresholds for categories such as `nudity` and `profanity`.
- `media_scan_status`
  Tracks whether a title is pending, scanned, unavailable, or failed for each supported category.

## Current detector pipeline

- Nudity detection still uses `ffmpeg` frame extraction plus NudeNet. The detector emits thumbnail-backed segments with `category="nudity"` and `source="nudenet"`.
- Profanity detection runs separately from the video frame scanner. It prefers external or embedded subtitles, then falls back to Whisper transcription if enabled and available. It stores matched caption/transcript excerpts in the segment row.

## Playback enforcement

Playback remains server-side. The watcher loads a user’s effective category settings, resolves a runtime adapter context, filters stored or sidecar-backed segments by enabled category and threshold, then asks `PlexClient.seek(...)` to jump over matching content. No ML inference happens in the playback hot path.

## Web UI

- `frontend/src/pages/Users.tsx` manages overall profile filtering plus category-level toggles and thresholds.
- `frontend/src/pages/Segments.tsx` shows segment timing, category, source, labels, and text excerpts.
- `frontend/src/pages/Settings.tsx` exposes detector settings, profanity term configuration, and Whisper fallback controls.

## Compatibility notes

- Internal module paths still use the historical `leapfrog` package name to avoid a destabilizing rewrite.
- User-facing branding, docs, and metadata are moving to `Leapfrog`.
- The schema migration is additive, so existing nudity scans remain readable after upgrade.
- Runtime adapters for Emby and Jellyfin currently resolve media and effective segments, but live native playback hooks for those servers are still future work.
