# Leapfrog — Mandatory Development Practices

This file is the authoritative source for how all work on this repository must be done.
Every change — regardless of size — must follow these rules. No exceptions.

---

## Table of Contents

1. [Tech Stack & Architecture](#1-tech-stack--architecture)
2. [Code Comments](#2-code-comments)
3. [Documentation Standards](#3-documentation-standards)
4. [Architecture Rules](#4-architecture-rules)
5. [Test Coverage](#5-test-coverage)
6. [Pull Request & Commit Standards](#6-pull-request--commit-standards)
7. [AI-Assisted Development](#7-ai-assisted-development)
8. [Deploy Workflow](#8-deploy-workflow)

---

## 1. Tech Stack & Architecture

| Layer | Technology | Notes |
|---|---|---|
| Backend | Python 3.11+, FastAPI, aiosqlite | All I/O must be async |
| Database | SQLite (WAL mode) | Single file at `~/.leapfrog/leapfrog.db` |
| ML Inference | NudeNet (ONNX, local) | CPU-only; wrapped in `asyncio.to_thread` |
| Video | FFmpeg / ffprobe | Subprocess via `frame_extractor.py` |
| Plex API | `plexapi` + `httpx` | Wrapped in `PlexClient` |
| Frontend | React, TypeScript, Vite, Tailwind | SPA served from `frontend/` |
| Tests | `pytest`, `pytest-asyncio` | See §6 |

**Module ownership:**

| Module | Responsibility |
|---|---|
| `database.py` | All SQL — no raw queries outside this file |
| `scanner.py` | Frame extraction, NudeNet inference, segment writing |
| `logger.py` | Process logging plus bounded in-memory replay for the web log console |
| `plex_client.py` | All Plex API calls — no `plexapi` imports elsewhere |
| `filter_engine.py` | Playback position checks and seek decisions |
| `watcher.py` | Polling loops only — no business logic |
| `sync.py` / `sync_merge.py` | GitHub sync and segment merge logic |
| `vidangel_export.py` | VidAngel catalog scraping, raw event export, and Leapfrog sidecar generation |
| `vidangel_sidecars.py` | Match VidAngel export data to Plex media files and write adjacent sidecars |
| `media_rewriter.py` | VidAngel-taxonomy rewrite planning, candidate discovery, and ffmpeg export execution |
| `bg_jobs.py` | Background job lifecycle — no domain logic |
| `web/routes/` | HTTP surface only — call domain modules, never raw DB |

---

## 2. Code Comments

### 2.1 What Must Be Commented

Comment when the logic is **not self-evident** from the code itself.

**Always comment:**

- Non-obvious algorithmic choices (`# O(n²) — acceptable for <50 segments per title`)
- Intentional trade-offs (`# Update every 30 steps to reduce write amplification`)
- Why something is done in a surprising way (`# threading.Lock, not asyncio.Lock — detector runs in a thread pool`)
- Magic numbers (`LOOKAHEAD_MS = 5000  # compensates for 5s polling latency`)
- Async/thread-safety notes (`# guarded by _state_lock — do not mutate outside _update_state()`)
- Fallback/retry logic rationale
- Any `# type: ignore` or `# noqa` — must explain why

**Never comment:**
- What the code obviously does (`# increment counter`)
- Commented-out code — delete it; git history preserves it
- TODO/FIXME

### 2.2 Module-Level Docstrings

Every Python module (`*.py`) must start with a one-line docstring:

```python
"""Short description of what this module owns."""
```

Multi-line is fine when the module has non-obvious scope or invariants.

### 2.3 Function/Method Docstrings

Required for:
- Every `async def` in `database.py` (documents query, params, return shape)
- Every `public` method in `PlexClient`
- Every FastAPI route handler

Not required for small private helpers where the name is fully self-explanatory.

Format — keep it short:

```python
async def get_segments_for_guid(plex_guid: str) -> list[dict]:
    """Return all segments for the given Plex GUID, ordered by start_ms."""
```

Only add parameter/return docs when the types or semantics are non-obvious.

### 2.4 Frontend Comments

TypeScript/React: comment the same categories as Python above.
Add a comment above any `useEffect` hook that has a non-trivial dependency array explaining the intent.

---

## 3. Documentation Standards

### 3.1 README.md

`README.md` is the user-facing reference. Update it when:

- A new configuration setting is added (add a row to the Configuration table)
- A new environment variable is added
- A new web UI page or feature is added
- A significant behaviour changes (e.g., skip logic, segment expansion)
- A new client compatibility result is known

Do **not** add implementation details or architecture notes to `README.md` — that goes in `AGENTS.md` or inline code comments.

### 3.2 AGENTS.md (this file)

Update this file when:

- A new module is added (add a row to §1 module table)
- An architectural rule changes
- A new mandatory practice is established
- A label is added to the GitHub label set

### 3.3 PHASE1_TEST_SCENARIOS.md and similar test documents

These scenario files describe integration-level expected behaviour. Update them when:
- A new sync/merge behaviour is implemented
- Edge cases are discovered and fixed
- A scenario's expected result changes

### 3.4 Changelogs

No `CHANGELOG.md` is maintained. The git log serves as the changelog. Write commit messages accordingly (see §6).

---

## 4. Architecture Rules

These rules are invariants. Breaking them requires explicit discussion before the PR.

### 4.1 Async Discipline

- **All I/O is async.** `aiosqlite`, `httpx.AsyncClient`, `asyncio.to_thread` for blocking libs.
- **Never block the event loop.** No `time.sleep`, synchronous file reads >1 KB, or synchronous network calls in the async path.
- **Use `asyncio.Lock` for shared async state.** Never use `threading.Lock` in code that runs on the event loop.
- **Wrap all `plexapi` calls in `asyncio.to_thread`.** The `plexapi` library is synchronous.

### 4.2 Database Layer

- **All SQL lives in `database.py`.** No raw SQL strings in routes, scanner, sync, or any other module.
- **Never load full rows to count.** Use `SELECT COUNT(*)` — never `len(await db.get_all_x())`.
- **No N+1 queries.** If a route or function calls the DB once per item in a list, it is a bug. Use `IN (...)`, `JOIN`, or batch queries.
- **Schema migrations are additive only.** Never drop or rename columns in a migration. Add new columns with defaults.
- **Index every foreign-key-like column** (`plex_guid`, `file_hash`, `status`).

### 4.3 HTTP & Plex Client

- **One `AsyncClient` per `PlexClient` instance.** Never construct `httpx.AsyncClient` ad-hoc inside a loop or per-request.
- **Always close `AsyncClient` on shutdown.** Wire `client.close()` into the application lifecycle.
- **Retry logic must have a ceiling.** Every retry loop must have a max-attempt count and exponential backoff or fixed cap.
- **No Plex API calls in loops without caching.** Calls like `get_episode_show_art` must be memoized within the request at minimum.

### 4.4 Scanner

- **One NudeNet detector per thread.** Use `threading.local()` — never instantiate inside a tight frame loop.
- **Scanner global state (`_current_guids`, `_skip_requested_guids`, queue wakeups, and pause/restart flags) must remain coherent with the persisted queue model.** Guard shared async mutations with the scanner state lock.
- **Segments are clustered before DB insert.** Never insert a raw frame-level row; always cluster via `_cluster_frames` / `_flush_cluster`.

### 4.5 Frontend

- **All API calls go through `src/api/`.** No `fetch`/`axios` calls inline in components or pages.
- **Polling intervals must cancel in-flight requests.** Use `AbortController` before each new poll tick.
- **No polling loop may run unbounded.** Every `setInterval`/recursive `setTimeout` must have a terminal condition (success, error, or max-duration).
- **Bulk actions must use bounded concurrency or a batch endpoint.** Never fire N sequential requests from a UI loop.

### 4.6 Sync

- **Sync is always manually triggered.** No background task or watcher may initiate a sync operation automatically.
- **File hash cache is keyed by `(path, size, mtime)`.** Recompute SHA256 only when any of these change.
- **Segment merge complexity must be sub-quadratic on practical inputs.** Use time-bucketed or sorted interval matching.

---

## 5. Test Coverage

### 5.1 Test Location & Structure

```
tests/
├── unit/
│   ├── test_database.py       # DB helpers — real in-memory SQLite
│   ├── test_sync_merge.py     # SegmentMerger logic
│   ├── test_filter_engine.py  # FilterEngine seek decisions
│   └── test_plex_client.py    # PlexClient (httpx mocked)
├── integration/
│   ├── test_routes_segments.py
│   ├── test_routes_sessions.py
│   └── test_routes_scanner.py
└── conftest.py                # shared fixtures
```

### 5.2 What Requires a Test

| Change type | Required coverage |
|---|---|
| New `database.py` function | Unit test: happy path + empty/missing input |
| New route handler | Integration test: happy path + 404/422 error cases |
| New sync / merge logic | Unit test: correctness + edge cases (empty, single source, conflict) |
| Bug fix | Regression test that fails before the fix and passes after |
| Performance optimisation | Before/after assertion (row count, call count, or timing) |
| New filter / skip logic | Unit test covering the boundary conditions |

### 5.3 Test Standards

- **Use real in-memory SQLite for database tests** — never mock the DB.
- **Mock `httpx` responses** for Plex API tests using `httpx.MockTransport` or `respx`.
- **Mock `plexapi`** at the boundary (`asyncio.to_thread` call site) — never import `plexapi` in test files.
- **Test names describe the scenario**: `test_get_segments_returns_empty_for_unknown_guid`, not `test_get_segments`.
- **Each test is independent** — no shared mutable state between tests; use fixtures.
- **`pytest-asyncio` mode: `asyncio_mode = "auto"`** (set in `pyproject.toml` or `pytest.ini`).

### 5.4 Running Tests

```bash
pytest tests/ -v
```

Tests must pass before any PR is merged. A PR that breaks existing tests will not be merged regardless of other quality.

### 5.5 Coverage Threshold

- Backend: ≥ 80% line coverage on `leapfrog/` (excluding `main.py`).
- New modules must hit 80% on first PR.
- Use `pytest --cov=leapfrog --cov-report=term-missing` to verify.

---

## 6. Pull Request & Commit Standards

### 6.1 Commit Messages

Format: `<type>: <what changed in imperative mood>`

| Type | When |
|---|---|
| `feat` | New user-visible feature or behaviour |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `refactor` | Code restructuring with no behaviour change |
| `test` | Test-only changes |
| `docs` | Documentation only |
| `chore` | Build, dependencies, tooling |

Rules:
- Subject line ≤ 72 characters
- Use body to explain **why**, not **what** (the diff shows what)
Examples:
```
perf: batch user filter lookups in sessions endpoint

Eliminates N+1 DB calls when multiple sessions are active.
Each request now loads all user filters once and maps in-memory.
```

```
fix: use asyncio.Lock for model download gate in scanner

threading.Lock blocks the event loop when the download path is reached
from an async context, causing session polling to stall.
```

### 6.2 PR Requirements

Before opening a PR:

- [ ] All existing tests pass (`pytest tests/ -v`)
- [ ] New tests added for the change (see §6.2)
- [ ] Coverage does not regress below threshold
- [ ] `README.md` updated if user-visible behaviour changed
- [ ] `AGENTS.md` updated if a new architecture rule or module was introduced
PR title mirrors the commit type:
`perf: batch user filter lookups in sessions endpoint`

---

## 7. AI-Assisted Development

When using Codex or any AI assistant on this repository:

### 7.1 Before Starting Work

- Read the relevant source files before proposing changes

### 7.2 When Writing Code

- Follow all rules in §4 Architecture Rules without exception
- Do not add helpers, abstractions, or error handling beyond what the task requires
- Do not refactor surrounding code unless the task explicitly calls for it

---

## 8. Deploy Workflow

**Every change that touches Python or frontend code must pass through a dev instance before touching production.** No exceptions — not for "trivial" fixes, not for one-liners.

### 8.1 Dev Instance

The dev instance runs on port **7980** with data dir `~/.leapfrog-dev/`. It is seeded from the production DB on first start so Plex credentials and settings are pre-configured.

Scripts:

```bash
bash scripts/dev-start.sh        # seed DB from prod (if needed) and start on :7980
bash scripts/dev-verify.sh       # smoke-test all API endpoints
bash scripts/dev-stop.sh         # stop the dev instance
```

`dev-start.sh --fresh` wipes `~/.leapfrog-dev/` and reseeds from production.

### 8.2 Mandatory Pre-Deploy Checklist

Before building the frontend or restarting the production server:

1. **Run unit tests**: `pytest tests/ -v` — all must pass
2. **Start dev instance**: `bash scripts/dev-start.sh`
3. **Verify startup log** — check `leapfrog-dev.log` for any exceptions or `AttributeError`
4. **Run smoke tests**: `bash scripts/dev-verify.sh` — all endpoints must return 200
5. **Exercise changed paths manually** — if a scanner change was made, verify a scan job runs; if UI changed, load the page in a browser at `http://localhost:7980`
6. **Stop dev instance**: `bash scripts/dev-stop.sh`
7. **Build frontend**: `cd frontend && npm run build`
8. **Deploy to production**: restart the production server

Steps 3–5 catch runtime errors (missing attributes, import failures, startup crashes) that unit tests cannot catch because they don't boot the full server.

### 8.3 Production Restart Procedure

Never kill production before the replacement is confirmed healthy:

1. Start replacement on a different port OR confirm the dev instance passes all checks
2. Kill production: `powershell -Command "Get-Process leapfrog -ErrorAction SilentlyContinue | Stop-Process -Force"`
3. Start production: `.venv/Scripts/leapfrog.exe > leapfrog_restart.log 2>&1 &`
4. Verify: poll `http://localhost:7979/api/settings` until it responds
5. Check log for errors before declaring done
