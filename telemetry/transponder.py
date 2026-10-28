#!/usr/bin/env python3
"""
transponder.py — Telemetry Layer v1.0 Transponder.

Validates an event against Event Bus Payload v1.0, enriches it and appends it
atomically (fcntl lock + flush + fsync) to the event log. No network, no git.

CLI:
    python3 transponder.py '{"event":"STEP_COMPLETED","core_id":"core-001","execution_step":"18/42"}'
Library:
    from telemetry.transponder import emit
    emit({"event": "HEARTBEAT"})

Exit codes: 0 ok, 2 usage, 3 bad JSON, 4 validation, 5 write failure.
"""
import fcntl
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0"

ALLOWED_EVENTS = {
    "CORE_STARTED", "CORE_STOPPED", "HEARTBEAT",
    "STEP_STARTED", "STEP_COMPLETED", "STEP_FAILED",
    "SPAWN_REQUESTED", "SPAWN_COMPLETED", "FATAL_ERROR",
}

# Conditional enforcement (ADR-002): execution events always carry a trace
# context. If the caller does not provide one, a root trace/span is generated.
TRACED_EVENTS = {
    "STEP_STARTED", "STEP_COMPLETED", "STEP_FAILED",
    "SPAWN_REQUESTED", "SPAWN_COMPLETED",
}

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
OBJECT_FIELDS = ("data", "context_diff", "guardrail_state", "metrics")
STRING_FIELDS = ("core_id", "source_system", "execution_step")


class ValidationError(ValueError):
    pass


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_and_enrich(payload: dict, core_id_env: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a JSON object")

    event = payload.get("event")
    if event is None:
        raise ValidationError("missing 'event'")
    if event not in ALLOWED_EVENTS:
        raise ValidationError(f"unknown event type: {event!r}")

    if "core_id" not in payload:
        if not core_id_env:
            raise ValidationError("missing 'core_id' and CORE_ID env is not set")
        payload["core_id"] = core_id_env

    if payload.get("schema", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValidationError(f"unsupported schema: {payload.get('schema')!r}")

    for f in STRING_FIELDS:
        if f in payload and not isinstance(payload[f], str):
            raise ValidationError(f"'{f}' must be a string")
    for f in OBJECT_FIELDS:
        if f in payload and not isinstance(payload[f], dict):
            raise ValidationError(f"'{f}' must be an object")

    if "trace_id" in payload and not TRACE_ID_RE.match(str(payload["trace_id"])):
        raise ValidationError("'trace_id' must be 32 lowercase hex chars")
    if "span_id" in payload and not SPAN_ID_RE.match(str(payload["span_id"])):
        raise ValidationError("'span_id' must be 16 lowercase hex chars")
    psid = payload.get("parent_span_id")
    if psid is not None and not SPAN_ID_RE.match(str(psid)):
        raise ValidationError("'parent_span_id' must be 16 lowercase hex chars or null")

    if event in TRACED_EVENTS:
        payload.setdefault("trace_id", secrets.token_hex(16))
        payload.setdefault("span_id", secrets.token_hex(8))
        payload.setdefault("parent_span_id", None)

    payload["schema"] = SCHEMA_VERSION
    payload.setdefault("event_id", str(uuid.uuid4()))
    payload.setdefault("timestamp", iso_utc_now())
    return payload


def atomic_append(path: str, line: str) -> None:
    """Append one line under an exclusive lock, then flush + fsync."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _log_path() -> str:
    return os.environ.get("EVENT_LOG_PATH", "telemetry/event_stream.jsonl")


def emit(payload: dict, path: str | None = None) -> dict:
    """Library entrypoint. Returns the enriched event (raises ValidationError / OSError)."""
    event = validate_and_enrich(dict(payload), os.environ.get("CORE_ID"))
    atomic_append(path or _log_path(), json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    return event


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: transponder.py '<json-string>'", file=sys.stderr)
        return 2
    try:
        payload = json.loads(argv[1])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        return 3
    try:
        event = validate_and_enrich(payload, os.environ.get("CORE_ID"))
    except ValidationError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        return 4
    try:
        atomic_append(_log_path(), json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    except OSError as e:
        print(f"Append failed: {e}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
