#!/usr/bin/env bash
# Start a dev instance of Leapfrog on port 7980.
#
# Uses ~/.leapfrog-dev/ as the data directory, seeded from the production
# DB so Plex credentials and settings are available without manual config.
#
# Usage:
#   bash scripts/dev-start.sh           # start
#   bash scripts/dev-start.sh --fresh   # wipe dev DB and start clean

set -euo pipefail

USER_HOME="${USERPROFILE:-$HOME}"
PROD_DB="$USER_HOME/.leapfrog/leapfrog.db"
DEV_DIR="$USER_HOME/.leapfrog-dev"
DEV_DB="$DEV_DIR/leapfrog.db"
DEV_LOG="leapfrog-dev.log"
PID_FILE=".dev-server.pid"

if [[ -x ".venv/bin/leapfrog" ]]; then
  LEAPFROG_BIN=".venv/bin/leapfrog"
elif [[ -x ".venv/Scripts/leapfrog.exe" ]]; then
  LEAPFROG_BIN=".venv/Scripts/leapfrog.exe"
else
  echo "[dev] ERROR: no Leapfrog executable found in .venv"
  exit 1
fi

if [[ "${1:-}" == "--fresh" ]]; then
  echo "[dev] Wiping dev data dir $DEV_DIR"
  rm -rf "$DEV_DIR"
fi

mkdir -p "$DEV_DIR"

# Seed dev DB from production if it doesn't exist yet.
if [[ ! -f "$DEV_DB" ]]; then
  if [[ -f "$PROD_DB" ]]; then
    echo "[dev] Seeding dev DB from production ($PROD_DB)"
    cp "$PROD_DB" "$DEV_DB"
  else
    echo "[dev] No production DB found — dev instance will start unconfigured"
  fi
else
  echo "[dev] Using existing dev DB at $DEV_DB"
fi

# Kill any existing dev server.
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[dev] Stopping existing dev server (PID $OLD_PID)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
  fi
  rm -f "$PID_FILE"
fi

echo "[dev] Starting dev server on http://localhost:7980 (log: $DEV_LOG)"
DEV_PID=$(
  LEAPFROG_BIN="$LEAPFROG_BIN" DEV_DIR="$DEV_DIR" DEV_LOG="$DEV_LOG" python - <<'PY'
import os
import subprocess

command = [os.environ["LEAPFROG_BIN"]]
env = os.environ.copy()
env["LEAPFROG_DATA"] = os.environ["DEV_DIR"]
env["LEAPFROG_PORT"] = "7980"

with open(os.environ["DEV_LOG"], "ab") as log_file:
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
print(proc.pid)
PY
)
echo "$DEV_PID" > "$PID_FILE"
echo "[dev] PID $DEV_PID — waiting for startup..."

# Poll until the server responds (max 30s).
for i in $(seq 1 30); do
  sleep 1
  if curl -sf http://localhost:7980/api/status > /dev/null 2>&1; then
    echo "[dev] Server is UP at http://localhost:7980"
    exit 0
  fi
done

echo "[dev] ERROR: server did not come up after 30s — check $DEV_LOG"
exit 1
