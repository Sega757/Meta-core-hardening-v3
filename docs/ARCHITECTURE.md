# META-CORE Architecture — status map

Legend: ✅ implemented & tested in this repo · 📐 specified, not implemented · ⚠️ needs verification before building

## Three-plane split
| Plane | Components | Status |
|---|---|---|
| Telemetry | transponder, event log, observer, log_sync, rotation | ✅ + reference Tier 1 (`src/observability/observer.py`: span trees, robust anomaly detector) |
| Reasoning | PTAC loop (Perceive-Think-Act-Check), Decision Packet JSON output | 📐 loop; ✅ reference: Decision Packet schema (`schemas/`), context allocator (`src/reasoning/`) |
| Control | validator (Decision Packet risk L0–L3, L-E-J-D-A-S filter), kill switch sidecar | ✅ reference: loop breaker + capability tokens (`src/control/validator.py`); 📐 L-E-J-D-A-S rules, sidecar transport — observer exit code 2 is the trigger hook |
| Execution | Node.js API, SQLite WAL, BullMQ/Redis | ✅ reference: schema + CAS commit in Python (`src/data/`, `src/execution/`); 📐 the Node.js/BullMQ/Redis implementation |

## Flow-Core (claims FSM, 12 states)
- A: RECEIVED → INTENT_EXTRACTED → TYPE_CLASSIFIED → EMOTION_ANALYZED
- B: ROUTED → CONTEXT_LINKED → ACTION_PLANNED → ACTION_EXECUTED → SLA_MONITORING
- C: RESOLVED / ESCALATED → LEGAL_PROCEEDING → ARCHIVED
- SLA timers: T_ACK 24h, T_WARN 50%, T_ESCALATE 80%, T_BREACH 100%.
- Status: 📐. Open ADR: SCXML FSM inside nodes vs Petri nets for multi-agent routing (hybrid proposed).
- ⚠️ Legal constants (e.g. "14 days" for Finnish KSL, business-day rules) must be checked against the actual law / a lawyer — the source notes present them as fact without citations.

## Knowledge Objects
`KO = (ko_id, fact_key, parent_ko_id, subject, predicate, object, embedding, provenance, created_epoch)`; `fact_key = SHA-256(JCS([subject, predicate]))`, `ko_id` covers content, provenance, parent and time ([ADR-0013](decisions/0013-provenance-typed-knowledge-objects.md)).
Status: ✅ reference (JCS, append-only schema, trusted reader, id audit — `src/data/`, `src/control/validator.py`). Ed25519 signing of KOs: 📐 (needs `cryptography`).

## Chronos V3 (System 0/1/2 metacognition)
Entropy-triggered pause → external data fetch (`chronos_nexus`) → Huber-loss denoise (`chronos_logos`) → KO.
Status: 📐. Overlaps with the separate Chronos research agent project.

## Roadmap (suggested order)
1. `ko/` — ✅ builder + JCS + tests done as `src/data/store.py` and `src/control/validator.py`; Ed25519 signer still 📐.
2. `control_plane/validator.py` — Decision Packet schema ✅ (`schemas/decision_packet.schema.json`); L0–L3 gating rules 📐 (open).
3. `control_plane/kill_switch.py` — admission control ✅ (`LoopCircuitBreaker`); consuming observer CRITICAL and stopping the container 📐.
4. `cognitive_core/agent.py` — PTAC loop emitting telemetry via `emit()`.
5. Flow-Core FSM (pure Python state table first; SCXML later if needed).
