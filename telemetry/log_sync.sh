#!/usr/bin/env bash
# log_sync.sh — batch-commit telemetry to git (run by systemd timer / cron).
# Never touches running agents. On push failure the local log is kept, the
# error goes to log_sync.err, and next runs back off exponentially.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
TELEMETRY_DIR="${TELEMETRY_DIR:-telemetry}"
ERROR_LOG="${REPO_DIR}/${TELEMETRY_DIR}/log_sync.err"
BACKOFF_FILE="${REPO_DIR}/${TELEMETRY_DIR}/.log_sync.backoff"
LOCK_FILE="${REPO_DIR}/${TELEMETRY_DIR}/.log_sync.lock"
MAX_SKIP="${MAX_SKIP:-12}"   # max skipped runs (12 x 5 min = 1 h)

cd "$REPO_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "log_sync already running"; exit 0; }

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

failures=0; skip=0
[[ -f "$BACKOFF_FILE" ]] && read -r failures skip < "$BACKOFF_FILE" || true
if (( skip > 0 )); then
    echo "$failures $((skip - 1))" > "$BACKOFF_FILE"
    echo "backoff: skipping run ($skip left)"
    exit 0
fi

shopt -s nullglob
PATHS=("${TELEMETRY_DIR}"/*.jsonl)
[[ -d "${TELEMETRY_DIR}/archive" ]] && PATHS+=("${TELEMETRY_DIR}/archive")
if (( ${#PATHS[@]} == 0 )) || [[ -z "$(git status --porcelain -- "${PATHS[@]}")" ]]; then
    echo "no telemetry changes"
    rm -f "$BACKOFF_FILE"
    exit 0
fi

git add -- "${PATHS[@]}"
git diff --cached --quiet || git commit -q -m "telemetry: batch sync $(ts)"

if git push -q; then
    rm -f "$BACKOFF_FILE"
    echo "$(ts) pushed"
else
    failures=$((failures + 1))
    skip=$(( 1 << (failures - 1 > 4 ? 4 : failures - 1) ))
    (( skip > MAX_SKIP )) && skip=$MAX_SKIP
    echo "$failures $skip" > "$BACKOFF_FILE"
    echo "$(ts) git push failed (attempt $failures, skipping next $skip runs)" >> "$ERROR_LOG"
    exit 1
fi
