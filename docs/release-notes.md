# Release Notes

## 0.1.0

Leapfrog is now positioned as a standalone open-source media-filtering project with its own canonical segment model, runtime adapters, and release workflow.

Highlights:

- canonical multi-category segment model
- per-user category preferences and thresholds across `nudity`, `sexual_content`, `profanity`, `violence`, and `drugs`
- shared-frame local detector pipeline with NudeNet for nudity plus prompt-based local semantic scoring for the other image categories
- subtitle-first profanity detection with optional Whisper fallback
- per-title scan detail with persisted stage timelines and category counts
- durable mutable queue controls and a live logging console
- server-side playback filtering for Plex
- runtime adapter resolution for Plex, Emby, and Jellyfin
- sidecar JSON export and import-ready runtime precedence
- release-readiness health/status endpoints and end-to-end integration coverage

Attribution and licensing remain preserved in `NOTICE` and `LICENSE`.
