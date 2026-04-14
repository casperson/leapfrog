# Contributing to Leapfrog

Leapfrog is a GPL-3.0 fork of Leapfrog focused on server-side media filtering with canonical segment exports and adapter-based playback resolution.

## Development setup

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

## Test workflow

Run the backend suite:

```bash
.venv/bin/pytest tests/ -q
```

Run the dev smoke flow:

```bash
bash scripts/dev-start.sh
bash scripts/dev-verify.sh
bash scripts/dev-stop.sh
```

## Architecture extension points

- Add detectors under `leapfrog/detectors/` and return `DetectorResult`.
- Add runtime adapters under `leapfrog/adapters/` and reuse `RuntimePlaybackAdapter`.
- Keep all SQL in `leapfrog/database.py`.
- Keep playback-time behavior limited to segment lookup, filtering, and seek decisions.

## Contributor expectations

- Add tests for any behavior change.
- Prefer additive schema changes.
- Keep Plex-, Emby-, and Jellyfin-specific logic out of the core segment model.
- Preserve upstream attribution in `NOTICE` and the GPL-3.0 license files.
