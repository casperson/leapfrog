# Leapfrog

Leapfrog is a GPL-3.0 home-server app for server-side media filtering. It scans Plex media in the background, stores detected skip segments in SQLite, and enforces skips during playback from the server instead of relying on client-side logic.

This repository is derived from [Cleanplex](https://github.com/nazmolla/Cleanplex). Attribution is preserved in [NOTICE](NOTICE) and the original GPL-3.0 license remains in [LICENSE](LICENSE).

## Current capabilities

- Local-first offline scanning on the Plex server machine with no cloud inference in the playback path
- Canonical VidAngel-first category taxonomy across backend, frontend, export, and sidecars
- Background nudity detection with `ffmpeg` frame extraction plus local NudeNet
- ONNX-backed local CLIP zero-shot classification that maps local sex, violence, and drugs detections into VidAngel labels on the shared sampled-frame pipeline
- Subtitle-first language detection with deterministic word and phrase matching mapped into VidAngel language labels
- Optional Whisper audio fallback when subtitles are unavailable and the local Whisper dependency is installed
- Per-user profile controls for VidAngel categories and exact VidAngel leaf filters, including toggles, thresholds, and granular skip choices
- Server-side playback enforcement through Plex session polling and seek commands
- Adjacent sidecar skip files for using existing segment data without rescanning the video
- Headless VidAngel export into exact raw event files, tag definitions, and Leapfrog sidecar JSON
- Browser UI for scan settings, queue management, title scan detail, segment review, live logs, and linked-user preferences

Leapfrog keeps scanning, segment storage, playback filtering, and UI review local to the server box. Optional GitHub-based segment sync remains manual and is not required for scanning or playback.

## How Leapfrog works

### 1. Library sync and scan queue

Leapfrog watches Plex libraries for new items and stores them as `scan_jobs`. It does not automatically queue everything at startup; use the dashboard queue controls to queue unscanned movies when you are ready.

### 2. Detector pipeline

Each queued media file runs through detector-specific scanners. Leapfrog still runs detector stages named `nudity`, `sexual_content`, `profanity`, `violence`, and `drugs`, but their saved segment categories are remapped into VidAngel taxonomy groups and exact VidAngel leaf labels before persistence.

- `nudity`
  Extracts frames with `ffmpeg`, scores them with NudeNet, clusters hits into segments, and maps them into VidAngel nudity and immodesty labels.
- `sexual_content`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for intimate or sexual scenes, then maps those hits into VidAngel sex and kissing labels.
- `profanity`
  Loads external subtitles first, falls back to embedded subtitles, and only then attempts Whisper transcription if enabled and available, mapping detected language into VidAngel language labels.
- `violence`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for fights, blood, and weapons, then maps those hits into VidAngel violence or human-functions labels.
- `drugs`
  Reuses the same sampled frames and applies local ONNX CLIP scoring for drug use and paraphernalia, then maps those hits into VidAngel alcohol-or-drug-use labels.

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

Leapfrog also checks for adjacent sidecar skip files before falling back to SQLite segments. A sidecar is a small file stored next to the media file that describes skip ranges for that title. Supported names are `Movie.leapfrog.json`, `Movie.segments.json`, `Movie.edl`, and `Movie.csv`. JSON sidecars preserve VidAngel taxonomy group categories and exact leaf labels; CSV rows may include `start_time`, `end_time`, `category`, `labels`, and `confidence`; EDL rows use start/end times and may optionally include a supported category and label after the timing fields.

If the dashboard shows `Skipping...` logs followed by proxy and direct seek failures, Leapfrog has matched a segment but Plex client control is failing. Check `/api/status` or `/api/sessions/{session_key}/seek-diagnostics` for the last seek attempt list, including the client identifier, advertised address/port, HTTP status, and failure detail.

For on-demand troubleshooting, you can also run a manual seek probe against an active session:

```bash
.venv/bin/python -m leapfrog.seek_probe --list-sessions
```

```bash
.venv/bin/python -m leapfrog.seek_probe --list-clients
```

```bash
.venv/bin/python -m leapfrog.seek_probe --compare-session-key 2
```

```bash
.venv/bin/python -m leapfrog.seek_probe --session-key 2 --delta-ms 1000
```

`--list-clients` dumps the Plex Companion `/clients` discovery list so you can compare active playback sessions against the set of actually controllable Companion clients.
`--compare-session-key` prints one active session next to the matched Companion advertisement, if any.

Or through the running API:

```http
POST /api/sessions/{session_key}/probe-seek
Content-Type: application/json

{"delta_ms": 1000}
```

You can also pass an explicit target offset:

```http
{"offset_ms": 1200500}
```

There is also a read-only Companion diagnostics endpoint:

```http
GET /api/sessions/companion-clients
```

And a per-session comparison endpoint:

```http
GET /api/sessions/{session_key}/companion-compare
```

### 4. Scan status, queue, and logs

- Scan status is persisted per title and per saved VidAngel category, plus an ordered stage timeline (`prepare`, detector stages, `finalize`).
- The scan queue is durable and mutable: titles can be moved to the top or bottom, reordered, canceled individually, canceled in batches, or canceled while active.
- The dashboard can queue all currently unscanned movies, either for the normal scan window or immediately.
- The web UI includes a live logging console backed by a bounded in-memory replay buffer plus SSE updates.

## Configuration

All settings are available in the web UI at `http://your-server:7979/settings`.

Important settings:

- Plex URL and Plex token
- Poll interval
- NudeNet confidence threshold for VidAngel nudity/immodesty mapping
- NudeNet model selection; the default is `640m` with a `250ms` frame interval and `0.4` threshold for higher sensitivity
- Semantic ONNX CLIP model preparation and conservative local-to-VidAngel mapping thresholds
- Semantic frame filtering suppresses text-only title/credits cards for sex-content and drug detections
- Granular VidAngel label defaults for local scanning and skip behavior
- Category default thresholds exposed through VidAngel category metadata
- Segment clustering defaults tuned for review: 2s merge gap, 2-hit minimum, and a 15s image-segment cap
- Language term list
- Language allowlist for known false positives
- Default language threshold
- Whisper fallback on/off and model name
- Log buffer capacity for the live console
- Scan frame interval and worker count
- Segment merge settings for image and subtitle detectors

## Per-user preferences

Each linked Plex profile has:

- a master server-side filtering toggle,
- a toggle and threshold for each VidAngel category group,
- per-label skip toggles for exact VidAngel filters within that category,
- effective skip evaluation based on stored segments rather than runtime inference.

If a user disables a category, those segments stay stored in the database but are ignored during playback enforcement for that user.

Common category groups include:

- `sex_nudity_immodesty`
- `sex_any`
- `kissing`
- `violence_blood_gore`
- `human_functions`
- `alcohol_or_drug_use`
- `language_blasphemy`
- `language_language_childish`
- `language_language_racial`
- `language_language_sexual`
- `language_profanity`
- `language_profanity_captions`
- `credits`

Exact selectable labels are the unprefixed VidAngel leaf keys inside those categories, such as `immodesty_female`, `shown_w_nudity`, `kissing_passion`, `graphic`, `drugs_illegal`, `fuck`, or `christ`.

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

The catalog export is a full refresh: it refetches every discovered VidAngel work and rewrites the master artifacts. It does not run Leapfrog's local video scanner. Sidecar generation remains incremental by default and skips existing adjacent `.leapfrog.json` files unless `--overwrite` is supplied.

#### `vidangel_export` command reference

```bash
.venv/bin/python -m leapfrog.vidangel_export \
  [--output-dir PATH] \
  [--concurrency N] \
  [--write-per-title-artifacts]
```

Flags:

- `--output-dir PATH`
  Write export files somewhere other than the default export directory (`repo_root/vidangel/`).
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

By default the exporter writes to `repo_root/vidangel/`:

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
- `raw_filter_events.index.json`
- `movie_filter_events.csv`
- `tv_filter_events.csv`
- `vidangel_catalog.xlsx`

This default output is intentionally database-first: one master catalog plus one master event table, not one file per VidAngel title.

#### Generate a filtered `.skp` from VidAngel export data

Leapfrog also lets you browse VidAngel titles that have exported events, review every timestamp and description, and generate a filtered `.skp` file containing only the exact events you select. The VidAngel page also keeps category and leaf-filter toggles for quickly selecting or clearing groups. The same leaf-filter path remains available from the backend and CLI for automation.

The filtered export workflow is:

1. open the VidAngel page in Leapfrog,
2. choose a title that has exported VidAngel events,
3. select whole categories or leaf filters, then include or exclude individual events,
4. generate the `.skp` download.

The exporter writes `raw_filter_events.index.json` beside the master event CSV. This compact byte index makes title changes load only the selected title's rows instead of reparsing the full export. Leapfrog also caches the large title catalog and tag definitions until their source files change.

Existing export files are prepared automatically in the background when Leapfrog starts. To prepare them explicitly without contacting VidAngel, refreshing the catalog, or scanning local movies, run:

```bash
.venv/bin/python -m leapfrog.vidangel_export prepare \
  --export-dir /path/to/existing/vidangel
```

The matching CLI command exports the same filtered selection to disk or stdout:

```bash
.venv/bin/python -m leapfrog.vidangel_export export-skp \
  --media-id movie-1 \
  --leaf-key fuck \
  --leaf-key immodesty_female \
  --output /path/to/Sample_Movie.skp
```

Important: Leapfrog only auto-loads sidecars that live adjacent to the actual movie or episode file, for example:

- `/media/Movies/Angel Has Fallen (2019).mkv`
- `/media/Movies/Angel Has Fallen (2019).leapfrog.json`

That means the current workflow is:

1. export VidAngel data,
2. match rows from `title_catalog.csv` or `title_catalog.json` against titles in your Plex library,
3. use `raw_filter_events.csv` plus `tag_definitions.*` to generate sidecars only for the titles you actually own,
4. place the matching `.leapfrog.json` file next to that media file.

Once the sidecar is adjacent to the local media file, Leapfrog will use it during playback and apply the current user's category and label preferences before deciding whether to skip.

### Convert external timestamp files

Leapfrog can convert external timestamp files into portable `.leapfrog.json` sidecars and export existing sidecars back into `.skp` text.

#### Supported import inputs

- `.skp` timestamp files
- plain text timestamp lists
- HTML pages that contain visible timestamp blocks

#### Convert `.skp` into a Leapfrog sidecar

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  to-sidecar \
  --input /path/to/Fight_Club_1999_local_faselhdwatch-local.skp \
  --input-format skp \
  --output /path/to/Fight_Club.leapfrog.json \
  --title "Fight Club" \
  --media-id "fight-club" \
  --category sex_nudity_immodesty \
  --source skp_import
```

#### Convert plain text or HTML timestamps into a Leapfrog sidecar

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  to-sidecar \
  --input /path/to/timestamps.txt \
  --input-format text \
  --output /path/to/Book_of_Eli.leapfrog.json \
  --title "The Book of Eli" \
  --category sex_nudity_immodesty \
  --source timestamp_text
```

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  to-sidecar \
  --input /path/to/page.html \
  --input-format html \
  --output /path/to/Book_of_Eli.leapfrog.json \
  --title "The Book of Eli" \
  --category sex_nudity_immodesty \
  --source timestamp_html
```

#### Fetch a URL directly and try to parse it as timestamp text or HTML

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  to-sidecar \
  --url "https://buymeacoffee.com/thetimestampdudes/timestamps-skip-the-book-eli-2010" \
  --input-format html \
  --output /path/to/Book_of_Eli.leapfrog.json \
  --title "The Book of Eli" \
  --category sex_nudity_immodesty \
  --source timestamp_url
```

Note: some pages, including BuyMeACoffee posts, may not expose the useful timestamp content in fetchable HTML. In those cases, save the page or copied timestamps locally and use `--input` instead of `--url`.

#### Convert an existing Leapfrog sidecar back into `.skp`

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  to-skp \
  --input /path/to/Fight_Club.leapfrog.json \
  --output /path/to/Fight_Club.skp \
  --alignment-json '{"local":0,"faselhdwatch":0}'
```

The `.skp` export preserves the time blocks and description text. Optional alignment JSON can be appended when you need to keep source-offset metadata in the exported file.

### Rewrite media from VidAngel leaf filters

Leapfrog also includes a Rewrite page that discovers titles with adjacent sidecars or adjacent `.skp` files, filters them using the VidAngel leaf taxonomy, and exports a sibling `.cleaned.mkv` file. Rewrite jobs are non-destructive: the original media file is never edited in place.

The rewrite workflow is:

1. open the Rewrite page in Leapfrog,
2. choose one or more rewrite-ready titles,
3. choose explicit VidAngel leaf filters or load them from a saved user profile,
4. preview the affected cut and mute durations,
5. start a background rewrite job.

Rewrite defaults:

- output container is always `mkv`
- output is written next to the original file with a `.cleaned.mkv` suffix
- non-language VidAngel leaves are always cut
- language leaves can be muted or cut
- when muting language, Leapfrog only mutes English-tagged audio streams, and it redacts matched offending words from English-tagged text subtitle streams instead of dropping the whole subtitle cue
- all detected audio streams are preserved through scene cuts; English alternate audio tracks are cut and muted on the same timeline as the primary English track
- rewriteable plain-text subtitle streams are rewritten to stay in sync after cuts; styled or image-based subtitle codecs are rejected when a rewrite would need to retime or redact them
- hardware H.264 encoding is attempted first when ffmpeg exposes a supported GPU encoder, then Leapfrog falls back to `libx264` automatically if hardware encode is unavailable or fails
- Leapfrog allows rewritten files to be up to 120% of the original file size before failing the job

The rewrite page uses these endpoints:

- `GET /api/rewrite/settings`
- `PUT /api/rewrite/settings`
- `GET /api/rewrite/candidates`
- `GET /api/rewrite/profile-leaf-keys/{user_id}`
- `POST /api/rewrite/plan`
- `POST /api/rewrite/jobs`
- `GET /api/rewrite/jobs/{job_id}`

#### Batch-convert a CSV manifest

You can also batch-convert many external timestamp sources at once from a CSV manifest:

```csv
Title,Input Type,Input,Output,Media Id,Category,Source
Fight Club,skp,/Users/bcasperson/Downloads/Fight_Club_1999_local_faselhdwatch-local.skp,/Users/bcasperson/dev/leapfrog/vidangel/Fight_Club.leapfrog.json,fight-club,sex_nudity_immodesty,skp_import
The Book of Eli,text,/Users/bcasperson/Downloads/book_of_eli_timestamps.txt,/Users/bcasperson/dev/leapfrog/vidangel/Book_of_Eli.leapfrog.json,the-book-of-eli,sex_nudity_immodesty,timestamp_text
```

Supported `Input Type` values are:

- `skp`
- `text`
- `html`
- `url`

Run the batch import like this:

```bash
.venv/bin/python -m leapfrog.skip_file_converter \
  batch-to-sidecar \
  --manifest /path/to/manifest.csv
```

A starter template with one example row is checked in at:

- [docs/skip_manifest.example.csv](/Users/bcasperson/dev/leapfrog/docs/skip_manifest.example.csv)

Each row writes one `.leapfrog.json` sidecar to the `Output` path. The command prints one `OK` or `ERROR` line per row and exits non-zero if any row fails.

#### `vidangel_sidecars` command reference

To generate sidecars only for titles in your local Plex library, run:

```bash
.venv/bin/python -m leapfrog.vidangel_sidecars
```

#### VidAngel export API

The VidAngel export browser page uses these endpoints:

- `GET /api/vidangel/export/catalog`
- `POST /api/vidangel/export/prepare`
- `GET /api/vidangel/export/titles/{media_id}/filters`
- `POST /api/vidangel/export/titles/{media_id}/skp`

`POST /api/vidangel/export/prepare` indexes and warms existing local artifacts without making VidAngel or Plex requests. `GET /api/vidangel/export/titles/{media_id}/filters` includes every event's stable `event_id`, timestamps, and description. `POST /api/vidangel/export/titles/{media_id}/skp` accepts `selected_event_ids` for exact event selection or `selected_leaf_keys` for grouped automation, and returns a downloadable `.skp` by default. Include `output_path` in the JSON body when you want Leapfrog to write the file to disk instead of returning it inline.

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
- otherwise `repo_root/vidangel/`

The generator matches:

- movies by normalized title and year
- episodes by show title, season number, and episode number resolved from Plex

Generated sidecars preserve exact VidAngel leaf labels using unprefixed keys like `fuck` or `graphic`. Those labels are exposed in the Profiles UI when `tag_definitions.json` exists in the VidAngel export directory, so each user can enable or disable specific VidAngel filters independently. At playback time Leapfrog loads the sidecar, checks the current user's category and label preferences, and skips only the timestamp windows whose labels are enabled for that user.

Each sidecar-generation run also writes coverage reports into the VidAngel export directory:

- `matched_plex_titles.json`
- `matched_plex_titles.csv`
- `matched_movie_plex_titles.json`
- `matched_movie_plex_titles.csv`
- `matched_tv_plex_titles.json`
- `matched_tv_plex_titles.csv`
- `unmatched_plex_titles.json`
- `unmatched_plex_titles.csv`
- `unmatched_movie_plex_titles.json`
- `unmatched_movie_plex_titles.csv`
- `unmatched_tv_plex_titles.json`
- `unmatched_tv_plex_titles.csv`
- `ambiguous_plex_titles.json`
- `ambiguous_plex_titles.csv`
- `ambiguous_movie_plex_titles.json`
- `ambiguous_movie_plex_titles.csv`
- `ambiguous_tv_plex_titles.json`
- `ambiguous_tv_plex_titles.csv`
- `skipped_plex_titles.json`
- `skipped_plex_titles.csv`
- `skipped_movie_plex_titles.json`
- `skipped_movie_plex_titles.csv`
- `skipped_tv_plex_titles.json`
- `skipped_tv_plex_titles.csv`
- `out_plex_titles.json`
- `out_plex_titles.csv`
- `out_movie_plex_titles.json`
- `out_movie_plex_titles.csv`
- `out_tv_plex_titles.json`
- `out_tv_plex_titles.csv`

Use `unmatched_plex_titles.*` to see which Plex titles do not have a usable VidAngel match, `ambiguous_plex_titles.*` to see titles that need manual cleanup or better matching rules, and `out_plex_titles.*` to see the combined set that did not land in `matched_plex_titles.*`. TV report files collapse fully uncovered series to a single show-level row; episode rows remain only when a show has partial VidAngel coverage.

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
  --output-dir ./vidangel
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
