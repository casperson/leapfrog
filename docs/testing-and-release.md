# Testing and Release Checklist

## End-to-end flow covered

The current automated coverage now includes:

- shared-frame detector orchestration through `scan_video`
- deterministic subtitle profanity extraction into stored segments
- deterministic image-detector persistence across all five categories
- per-title scan status and scan-detail API payloads
- mutable queue snapshot and queue mutation routes
- log history replay plus SSE stream formatting
- user preference filtering and effective skip-set generation
- sidecar export generation
- runtime adapter resolution for Plex, Emby, and Jellyfin
- frontend contract coverage for queue, segment review, scan detail, and log console payloads

## Local checks

```bash
.venv/bin/python -m pytest tests/ -v
bash scripts/dev-start.sh
bash scripts/dev-verify.sh
bash scripts/dev-stop.sh
cd frontend && npm test
cd frontend && npm run build
```

The dev-start smoke path is required because schema migration and startup integration problems can pass unit tests while still preventing the local server from booting against an existing database.

## Packaging checks

```bash
source .venv/bin/activate
python -m build
```

## Release notes

Release notes are tracked in [docs/release-notes.md](release-notes.md).

## Known gaps before a broader public release

- Native Emby playback polling and seek hooks
- Native Jellyfin playback polling and seek hooks
- Container images are not yet maintained in-repo
- Whisper remains optional and depends on the user installing a compatible local package
- The semantic image backend is still a local heuristic scorer and needs threshold tuning against real media before it should be treated as fully calibrated
