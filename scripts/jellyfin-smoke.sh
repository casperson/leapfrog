#!/usr/bin/env bash
# Optional Jellyfin-focused smoke test for a running Leapfrog instance.
#
# Usage:
#   bash scripts/jellyfin-smoke.sh
#
# Optional environment overrides:
#   BASE_URL=http://localhost:7980
#   EXPECT_CONFIGURED=1
#   JELLYFIN_IMAGE_REF=/Items/<id>/Images/Primary?tag=<tag>
#   JELLYFIN_SEGMENT_ID=<segment id>
#   JELLYFIN_SESSION_KEY=<active session key>

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:7980}"
EXPECT_CONFIGURED="${EXPECT_CONFIGURED:-0}"
PASS=0
FAIL=0

check_status() {
  local desc="$1"
  local method="$2"
  local url="$3"
  local expect="${4:-200}"
  local status

  status=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" --max-time 10 "$url" 2>/dev/null)
  [[ -z "$status" ]] && status="000"
  if [[ "$status" == "$expect" ]]; then
    echo "  PASS  $desc ($status)"
    ((PASS++)) || true
  else
    echo "  FAIL  $desc — expected $expect, got $status"
    ((FAIL++)) || true
  fi
}

check_json_field() {
  local desc="$1"
  local url="$2"
  local python_expr="$3"
  local body

  body=$(curl -s --max-time 10 "$url" 2>/dev/null || true)
  if [[ -z "$body" ]]; then
    echo "  FAIL  $desc — empty response"
    ((FAIL++)) || true
    return
  fi

  if RESPONSE_JSON="$body" PYTHON_EXPR="$python_expr" python - <<'PY'
import json
import os

payload = json.loads(os.environ["RESPONSE_JSON"])
if not eval(os.environ["PYTHON_EXPR"], {"payload": payload}):
    raise SystemExit(1)
PY
  then
    echo "  PASS  $desc"
    ((PASS++)) || true
  else
    echo "  FAIL  $desc"
    ((FAIL++)) || true
  fi
}

echo "=== Leapfrog Jellyfin smoke tests ==="

check_status "API /api/status" "GET" "$BASE_URL/api/status"
check_status "API /api/settings" "GET" "$BASE_URL/api/settings"
check_status "API /api/server-id" "GET" "$BASE_URL/api/server-id"
check_status "API /api/libraries" "GET" "$BASE_URL/api/libraries"
check_status "API /api/users" "GET" "$BASE_URL/api/users"
check_status "API /api/sessions" "GET" "$BASE_URL/api/sessions"

check_json_field "settings report jellyfin as the active server type" "$BASE_URL/api/settings" 'payload.get("server_type") == "jellyfin"'

if [[ "$EXPECT_CONFIGURED" == "1" ]]; then
  check_json_field "status reports Jellyfin as configured and connected" "$BASE_URL/api/status" 'payload.get("configured") is True and payload.get("adapters", {}).get("jellyfin", {}).get("native_connected") is True'
fi

if [[ -n "${JELLYFIN_IMAGE_REF:-}" ]]; then
  check_status "API /api/server-image (authenticated Jellyfin artwork proxy)" "GET" "$BASE_URL/api/server-image?ref=${JELLYFIN_IMAGE_REF}" "200"
fi

if [[ -n "${JELLYFIN_SEGMENT_ID:-}" ]]; then
  check_status "API /api/segments/${JELLYFIN_SEGMENT_ID}/jump" "POST" "$BASE_URL/api/segments/${JELLYFIN_SEGMENT_ID}/jump" "200"
fi

if [[ -n "${JELLYFIN_SESSION_KEY:-}" ]]; then
  check_status "API /api/sessions/${JELLYFIN_SESSION_KEY}/skip" "POST" "$BASE_URL/api/sessions/${JELLYFIN_SESSION_KEY}/skip" "200"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ $FAIL -gt 0 ]]; then
  echo "JELLYFIN SMOKE TEST FAILED"
  exit 1
fi

echo "JELLYFIN SMOKE TEST PASSED"
