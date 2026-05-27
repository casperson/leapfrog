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
- Per-user profile controls for all five categories, including toggles, thresholds, and granular label skip choices
- Server-side playback enforcement through Plex session polling and seek commands
- Adjacent sidecar skip files for using existing segment data without rescanning the video
- Headless VidAngel export into exact raw event files, tag definitions, and Leapfrog sidecar JSON
- Browser UI for scan settings, queue management, title scan detail, segment review, live logs, and linked-user preferences

Leapfrog keeps scanning, segment storage, playback filtering, and UI review local to the server box. Optional GitHub-based segment sync remains manual and is not required for scanning or playback.

## How Leapfrog works

### 1. Library sync and scan queue

Leapfrog watches Plex libraries for new items and stores them as `scan_jobs`. It does not automatically queue everything at startup; use the dashboard queue controls to queue unscanned movies when you are ready.

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
3. filters those segments by enabled category, enabled labels, and threshold,
4. seeks the active client past matching content.

Playback-time filtering is still a database lookup plus a server-side seek. No ML inference runs in the playback hot path.

Leapfrog also checks for adjacent sidecar skip files before falling back to SQLite segments. A sidecar is a small file stored next to the media file that describes skip ranges for that title. Supported names are `Movie.leapfrog.json`, `Movie.segments.json`, `Movie.edl`, and `Movie.csv`. JSON sidecars preserve Leapfrog categories and labels; CSV rows may include `start_time`, `end_time`, `category`, `labels`, and `confidence`; EDL rows use start/end times and may optionally include a supported category and label after the timing fields.

If the dashboard shows `Skipping...` logs followed by proxy and direct seek failures, Leapfrog has matched a segment but Plex client control is failing. Check `/api/status` or `/api/sessions/{session_key}/seek-diagnostics` for the last seek attempt list, including the client identifier, advertised address/port, HTTP status, and failure detail.

### 4. Scan status, queue, and logs

- Scan status is persisted per title and per category, plus an ordered stage timeline (`prepare`, the five categories, `finalize`).
- The scan queue is durable and mutable: titles can be moved to the top or bottom, reordered, canceled individually, canceled in batches, or canceled while active.
- The dashboard can queue all currently unscanned movies, either for the normal scan window or immediately.
- The web UI includes a live logging console backed by a bounded in-memory replay buffer plus SSE updates.

## Configuration

All settings are available in the web UI at `http://your-server:7979/settings`.

Important settings:

- Plex URL and Plex token
- Poll interval
- NudeNet confidence threshold
- NudeNet model selection; the default is `640m` with a `250ms` frame interval and `0.4` threshold for higher sensitivity
- Semantic ONNX CLIP model preparation and conservative per-category semantic thresholds
- Semantic frame filtering suppresses text-only title/credits cards for sexual-content and drug detections
- Semantic detection defaults keep broad sexual-content labels opt-in to reduce repeated false positives
- Granular label defaults for detection and skip behavior
- Category default thresholds exposed through canonical category metadata
- Segment clustering defaults tuned for review: 2s merge gap, 2-hit minimum, and a 15s image-segment cap
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
- per-label skip toggles for granular control within a category,
- effective skip evaluation based on stored segments rather than runtime inference.

If a user disables a category, those segments stay stored in the database but are ignored during playback enforcement for that user.

Labels provide detail without expanding the top-level category list. Examples include:

- `sexual_content`: `explicit_sex`, `simulated_sex`, `oral_sex`, `masturbation`, `sexual_touching`, `intimate_touch`, `bed_intimacy`, `heavy_making_out`, `romantic_kiss`, `brief_kiss`, `lingerie`, `striptease`
- `violence`: `graphic_violence`, `blood`, `fight`, `weapon_threat`, `gunfire`, `stabbing`, `explosion`, `dead_body`, `disturbing_image`, `medical_injury`
- `drugs`: `hard_drug_use`, `needle`, `powder_drugs`, `pill_abuse`, `drug_paraphernalia`, `smoking_drugs`, `marijuana`, `alcohol_abuse`, `tobacco`
- `nudity`: NudeNet body-part labels such as `FEMALE_BREAST_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`, and `BUTTOCKS_EXPOSED`

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
- `LEAPFROG_VIDANGEL_AUTHORIZATION`
  VidAngel API authorization header value, for example `Token ...`
- `LEAPFROG_VIDANGEL_PROFILE_ID`
  VidAngel profile id used for tag-set requests
- `LEAPFROG_VIDANGEL_HEADERS_FILE`
  Optional JSON or plain-text header file containing `authorization`, `x-profile`, and related VidAngel headers
- `LEAPFROG_VIDANGEL_APP_VERSION`
  Optional override for the VidAngel web app version header
- `LEAPFROG_VIDANGEL_CONCURRENCY`
  Optional max concurrent VidAngel requests for the exporter. Default: `6`

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

### Export VidAngel filters

Once you have a valid VidAngel authorization token and profile id, Leapfrog can export:

- exact raw VidAngel events as JSON and CSV,
- leaf tag definitions with example descriptions when VidAngel provides them,
- a master title catalog you can filter against your own Plex library later.

The most reliable command is the module entrypoint:

```bash
LEAPFROG_VIDANGEL_AUTHORIZATION='Token ...' \
LEAPFROG_VIDANGEL_PROFILE_ID='736336' \
.venv/bin/python -m leapfrog.vidangel_export
```

#### `vidangel_export` command reference

```bash
.venv/bin/python -m leapfrog.vidangel_export \
  [--output-dir PATH] \
  [--concurrency N] \
  [--write-per-title-artifacts]
```

Flags:

- `--output-dir PATH`
  Write export files somewhere other than the default export directory.
- `--concurrency N`
  Limit concurrent VidAngel HTTP requests. Default: `6`
- `--write-per-title-artifacts`
  Also emit:
  - `raw_titles/*.json`
  - `sidecars/*.leapfrog.json`
  The default export stays database-first and does not create one file per VidAngel title.

Authentication may be provided with either:

- `LEAPFROG_VIDANGEL_AUTHORIZATION` and `LEAPFROG_VIDANGEL_PROFILE_ID`, or
- `LEAPFROG_VIDANGEL_HEADERS_FILE=/path/to/vidangel-headers.json`

By default the exporter writes to `~/.leapfrog/vidangel_exports/`:

- `catalog_manifest.json`
- `title_catalog.json`
- `title_catalog.csv`
- `movies_catalog.json`
- `movies_catalog.csv`
- `tv_catalog.json`
- `tv_catalog.csv`
- `shows_catalog.json`
- `shows_catalog.csv`
- `tag_definitions.json`
- `tag_definitions.csv`
- `raw_filter_events.csv`
- `movie_filter_events.csv`
- `tv_filter_events.csv`
- `vidangel_catalog.xlsx`

This default output is intentionally database-first: one master catalog plus one master event table, not one file per VidAngel title.

Important: Leapfrog only auto-loads sidecars that live adjacent to the actual movie or episode file, for example:

- `/media/Movies/Angel Has Fallen (2019).mkv`
- `/media/Movies/Angel Has Fallen (2019).leapfrog.json`

That means the current workflow is:

1. export VidAngel data,
2. match rows from `title_catalog.csv` or `title_catalog.json` against titles in your Plex library,
3. use `raw_filter_events.csv` plus `tag_definitions.*` to generate sidecars only for the titles you actually own,
4. place the matching `.leapfrog.json` file next to that media file.

Once the sidecar is adjacent to the local media file, Leapfrog will use it during playback and apply the current user's category and label preferences before deciding whether to skip.

#### `vidangel_sidecars` command reference

To generate sidecars only for titles in your local Plex library, run:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars
```

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars \
  [--library-id ID] \
  [--export-dir PATH] \
  [--dry-run] \
  [--overwrite]
```

Flags:

- `--library-id <id>` limits sidecar generation to one Plex library
- `--export-dir PATH` reads the VidAngel export database from a different directory
- `--dry-run` reports matches without writing files
- `--overwrite` rewrites existing `.leapfrog.json` files

If `--export-dir` is omitted, the generator reads from:

- `LEAPFROG_VIDANGEL_EXPORT_DIR`, if set
- otherwise `~/.leapfrog/vidangel_exports/`

The generator matches:

- movies by normalized title and year
- episodes by show title, season number, and episode number resolved from Plex

Generated sidecars preserve exact VidAngel leaf labels using keys like `vidangel:fuck` or `vidangel:graphic`. Those labels are exposed in the Profiles UI when `tag_definitions.json` exists in the VidAngel export directory, so each user can enable or disable specific VidAngel filters independently. At playback time Leapfrog loads the sidecar, checks the current user's category and label preferences, and skips only the timestamp windows whose labels are enabled for that user.

Each sidecar-generation run also writes coverage reports into the VidAngel export directory:

- `matched_plex_titles.json`
- `matched_plex_titles.csv`
- `unmatched_plex_titles.json`
- `unmatched_plex_titles.csv`
- `ambiguous_plex_titles.json`
- `ambiguous_plex_titles.csv`
- `skipped_plex_titles.json`
- `skipped_plex_titles.csv`

Use `unmatched_plex_titles.*` to see which Plex titles do not have a usable VidAngel match, and `ambiguous_plex_titles.*` to see titles that need manual cleanup or better matching rules.

#### VidAngel environment variables

- `LEAPFROG_VIDANGEL_AUTHORIZATION`
  VidAngel API authorization header value, for example `Token ...`
- `LEAPFROG_VIDANGEL_PROFILE_ID`
  VidAngel profile id used for tag-set requests
- `LEAPFROG_VIDANGEL_HEADERS_FILE`
  Optional JSON or plain-text header file containing `authorization`, `x-profile`, and related VidAngel headers
- `LEAPFROG_VIDANGEL_APP_VERSION`
  Optional override for the VidAngel web app version header
- `LEAPFROG_VIDANGEL_CONCURRENCY`
  Optional max concurrent VidAngel requests for the exporter. Default: `6`
- `LEAPFROG_VIDANGEL_EXPORT_DIR`
  Optional default directory used by the sidecar generator when `--export-dir` is omitted

For unattended runs, store the VidAngel headers in a file and point the exporter at it:

```bash
.venv/bin/python -m leapfrog.vidangel_export \
  --output-dir ~/.leapfrog/vidangel_exports
```

with either:

- `LEAPFROG_VIDANGEL_AUTHORIZATION` and `LEAPFROG_VIDANGEL_PROFILE_ID`, or
- `LEAPFROG_VIDANGEL_HEADERS_FILE=/path/to/vidangel-headers.json`

where `vidangel-headers.json` looks like:

```json
{
  "authorization": "Token ...",
  "x-profile": "736336",
  "x-app-version": "2026-05-22_07-35-15"
}
```

#### Sidecar generation API

Route:

```http
POST /api/vidangel/sidecars/generate
```

Example body:

```json
{
  "library_id": "5",
  "dry_run": true,
  "overwrite": false
}
```

Fields:

- `library_id`: optional Plex library id
- `dry_run`: optional, default `false`
- `overwrite`: optional, default `false`

#### Common workflows

Build the master VidAngel database:

```bash
LEAPFROG_VIDANGEL_AUTHORIZATION='Token ...' \
LEAPFROG_VIDANGEL_PROFILE_ID='736336' \
.venv/bin/python -m leapfrog.vidangel_export
```

Build the database and also emit one raw JSON and one sidecar per VidAngel title:

```bash
LEAPFROG_VIDANGEL_AUTHORIZATION='Token ...' \
LEAPFROG_VIDANGEL_PROFILE_ID='736336' \
.venv/bin/python -m leapfrog.vidangel_export --write-per-title-artifacts
```

Check which Plex titles match before writing sidecars:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars --dry-run
```

Generate sidecars for all Plex titles that match:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars
```

Generate sidecars for one Plex library only:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars --library-id 5
```

Rewrite sidecars even if they already exist:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars --overwrite
```

Use a non-default VidAngel export directory:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars \
  --export-dir /path/to/vidangel_exports \
  --dry-run
```

### Windows: run after install

If the repository is already installed, use the Windows helper script for normal starts after pulling changes.

Start or restart Leapfrog in the background:

```powershell
cd C:\path\to\leapfrog
powershell -ExecutionPolicy Bypass -File .\scripts\windows-start.ps1
```

That script now:

- stops the existing background Leapfrog process if one is running,
- refreshes the editable Python install with `pip install -e .`,
- runs `npm install` if `frontend\node_modules` is missing,
- runs `npm run build`,
- starts `.\.venv\Scripts\leapfrog.exe` hidden in the background.

Open `http://localhost:7979`.

For a faster start when you know nothing changed, you can skip the refresh/build steps:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows-start.ps1 -SkipPackageRefresh -SkipFrontendBuild
```

### Windows: run without leaving a terminal open

Leapfrog can run in the background on Windows without keeping a terminal window open.

Start it detached:

```powershell
cd C:\path\to\leapfrog
powershell -ExecutionPolicy Bypass -File .\scripts\windows-start.ps1
```

That helper script:

- stops an existing Leapfrog background process before relaunching,
- refreshes the editable Python package,
- rebuilds frontend assets,
- launches `.\.venv\Scripts\leapfrog.exe` in the background,
- writes stdout to `leapfrog.out.log`,
- writes stderr to `leapfrog.err.log`,
- stores the background PID in `.leapfrog-windows.pid`.

Stop the background process:

```powershell
cd C:\path\to\leapfrog
powershell -ExecutionPolicy Bypass -File .\scripts\windows-stop.ps1
```

Optional custom location or port:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows-start.ps1 `
  -DataDir "D:\LeapfrogData" `
  -ListenHost "0.0.0.0" `
  -Port "7979"
```

### Windows: start automatically on system startup

Use Windows Task Scheduler.

Create a task with:

- Program: `powershell.exe`
- Arguments: `-ExecutionPolicy Bypass -File C:\path\to\leapfrog\scripts\windows-start.ps1`
- Start in: `C:\path\to\leapfrog`

Recommended task settings:

- Trigger: `At log on` or `At startup`
- Run whether user is logged on or not: enabled if you want it headless
- Run with highest privileges: enabled
- Configure for: your current Windows version

If you need a custom data dir or port at startup, edit the task arguments:

- `-ExecutionPolicy Bypass -File C:\path\to\leapfrog\scripts\windows-start.ps1 -DataDir D:\LeapfrogData -Port 7979`

Run the task as the same Windows user account that can access:

- your Plex media files
- your chosen `LEAPFROG_DATA` directory
- any local model cache location Leapfrog uses

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
  Shows active scans, the mutable queue manager, controls for queueing unscanned movies, and live operational status.
- `Library`
  Shows titles even when a scan is partial or clean with zero saved segments, and opens per-title scan detail.
- `Segments`
  Shows stored segments with category, source, confidence, labels, excerpts, thumbnails, and whether the selected user would currently skip them.
- `Logs`
  Shows bounded recent history plus live SSE updates from the local logging buffer.
- `Settings`
  Includes maintenance controls to clear stored segments, thumbnails, scan status, and queued scans before a clean rescan.

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
