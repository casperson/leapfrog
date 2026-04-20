# Local-First Scan Flow

This document describes the current local scan, queue, and review model for Leapfrog.

## Canonical categories

Leapfrog uses one canonical category list everywhere:

- `nudity`
- `sexual_content`
- `profanity`
- `violence`
- `drugs`

These keys are defined once in `leapfrog/domain.py` and reused by preferences, detectors, scan status, export payloads, and frontend metadata.

Fine-grained detector detail belongs in `labels`, not in new top-level categories. Typical labels include:

- `kissing`
- `intimate_touch`
- `fight`
- `blood`
- `weapon`
- `smoke`
- `needle`
- `pill`
- `drug_paraphernalia`

## Local detector choices

Leapfrog keeps all inference local to the Plex server machine.

- `nudity`
  Uses local NudeNet on sampled frames.
- `sexual_content`
  Uses shared local ONNX CLIP zero-shot scoring on sampled frames.
- `violence`
  Uses shared local ONNX CLIP zero-shot scoring on sampled frames.
- `drugs`
  Uses shared local ONNX CLIP zero-shot scoring on sampled frames.
- `profanity`
  Uses subtitles first, then embedded subtitles, then optional local Whisper fallback.

The semantic detector backend now lives across `leapfrog/detectors/semantic.py` and `leapfrog/detectors/clip_onnx.py`:

- the prompt bank remains centralized in `semantic.py`
- local CLIP text and vision encoders run through ONNX Runtime
- prompt embeddings are cached per label bank
- image embeddings are cached and reused across `sexual_content`, `violence`, and `drugs`
- model files are cached locally under `LEAPFROG_DATA/models/`

## Shared frame pipeline

Image-based detectors scan sampled frames once per title:

1. `scanner.py` resolves the media file and duration.
2. `frame_extractor.py` samples JPEG frames once.
3. The same `SampledFrame[]` list is passed to:
   - `NudityDetector`
   - `SexualContentDetector`
   - `ViolenceDetector`
   - `DrugsDetector`
4. Each detector returns category-specific `MediaSegment` records.
5. Frame hits are clustered into continuous segments before any DB write.

This keeps the scan path local and avoids duplicated `ffmpeg` extraction work.

## Segment model

All detectors emit the shared stored segment schema:

- `category`
- `source`
- `confidence`
- `labels`
- `text_excerpt`
- `thumbnail_path`

Playback still consumes stored segments by category and threshold. The app does not make playback decisions from `labels` in the hot path.

## Scan status model

Leapfrog persists scan visibility at two levels:

- `media_scan_status`
  Per-title, per-category summary rows for status, source, detail, progress, and segment counts.
- `media_scan_stage_status`
  Ordered stage rows for `prepare`, the five categories, and `finalize`.

This supports:

- `unscanned`
- `queued`
- `scanning`
- `partially_scanned`
- `scanned_clean`
- `scanned_flagged`
- `failed`

Titles remain visible in the UI even when `segment_count == 0` if they are queued, partial, clean, scanning, or failed.

## Queue model

The queue is persisted in `scan_jobs`, not held only in process memory.

Relevant fields:

- `queue_state`
- `queue_position`
- `queue_priority`
- `cancel_requested`
- `queue_reason`
- `queued_at`
- `queue_updated_at`

Supported mutations:

- reorder with an explicit snapshot
- move to top
- move to bottom
- move up
- move down
- cancel one queued item
- cancel selected queued items
- cancel all queued items
- cancel the current active scan

Worker claim order is stable and SQLite-backed, so the queue survives restarts and is safe to render directly in the UI.

## Logging console

The log console is backed by `leapfrog/logger.py`.

- The process still logs to stdout for normal operation.
- A bounded in-memory ring buffer mirrors log records for UI replay.
- `GET /api/logs/history` returns filtered recent entries.
- `GET /api/logs/stream` exposes filtered SSE updates.

Filtering currently supports:

- minimum log level
- source/module prefix

## Playback preference mapping

Playback remains preference-driven and local:

1. load stored user filter state and category preferences
2. resolve effective thresholds for the five canonical categories
3. load stored segments from DB or a local sidecar
4. keep only segments whose category is enabled and whose confidence meets threshold
5. seek past those segments during playback

No image or subtitle inference runs while playback is active.
