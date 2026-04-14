# Adapter Model

Leapfrog has two adapter layers:

## 1. Export adapters

These convert canonical segment exports into target-facing JSON payloads.

- `plex`
- `emby`
- `jellyfin`
- `sidecar`

They all start from the canonical export shape in `leapfrog/segment_export.py`.

## 2. Runtime adapters

These resolve playback-time segment decisions from a server-specific media reference.

Shared contract:

- `resolve_playback_context(reference, user_preferences=None)`
- `resolve_media_id(reference)`
- `get_effective_segments(reference, user_preferences=None)`
- `get_scan_status(reference)`

Shared runtime objects live in `leapfrog/adapters/runtime_common.py`:

- `RuntimeMediaReference`
- `RuntimePlaybackContext`

## Shared precedence rules

All runtime adapters use the same segment-source precedence:

1. sidecar JSON if present
2. SQLite-backed DB segments if no sidecar exists
3. merged `sidecar+db` when both exist

All runtime adapters also use the same user preference model and threshold logic.

## Current native state

- Plex has a live playback loop and seek integration.
- Emby and Jellyfin currently implement the runtime resolution layer only.
- Native Emby and Jellyfin playback hooks are still a future step.
