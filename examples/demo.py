#!/usr/bin/env python3
"""Simulates two cores: A runs steps and hands off to B; B spawns a child."""
import os, secrets, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from telemetry.transponder import emit

trace = secrets.token_hex(16)
root = secrets.token_hex(8)

emit({"event": "CORE_STARTED", "core_id": "core-google-a"})
emit({"event": "CORE_STARTED", "core_id": "core-anthropic-b"})
emit({"event": "STEP_STARTED", "core_id": "core-google-a", "trace_id": trace,
      "span_id": root, "parent_span_id": None, "execution_step": "1/3"})
handoff = secrets.token_hex(8)
emit({"event": "STEP_COMPLETED", "core_id": "core-anthropic-b", "trace_id": trace,
      "span_id": handoff, "parent_span_id": root, "execution_step": "2/3",
      "context_diff": {"added": ["goal"], "dropped": []},
      "metrics": {"prompt_tokens": 812, "completion_tokens": 120}})
spawn = secrets.token_hex(8)
emit({"event": "SPAWN_REQUESTED", "core_id": "core-anthropic-b", "trace_id": trace,
      "span_id": spawn, "parent_span_id": handoff, "data": {"child_dna": "manifest.json"}})
emit({"event": "SPAWN_COMPLETED", "core_id": "core-child-c", "trace_id": trace,
      "span_id": secrets.token_hex(8), "parent_span_id": spawn})
emit({"event": "HEARTBEAT", "core_id": "core-child-c"})
print("demo events written to", os.environ.get("EVENT_LOG_PATH"))
