# Leapfrog

Leapfrog is a GPL-3.0 home-server app for server-side media filtering. It scans Plex media in the background, stores detected skip segments in SQLite, and enforces skips during playback from the server instead of relying on client-side logic.

This repository is derived from [Cleanplex](https://github.com/nazmolla/Cleanplex). Attribution is preserved in [NOTICE](NOTICE) and the original GPL-3.0 license remains in [LICENSE](LICENSE).

## Current capabilities

- Local-first offline scanning on the Plex server machine with no cloud inference in the playback path
- Canonical category taxonomy across backend, frontend, export, and tests:
  `nudity`, `sexual_content`, `profanity`, `violence`, `drugs`
- Background nudity detection with `ffmpeg` frame extraction plus local NudeNet
- ONNX-backed local CLIP zero-shot classification for `sexual_content`, `violence`, and `drugs` on the shared sampled-frame pipeline
- Subtitle-first profanity detection with deterministic word and phrase matching
- Optional Whisper audio fallback when subtitles are unavailable and the local Whisper dependency is installed
- Per-user profile controls for all five categories, including toggles and thresholds
- Server-side playback enforcement through Plex session polling and seek commands
- Browser UI for scan settings, queue management, title scan detail, segment review, live logs, and linked-user preferences

Leapfrog keeps scanning, segment storage, playback filtering, and UI review local to the server box. Optional GitHub-based segment sync remains manual and is not required for scanning or playback.

## How Leapfrog works

### 1. Library sync and scan queue

Leapfrog watches Plex libraries for new items, stores them as `scan_jobs`, and queues them for offline analysis.

### 2. Detector pipeline

Each queued media file runs through detector-specific scanners:

- `nudity`
  Extracts frames with `ffmpeg`, scores them with NudeNet, clusters hits into segments, and stores thumbnails plus NudeNet labels for review.
- `sexual_content`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for intimate or sexual scenes that do not depend on nudity hits.
- `profanity`
  Loads external subtitles first, falls back to embedded subtitles, and only then attempts Whisper transcription if enabled and available.
- `violence`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for fights, blood, and weapons.
- `drugs`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for drug use and paraphernalia.

All detectors emit the same segment shape:

- `media_id`
- `start_ms`
- `end_ms`
- `category`
- `source`
- `confidence`
- `labels`
- `text_excerpt`
- `thumbnail_path`
- `review_status`

### 3. Playback enforcement

When Plex sessions are active, Leapfrog:

1. loads the current user’s effective category preferences,
2. fetches stored segments for the playing media item,
3. filters those segments by enabled category and threshold,
4. seeks the active client past matching content.

Playback-time filtering is still a database lookup plus a server-side seek. No ML inference runs in the playback hot path.

### 4. Scan status, queue, and logs

- Scan status is persisted per title and per category, plus an ordered stage timeline (`prepare`, the five categories, `finalize`).
- The scan queue is durable and mutable: titles can be moved to the top or bottom, reordered, canceled individually, canceled in batches, or canceled while active.
- The web UI includes a live logging console backed by a bounded in-memory replay buffer plus SSE updates.

## Configuration

All settings are available in the web UI at `http://your-server:7979/settings`.

Important settings:

- Plex URL and Plex token
- Poll interval
- NudeNet confidence threshold
- Semantic ONNX CLIP model preparation and per-category semantic thresholds
- Category default thresholds exposed through canonical category metadata
- Profanity term list
- Profanity allowlist for known false positives
- Default profanity threshold
- Whisper fallback on/off and model name
- Log buffer capacity for the live console
- Scan frame interval and worker count
- Segment merge settings for image and subtitle detectors

## Per-user preferences

Each linked Plex profile has:

- a master server-side filtering toggle,
- a toggle and threshold for each canonical category,
- effective skip evaluation based on stored segments rather than runtime inference.

If a user disables a category, those segments stay stored in the database but are ignored during playback enforcement for that user.

## Running locally

### Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe`
- Node.js 20+ for the web UI build
- Plex Media Server reachable on the local network for live Plex playback control
- Internet access once to download the optional local semantic ONNX model if it is not already cached under `LEAPFROG_DATA/models/`

### Environment variables

- `LEAPFROG_DATA`
  Override the data directory. Default: `~/.leapfrog`
- `LEAPFROG_HOST`
  Bind host for the FastAPI server. Default: `0.0.0.0`
- `LEAPFROG_PORT`
  Bind port for the FastAPI server. Default: `7979`
- `LEAPFROG_SYNC_GITHUB_TOKEN`
  Optional GitHub token for manual sync upload/download features

### Install

```bash
git clone https://github.com/casperson/leapfrog.git
cd leapfrog
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cd frontend
npm install
npm run build
cd ..
```

### Start

```bash
.venv/bin/python -m leapfrog
```

Open `http://localhost:7979`.

### Build a distributable package

```bash
source .venv/bin/activate
python -m build
```

The resulting wheel and sdist are written to `dist/`.

## Development workflow

### Backend and frontend checks

```bash
.venv/bin/python -m pytest tests/ -v
bash scripts/dev-start.sh
bash scripts/dev-verify.sh
bash scripts/dev-stop.sh
cd frontend && npm test
cd frontend && npm run build
```

`scripts/dev-start.sh` now works with either POSIX virtualenv layouts (`.venv/bin/leapfrog`) or Windows-style layouts (`.venv/Scripts/leapfrog.exe`).

### Optional Whisper setup

Whisper fallback is designed to be optional. If you want audio transcription fallback, install a compatible local Whisper package into the same virtual environment and keep the setting enabled in the UI.

If Whisper is unavailable, Leapfrog records the profanity scan status as unavailable instead of breaking nudity scanning or playback enforcement.

## Queue and review UI

- `Dashboard`
  Shows active scans, the mutable queue manager, and live operational status.
- `Library`
  Shows titles even when a scan is partial or clean with zero saved segments, and opens per-title scan detail.
- `Segments`
  Shows stored segments with category, source, confidence, labels, excerpts, thumbnails, and whether the selected user would currently skip them.
- `Logs`
  Shows bounded recent history plus live SSE updates from the local logging buffer.

## Adding a detector

Detectors live under `leapfrog/detectors/` and follow the shared detector interface in `leapfrog/detectors/base.py`.

Each detector should:

1. accept a `MediaScanTarget`,
2. return `DetectorResult`,
3. emit `MediaSegment` records in the shared schema,
4. avoid doing any work in the playback hot path.

## Adding an adapter

Runtime adapters now share one contract and one canonical segment format:

1. create a runtime adapter under `leapfrog/adapters/`,
2. convert server-specific playback references into `RuntimeMediaReference`,
3. reuse the shared resolver in `leapfrog/adapters/runtime_common.py`,
4. keep server-specific session or seek code outside the core domain model.

See:

- [docs/adapter-model.md](docs/adapter-model.md)
- [docs/local-first-scan-flow.md](docs/local-first-scan-flow.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/testing-and-release.md](docs/testing-and-release.md)

## Project layout

- `leapfrog/database.py`
  SQLite schema and all SQL access
- `leapfrog/scanner.py`
  Queueing and detector orchestration
- `leapfrog/detectors/`
  Category-specific scanners
- `leapfrog/logger.py`
  Process logging plus bounded replay for the UI log console
- `leapfrog/filter_engine.py`
  Playback-time segment selection and seek behavior
- `leapfrog/web/routes/`
  API surface for settings, scans, users, and segments
- `docs/current-architecture.md`
  Current internal architecture summary
- `docs/adapter-model.md`
  Canonical export and adapter contract
- `docs/testing-and-release.md`
  Test, packaging, and release checklist
- `docs/release-notes.md`
  Current release readiness notes

## Licensing and attribution

Leapfrog remains GPL-3.0 licensed. This project is derived from Cleanplex and preserves upstream attribution in [NOTICE](NOTICE).
