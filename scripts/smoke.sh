#!/usr/bin/env bash
# WanderChat smoke test for a fresh clone (bash; Git Bash on Windows works too).
# Mirrors scripts/smoke.ps1:
#   .env present -> docker compose up --build -d --wait -> /health -> signup -> login
#   -> /chat -> /chat/stream (done event). PASS/FAIL per step; exit 1 on first failure.
#
# Usage: scripts/smoke.sh [--skip-up] [BASE_URL]      (default http://localhost:8000)
# Needs: docker, curl. No jq/python on the host (JSON fields are read with sed).
set -u

SKIP_UP=0
if [ "${1:-}" = "--skip-up" ]; then SKIP_UP=1; shift; fi
BASE_URL="${1:-http://localhost:8000}"
cd "$(dirname "$0")/.." || exit 1

pass() { printf 'PASS  %-28s %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %-28s %s\n' "$1" "$2"; exit 1; }
# Extract a top-level string/number field from a flat JSON object (our responses are flat).
field() { printf '%s' "$1" | sed -n "s/.*\"$2\":[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"; }
# POST JSON; prints body, then HTTP status on the last line.
post() { curl -s --max-time 60 -w '\n%{http_code}' -X POST "$BASE_URL$1" -H 'Content-Type: application/json' "${@:3}" -d "$2"; }
split() { BODY=$(printf '%s' "$1" | sed '$d'); CODE=$(printf '%s' "$1" | tail -n1); }

[ -f .env ] || fail ".env present" "missing .env: cp .env.example .env and set JWT_SECRET (see backend/README.md)"
pass ".env present" ".env found"

if [ "$SKIP_UP" -eq 0 ]; then
  start=$(date +%s)
  out=$(docker compose up --build -d --wait 2>&1) || fail "docker compose up --wait" "$(printf '%s' "$out" | tail -n 15)"
  pass "docker compose up --wait" "healthy after $(( $(date +%s) - start ))s"
fi

health=$(curl -s --max-time 10 "$BASE_URL/health") || fail "GET /health" "cannot connect to $BASE_URL"
[ "$(field "$health" status)" = "ok" ] && [ "$(field "$health" mongo)" = "up" ] || fail "GET /health" "$health"
pass "GET /health" "status=ok mongo=up"

rand=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')
email="smoke-$rand@example.com"
creds="{\"email\": \"$email\", \"password\": \"smoke test password 123\"}"
thread="smoke-$rand"

split "$(post /auth/signup "$creds")"
[ "$CODE" = "201" ] || fail "POST /auth/signup" "HTTP $CODE $BODY"
pass "POST /auth/signup" "id=$(field "$BODY" id)"

split "$(post /auth/login "$creds")"
[ "$CODE" = "200" ] || fail "POST /auth/login" "HTTP $CODE $BODY"
access=$(field "$BODY" access_token)
[ -n "$access" ] || fail "POST /auth/login" "no access_token"
pass "POST /auth/login" "expires_in=$(field "$BODY" expires_in)"

split "$(post /chat "{\"message\": \"Plan 3 days in Lisbon\", \"thread_id\": \"$thread\"}" -H "Authorization: Bearer $access")"
[ "$CODE" = "200" ] && [ "$(field "$BODY" thread_id)" = "$thread" ] || fail "POST /chat" "HTTP $CODE $BODY"
pass "POST /chat" "thread_id=$thread"

stream=$(curl -s -N --max-time 60 -X POST "$BASE_URL/chat/stream" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $access" -d "{\"message\": \"And day 4?\", \"thread_id\": \"$thread\"}") \
  || fail "POST /chat/stream" "curl failed"
tokens=$(printf '%s\n' "$stream" | grep -c '^event: token$')
printf '%s\n' "$stream" | grep -q '^event: error$' && fail "POST /chat/stream" "stream ended with an error event"
printf '%s\n' "$stream" | grep -q '^event: done$' || fail "POST /chat/stream" "no done event: $(printf '%s' "$stream" | tail -n 4)"
pass "POST /chat/stream" "$tokens token events, then done"

echo "SMOKE: ALL PASS"
