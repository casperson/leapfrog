# Leapfrog

Leapfrog is a GPL-3.0 home-server app for server-side media filtering. It scans Plex media in the background, stores detected skip segments in SQLite, and enforces skips during playback from the server instead of relying on client-side logic.

This repository is derived from [Cleanplex](https://github.com/nazmolla/Cleanplex). Attribution is preserved in [NOTICE](NOTICE) and the original GPL-3.0 license remains in [LICENSE](LICENSE).

## Current capabilities

- Background nudity and sexual-content detection with `ffmpeg` frame extraction plus NudeNet
- Subtitle-first profanity detection with deterministic word and phrase matching
- Optional Whisper audio fallback when subtitles are unavailable and the local Whisper dependency is installed
- Per-user profile controls for `nudity` and `profanity`, including category toggles and thresholds
- Server-side playback enforcement through Plex session polling and seek commands
- Browser UI for scan settings, segment review, scan status, and linked-user preferences

## How Leapfrog works

### 1. Library sync and scan queue

Leapfrog watches Plex libraries for new items, stores them as `scan_jobs`, and queues them for offline analysis.

### 2. Detector pipeline

Each queued media file runs through detector-specific scanners:

- `nudity`
  Extracts frames with `ffmpeg`, scores them with NudeNet, clusters hits into segments, and stores thumbnails for review.
- `profanity`
  Loads external subtitles first, falls back to embedded subtitles, and only then attempts Whisper transcription if enabled and available.

All detectors emit the same segment shape:

- `media_id`
- `start_ms`
- `end_ms`
- `category`
- `source`
- `confidence`
- `text_excerpt`
- `review_status`

### 3. Playback enforcement

When Plex sessions are active, Leapfrog:

1. loads the current user’s effective category preferences,
2. fetches stored segments for the playing media item,
3. filters those segments by enabled category and threshold,
4. seeks the active client past matching content.

Playback-time filtering is still a database lookup plus a server-side seek. No ML inference runs in the playback hot path.

## Configuration

All settings are available in the web UI at `http://your-server:7979/settings`.

Important settings:

- Plex URL and Plex token
- Poll interval
- NudeNet confidence threshold
- Profanity term list
- Default profanity threshold
- Whisper fallback on/off and model name
- Scan frame interval and worker count
- Segment merge settings for nudity and profanity

## Per-user preferences

Each linked Plex profile has:

- a master server-side filtering toggle,
- a `nudity` toggle and threshold,
- a `profanity` toggle and threshold.

If a user disables a category, those segments stay stored in the database but are ignored during playback enforcement for that user.

## Running locally

### Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe`
- Node.js 20+ for the web UI build
- Plex Media Server reachable on the local network for live Plex playback control

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
cd frontend && npm run build
```

`scripts/dev-start.sh` now works with either POSIX virtualenv layouts (`.venv/bin/leapfrog`) or Windows-style layouts (`.venv/Scripts/leapfrog.exe`).

### Optional Whisper setup

Whisper fallback is designed to be optional. If you want audio transcription fallback, install a compatible local Whisper package into the same virtual environment and keep the setting enabled in the UI.

If Whisper is unavailable, Leapfrog records the profanity scan status as unavailable instead of breaking nudity scanning or playback enforcement.

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
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/testing-and-release.md](docs/testing-and-release.md)

## Project layout

- `leapfrog/database.py`
  SQLite schema and all SQL access
- `leapfrog/scanner.py`
  Queueing and detector orchestration
- `leapfrog/detectors/`
  Category-specific scanners
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
