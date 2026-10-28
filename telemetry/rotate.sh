#!/usr/bin/env bash
# rotate.sh — rotate event_stream.jsonl at a size threshold, gzip into archive/.
# Copy+truncate under the same flock transponder.py uses, so no event is lost.
set -euo pipefail

TELEMETRY_DIR="${TELEMETRY_DIR_ABS:-$(cd "$(dirname "$0")" && pwd)}"
LOG="${EVENT_LOG_PATH:-${TELEMETRY_DIR}/event_stream.jsonl}"
MAX_BYTES="${ROTATE_MAX_BYTES:-104857600}"   # 100 MB
ARCHIVE="$(dirname "$LOG")/archive"

[[ -f "$LOG" ]] || exit 0
size=$(stat -c %s "$LOG")
(( size < MAX_BYTES )) && exit 0

mkdir -p "$ARCHIVE"
target="${ARCHIVE}/$(basename "$LOG").$(date -u +%Y%m%d_%H%M%S)"

exec 8>>"$LOG"
flock -x 8
cp "$LOG" "$target"
: > "$LOG"
flock -u 8
exec 8>&-

gzip -9 "$target"
echo "rotated ${size} bytes -> ${target}.gz"
