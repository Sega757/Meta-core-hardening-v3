#!/usr/bin/env python3
"""
observer.py — passive reader of the Telemetry Layer v1.0 event log.

Blind-parses event_stream.jsonl (read-only), rebuilds trace trees from
trace_id / span_id / parent_span_id and reports:
  * per-trace span count + max depth
  * handoff-depth violations (depth > MAX_HANDOFF_DEPTH)      -> CRITICAL
  * parent_span_id cycles                                      -> CRITICAL
  * FATAL_ERROR events                                         -> CRITICAL
  * orphan spawns (SPAWN_REQUESTED without SPAWN_COMPLETED)    -> WARN
  * STEP_FAILED events                                         -> WARN
  * stale cores (silent > HEARTBEAT_TIMEOUT_S, not stopped)    -> WARN
  * malformed lines / unsupported schema versions (skipped)    -> WARN

Exit codes: 0 OK, 1 WARN, 2 CRITICAL — wire exit 2 to the Kill-Switch.

Usage:
    python3 observer.py [path] [--json]
Env: EVENT_LOG_PATH, MAX_HANDOFF_DEPTH (default 5), HEARTBEAT_TIMEOUT_S (default 300)
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

SUPPORTED_SCHEMA = "1.0"


def parse_ts(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def read_events(path: str) -> tuple[list[dict], list[str]]:
    events, warnings = [], []
    if not os.path.exists(path):
        return events, [f"log not found: {path}"]
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"line {n}: malformed JSON, skipped")
                continue
            if not isinstance(ev, dict) or ev.get("schema") != SUPPORTED_SCHEMA:
                warnings.append(f"line {n}: unsupported schema, skipped")
                continue
            events.append(ev)
    return events, warnings


def span_depths(parents: dict[str, str | None]) -> tuple[dict[str, int], list[str]]:
    """parents: span_id -> parent_span_id. Root depth = 0.
    A parent not present in the trace is treated as an external root (depth 0 for child).
    Returns (depths, spans_on_cycles)."""
    depths: dict[str, int] = {}
    cyclic: set[str] = set()
    for start in parents:
        chain, seen, cur = [], set(), start
        while cur in parents and cur not in depths:
            if cur in seen:
                cyclic.update(chain[chain.index(cur):])
                break
            seen.add(cur)
            chain.append(cur)
            cur = parents[cur]
        base = depths[cur] + 1 if cur in depths else 0
        for i, s in enumerate(reversed(chain)):
            if s in cyclic:
                depths[s] = -1
            else:
                depths.setdefault(s, base + i)
    return depths, sorted(cyclic)


def analyze(events: list[dict], max_depth: int = 5, hb_timeout: float = 300,
            now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    traces: dict[str, dict[str, str | None]] = defaultdict(dict)
    last_seen: dict[str, datetime] = {}
    stopped: set[str] = set()
    spawn_requests: dict[str, dict] = {}
    spawn_done: set[str] = set()
    failures: list[dict] = []

    for ev in events:
        core, kind = ev.get("core_id"), ev.get("event")
        ts = parse_ts(ev.get("timestamp"))
        if core and ts and (core not in last_seen or ts > last_seen[core]):
            last_seen[core] = ts
        if kind == "CORE_STOPPED":
            stopped.add(core)
        elif kind == "CORE_STARTED":
            stopped.discard(core)

        tid, sid = ev.get("trace_id"), ev.get("span_id")
        if tid and sid:
            traces[tid].setdefault(sid, ev.get("parent_span_id"))

        if kind == "SPAWN_REQUESTED" and sid:
            spawn_requests[sid] = ev
        elif kind == "SPAWN_COMPLETED":
            # Completion may reuse the request span or be its child.
            spawn_done.update(x for x in (sid, ev.get("parent_span_id")) if x)
        elif kind in ("STEP_FAILED", "FATAL_ERROR"):
            failures.append({"event": kind, "core_id": core, "trace_id": tid,
                             "execution_step": ev.get("execution_step"),
                             "event_id": ev.get("event_id")})

    trace_report, critical, warnings = {}, [], []
    for tid, parents in traces.items():
        depths, cyclic = span_depths(parents)
        mx = max(depths.values(), default=0)
        trace_report[tid] = {"spans": len(parents), "max_depth": mx, "cyclic_spans": cyclic}
        if cyclic:
            critical.append(f"trace {tid}: parent_span_id cycle {cyclic}")
        if mx > max_depth:
            critical.append(f"trace {tid}: handoff depth {mx} > {max_depth}")

    orphans = sorted(s for s in spawn_requests if s not in spawn_done)
    if orphans:
        warnings.append(f"{len(orphans)} orphan spawn(s): {orphans}")
    for f in failures:
        msg = f"{f['event']} core={f['core_id']} step={f['execution_step']}"
        (critical if f["event"] == "FATAL_ERROR" else warnings).append(msg)
    stale = sorted(c for c, t in last_seen.items()
                   if c not in stopped and (now - t).total_seconds() > hb_timeout)
    if stale:
        warnings.append(f"stale cores (> {hb_timeout:.0f}s silent): {stale}")

    return {
        "events": len(events),
        "cores": sorted(last_seen),
        "traces": trace_report,
        "orphan_spawns": orphans,
        "failures": failures,
        "stale_cores": stale,
        "critical": critical,
        "warnings": warnings,
        "status": "CRITICAL" if critical else ("WARN" if warnings else "OK"),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.environ.get("EVENT_LOG_PATH", "telemetry/event_stream.jsonl")
    events, parse_warnings = read_events(path)
    report = analyze(events,
                     max_depth=int(os.environ.get("MAX_HANDOFF_DEPTH", "5")),
                     hb_timeout=float(os.environ.get("HEARTBEAT_TIMEOUT_S", "300")))
    report["warnings"] = parse_warnings + report["warnings"]
    if report["status"] == "OK" and parse_warnings:
        report["status"] = "WARN"

    if "--json" in argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"[{report['status']}] events={report['events']} "
              f"cores={len(report['cores'])} traces={len(report['traces'])}")
        for c in report["critical"]:
            print(f"  CRITICAL: {c}")
        for w in report["warnings"]:
            print(f"  warn: {w}")
    return {"OK": 0, "WARN": 1, "CRITICAL": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
