# Testing and Release Checklist

## End-to-end flow covered

The current automated coverage now includes:

- detector orchestration through `scan_video`
- subtitle profanity extraction into stored segments
- nudity segment preservation
- user preference filtering
- sidecar export generation
- runtime adapter resolution for Plex, Emby, and Jellyfin

## Local checks

```bash
.venv/bin/pytest tests/ -q
bash scripts/dev-start.sh
bash scripts/dev-verify.sh
bash scripts/dev-stop.sh
cd frontend && npm run build
```

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
