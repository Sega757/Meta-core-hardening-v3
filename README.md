# META-CORE — Telemetry Layer v1.0 + Executable Reference Core

Portable agent-engine core. The repo is a hybrid: design documents under `docs/`
plus code that is **working and tested** today:

1. the decoupled **telemetry bus** that every node writes to (`telemetry/`), and
2. an executable **reference core** for the safety path (`src/`): RFC 8785
   canonicalization, the loop/kill-switch breaker, capability tokens, the
   append-only Knowledge Object schema with in-database hash verification, the
   compare-and-swap commit with a transactional outbox, the waterfilling context
   allocator, Tier 1 observability (span trees, robust anomaly detector) and
   fail-fast configuration.

The reference core is not wired into a running agent; it is the executable
specification of ADRs 0011–0016. The rest of the design (Flow-Core, Chronos V3,
the Node.js Execution Plane) is specified in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and tracked as roadmap.

## Core invariant

> A core never reports its state to a specific recipient. It publishes a fact via
> the Transponder. Anyone who needs state reads the event stream.

```
agent / ADK node ──► transponder.py ──(flock+fsync append)──► event_stream.jsonl
                                                                   │
                         observer.py (read-only) ◄─────────────────┤
                         rotate.sh  (100 MB → archive/*.gz) ◄──────┤
                         log_sync.sh (git batch push, 5 min) ◄─────┘
```

## Layout

| Path | Purpose |
|---|---|
| `telemetry/transponder.py` | Validates event (schema v1.0, 9-event ENUM), enriches, atomic append. Zero network/git. CLI + `emit()` API. |
| `telemetry/observer.py` | Passive parser: trace trees, handoff depth, cycles, orphan spawns, stale cores. Exit 0/1/2 = OK/WARN/CRITICAL. |
| `telemetry/log_sync.sh` | Batch `git commit/push` only if changes exist; flock; exponential backoff; errors → `log_sync.err`. |
| `telemetry/rotate.sh` | Copy+truncate rotation under the writer lock, gzip into `archive/`. |
| `schemas/event_bus_payload.v1.json` | JSON Schema 2020-12 contract. |
| `deploy/systemd/` | Timers for sync (5 min) and observer (1 min). |
| `examples/demo.py` | Two-core handoff + spawn simulation. |
| `schemas/decision_packet.schema.json` | JSON Schema 2020-12 for the Decision Packet (attestation and capability token are null until the Control Plane fills them). |
| `src/control/validator.py` | RFC 8785 (JCS) + SHA-256 hashes, `LoopCircuitBreaker` (ADR-0011), HMAC capability tokens (ADR-0016), pydantic packet models. |
| `src/data/schema.sql`, `src/data/store.py` | Append-only versioned Knowledge Objects, trusted views, state ledger (ADR-0013); writer, restricted reader, id audit. |
| `src/execution/state_ledger.py` | Token verification + compare-and-swap commit with replay protection and an atomic `execution_outbox` intent (ADR-0016). SQLite path only. |
| `src/observability/observer.py` | `TraceDAG` (span trees, durable writes via the transponder), `RuntimeAnomalyDetector` (Modified Z-score/MAD + Huber, cold start), `Tier1Observer` (ADR-0015). |
| `src/config.py`, `.env.example`, `config/ai_gateway.yaml` | Fail-fast settings (the HMAC secret is empty on purpose), restricted-YAML parser, and the ADR-0012 gateway manifest check: `make check-config`. |
| `src/reasoning/context_budget.py` | Waterfilling context allocator with an inelastic reserve (ADR-0014). |
| `tests/` | stdlib `unittest`, incl. 8-process concurrent write test, TOCTOU race, DDL trigger tests. |

## Quick start

```bash
make install  # pydantic>=2.6, needed only for the src/ reference core
make test     # 164 tests (14 telemetry + 150 reference core)
make check-config   # validates config/ai_gateway.yaml (ADR-0012 rules)
cp .env.example .env   # then set META_CORE_HMAC_SECRET (see the file)
make demo     # writes demo events, runs observer

export CORE_ID=core-001 EVENT_LOG_PATH=telemetry/event_stream.jsonl
python3 telemetry/transponder.py '{"event":"STEP_COMPLETED","execution_step":"18/42","data":{"status":"success"}}'
python3 telemetry/observer.py --json
```

From Python:

```python
from telemetry.transponder import emit
emit({"event": "STEP_STARTED", "core_id": "core-001", "trace_id": tid, "span_id": sid, "parent_span_id": parent})
```

Requirements: the telemetry layer needs Python 3.10+ (stdlib only), bash, git,
util-linux (`flock`), Linux/macOS (uses `fcntl`). The reference core under `src/`
needs Python 3.12, SQLite ≥ 3.35 and `pydantic>=2.6` (`requirements.txt`);
`jsonschema` and `PyYAML` are optional (four cross-check tests skip without them).

## Event contract (v1.0)

Required: `schema`, `event_id`, `core_id`, `event`, `timestamp`.
Events: `CORE_STARTED CORE_STOPPED HEARTBEAT STEP_STARTED STEP_COMPLETED STEP_FAILED SPAWN_REQUESTED SPAWN_COMPLETED FATAL_ERROR`.
Execution events (`STEP_*`, `SPAWN_*`) always carry `trace_id`/`span_id` — generated as a root span if the caller omits them.
Optional objects: `data`, `context_diff`, `guardrail_state`, `metrics`.

## Deploy (systemd)

```bash
sudo cp -r . /opt/meta-core && sudo useradd -r metacore && sudo chown -R metacore /opt/meta-core
sudo cp deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now log-sync.timer observer.timer
```

Wire `observer.service` exit code 2 to your kill switch (`OnFailure=`).

## Decisions

Architecture decisions (ADR-0001 … ADR-0016) live in
[`docs/decisions/`](docs/decisions/README.md); open questions with explicit
status (OPEN / SPECIFIED / REFERENCE) in [`docs/open-questions.md`](docs/open-questions.md).
The three notes below are the telemetry layer's own early decisions, numbered
separately:

- **ADR-001** — No git in the Transponder. Push is batched by `log_sync.sh` (rate limits, latency, lock contention).
- **ADR-002** — `trace_id` conditional: optional for lifecycle/heartbeat, auto-generated for execution events.
- **ADR-003** — Rotation is copy+truncate under the writer's flock (rename could lose in-flight appends).

## License

TBD
