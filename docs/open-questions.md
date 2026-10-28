# Open Questions

The META-CORE blueprint specifies *what* boundary each component enforces,
but a number of interfaces and thresholds are referenced without a concrete
specification. This page consolidates every such gap in one place so they
can be tracked and closed deliberately, instead of discovered piecemeal
during implementation. Each item links back to the page where it surfaced.

**Status vocabulary.** An item is only called closed when it can be checked.
**OPEN** — nothing decided. **SPECIFIED** — decided in an ADR, no code.
**REFERENCE** — decided, with reference code and passing tests in this
repository, but not wired into a running system and not calibrated on real
traces. A text description alone never moves an item out of OPEN.

## IPC & transport

- **AIDL Gateway ↔ Executor contract** — the interface definition
  (method signatures, data types) bridging the Node.js API Backend and
  `agent.py` is not specified. ([Execution Plane](architecture/execution-plane.md),
  [Reasoning Plane](architecture/reasoning-plane.md))
- **AIDL transport layer** — whether the bridge runs over stdio, Unix domain
  sockets, TCP loopback, or named pipes, and what serializes the payload
  between TypeScript and Python. ([Component Connectivity](architecture/component-connectivity.md))
- **Sidecar transport protocol** — the network/IPC mechanism (HTTP, Unix
  socket, gRPC) carrying Decision Packets from `agent.py` to the Control
  Plane sidecar. ([Control Plane](architecture/control-plane.md))
- **Kill-Switch termination signal** — whether the emergency stop is a
  `SIGKILL`, `SIGTERM`, or a sidecar-issued stop request/webhook.
  ([Control Plane](architecture/control-plane.md))
- **AIDL security handshake** — how the Node.js backend authenticates the
  connecting `agent.py` engine, so an unauthorized process can't spoof it.
  ([Assembly Stages](deployment/assembly-stages.md))

## Safety logic

- **L0–L3 escalation rules** — the concrete evaluation logic and numeric
  thresholds distinguishing the four escalation tiers.
  ([L-E-J-D-A-S framework](security/l-e-j-d-a-s-framework.md))
- **Validation-failure feedback loop** — whether a rejected Decision Packet's
  detail routes back to `agent.py` to trigger an automatic corrective PTAC
  retry, or requires human intervention. ([Reasoning Plane](architecture/reasoning-plane.md))
- **Kill-Switch numeric ceilings** — **OPEN** for execution time and memory.
  For loops, consecutive errors and tokens there are defaults in
  [ADR-0011](decisions/0011-loop-detection-algorithm.md) (**REFERENCE**, not
  calibrated on real traces).
  ([L-E-J-D-A-S framework](security/l-e-j-d-a-s-framework.md))
- **Active loop-detection algorithm** — **REFERENCE**:
  [ADR-0011](decisions/0011-loop-detection-algorithm.md), `LoopCircuitBreaker` in
  `src/control/validator.py`, tests in `tests/test_control_invariants.py`.
  Read-only exploration (pagination) is exempt when the tools are declared read-only by
  the trusted side and no arguments repeat; the false-positive rate on real traces is
  unmeasured, and the read-only registry itself is not implemented.
  ([Control Plane](architecture/control-plane.md))
- **Validation-rejection feedback** — the *content* is fixed (packet id, level,
  coarse reason class; see the reward-hacking section of the
  [L-E-J-D-A-S framework](security/l-e-j-d-a-s-framework.md)); whether it drives
  an automatic retry is **OPEN**.
- **Post-kill state recovery** — rollback protocol for transactions cut off
  mid-execution by an L3 kill. ([Control Plane](architecture/control-plane.md))

## Data & storage

- **EGDS engine** — the underlying software running the Knowledge Object
  store (plain relational tables, `pgvector`, an embedded key-value engine).
  ([Data Architecture](architecture/data-architecture.md))
- **Embedding pipeline** — which model (local or external API) populates the
  `embedding` field on a Knowledge Object. ([Data Architecture](architecture/data-architecture.md))
- **Subject/predicate normalization** — encoding is fixed by JCS; case,
  whitespace and Unicode-normalization policy before computing `fact_key` is
  **OPEN**. ([Data Architecture](architecture/data-architecture.md))
- **No-op re-assertion policy** — an identical re-assertion currently creates a
  new KO version; whether the writer skips it is **OPEN**.
  ([ADR-0013](decisions/0013-provenance-typed-knowledge-objects.md))
- **Re-embedding** — `embedding` is fixed at insert because rows are immutable;
  where updated embeddings live is **OPEN**.
- **Trigger drift** — `ko_verify_integrity` verifies ids inside the database
  (**REFERENCE**, [ADR-0013](decisions/0013-provenance-typed-knowledge-objects.md)), but
  `open_writer` does not detect a trigger that was *modified* rather than dropped, and the
  `json_quote` self-test has only been run on SQLite 3.45.1. **OPEN**.
- **SHA-256 collision handling** — no fallback/resolution framework is
  defined. ([Data Architecture](architecture/data-architecture.md))
- **WAL checkpoint policy** — automated checkpoint thresholds or explicit
  `PRAGMA wal_checkpoint` scheduling to bound `-wal` file growth.
  ([ADR-0004](decisions/0004-sqlite-wal-mode.md))
- **Connection pool sizing** — max concurrent connection limits for the
  SQLite driver inside the Next.js API server. ([ADR-0004](decisions/0004-sqlite-wal-mode.md))

## Validation & schema

- **Zod ↔ JSON Schema drift prevention** — **OPEN** on the Node.js side.
  `schemas/decision_packet.schema.json` is hand-written and tests keep it in
  parity with the Python model; Zod types are not generated from it yet.
  ([ADR-0006](decisions/0006-dual-schema-validation.md))
- **Validation-error reporting pipeline** — the interface routing a Zod or
  JSON Schema validation failure back to the agent for self-correction.
  ([ADR-0006](decisions/0006-dual-schema-validation.md))

## Queueing & concurrency

- **BullMQ idempotency-key generation** — **SPECIFIED**: the key is the
  packet's `packet_id`, enforced in the database by `applied_packets`
  ([ADR-0016](decisions/0016-capability-tokens-and-cas.md)). The BullMQ wiring
  is **OPEN**. ([Execution Plane](architecture/execution-plane.md))
- **Redis lock TTL / expiry policy** — no defined time-to-live for
  distributed agent locks, which risks a deadlock if a worker crashes
  mid-execution. ([ADR-0005](decisions/0005-redis-bullmq-execution-only.md))
- **Dead-letter queue behavior** — recovery procedure when an idempotent
  worker repeatedly fails under the at-least-once delivery guarantee.
  ([ADR-0005](decisions/0005-redis-bullmq-execution-only.md))
- **Concurrency-breach behavior** — what happens to a request that exceeds
  the 1–2 task Redis concurrency threshold (queued vs. rejected).
  ([Component Connectivity](architecture/component-connectivity.md))

## Telemetry & observability

- **Event log rotation/retention** — no truncation or archival policy for
  `event_stream.jsonl`, risking unbounded disk growth.
  ([Observability Plane](architecture/observability-plane.md))
- **Concurrent-writer locking** — the mechanism `transponder.py` uses when
  multiple parallel agent processes append simultaneously.
  ([Observability Plane](architecture/observability-plane.md))
- **Git authentication** — whether `log_sync.sh` uses SSH deploy keys or
  HTTPS tokens against the remote repository, and how credentials are
  provisioned. ([ADR-0001](decisions/0001-decoupled-telemetry-sync.md))
- **Partial-write corruption handling** — behavior if a process terminates
  mid-append to `event_stream.jsonl`. ([Observability Plane](architecture/observability-plane.md))
- **Anomaly-detection thresholds** — **REFERENCE**: `RuntimeAnomalyDetector` in
  `src/observability/observer.py` (Modified Z-score / MAD > 3.5, Huber δ = 1.345,
  window 64, `min_samples` 10, `scale_floor` 1.0), [ADR-0015](decisions/0015-claim-level-trace-evaluation.md).
  Not wired into a running process; every value is a textbook or arbitrary starting
  value, unverified on this workload.
  ([Observability Plane](architecture/observability-plane.md))
- **Trace schema** — span trees are built by `telemetry/observer.py` from
  `trace_id` / `span_id` / `parent_span_id`; the full span schema and the Tier 2
  offline-audit pipeline are **OPEN**.
  ([Observability Plane](architecture/observability-plane.md))

## Deployment

- **`docker-compose.yml` contents** — the path
  `/containerization/docker-compose.yml` is referenced but the file itself
  is not yet written. ([ADR-0007](decisions/0007-multi-container-deployment.md))
- **Sandbox jail policy** — the concrete OS-level mechanism (AppArmor,
  gVisor, or equivalent) enforcing the blocked-socket rule.
  ([Deployment Requirements](deployment/requirements.md))
- **`.env` contract** — `.env.example` now covers the reference core and the telemetry
  layer (`src.config.Settings` fails fast); the variables linking Redis to BullMQ and
  other Stage 1 services are still **OPEN**. ([Deployment Requirements](deployment/requirements.md))
- **Process supervisor** — PM2, a systemd unit, or a container-native
  restart policy for the Node.js API in production.
  ([Deployment Requirements](deployment/requirements.md))
- **Ed25519 key rotation policy** — distribution and lifecycle management of
  the public keys `agent.py` uses to verify command authenticity.
  ([Deployment Requirements](deployment/requirements.md))
- **Package version pinning** — exact `cryptography`/`numpy` version
  requirements are unspecified. ([Deployment Requirements](deployment/requirements.md))
- **Stage 1 test suite** — the concrete commands/parameters for the
  concurrency and write-lock verification tests.
  ([Assembly Stages](deployment/assembly-stages.md))

## Raised by external audits

- **TOCTOU between Control and Execution** — **REFERENCE** for the SQLite path
  ([ADR-0016](decisions/0016-capability-tokens-and-cas.md)); external side
  effects, the Redis path and the Node.js verifier are **OPEN** (below).
- **Stack reconciliation, formal verification scope, and a possible
  "Economics" scorecard field** — still **OPEN**; see
  [Audit 0001](audits/0001-go-grpc-tlaplus-stack-audit.md#open-questions-raised-by-this-audit).

## Reasoning

- **LLM call fault tolerance** — retry/backoff logic for a failed API call
  inside the PTAC cycle. ([Reasoning Plane](architecture/reasoning-plane.md))
- **Context token budgeting** — **REFERENCE**:
  [ADR-0014](decisions/0014-context-budgeting.md), `WaterfillingContextAllocator`
  in `src/reasoning/context_budget.py`. Not wired into `agent.py`; tokenizer
  choice and a counting safety margin are **OPEN**.
  ([Reasoning Plane](architecture/reasoning-plane.md))
- **Intent pre-filter model and threshold** — which classifier and score
  cutoff screens unstructured external content before it enters the
  Think-phase prompt, and whether flagged content is dropped silently or
  logged. ([Reasoning Plane](architecture/reasoning-plane.md))
- **ADK 2.0 binding** — the exact interfaces binding Neocortex actions to
  ADK 2.0 primitives. ([Reasoning Plane](architecture/reasoning-plane.md))
- **Self-correction retry limit** — the maximum number of schema-validation
  retries permitted in Check before the agent raises an operational
  exception. ([Reasoning Plane](architecture/reasoning-plane.md))

## Introduced by ADRs 0011–0016

- **Capability-key provisioning and rotation** — the HMAC key is shared by the
  sidecar and the Execution Plane and must never reach the Reasoning Plane; how
  it is distributed and rotated is **OPEN**.
  ([ADR-0016](decisions/0016-capability-tokens-and-cas.md))
- **External side effects** — the intent is now committed atomically with the CAS in
  `execution_outbox` with an idempotency key (**REFERENCE**, SQLite path,
  [ADR-0016](decisions/0016-capability-tokens-and-cas.md)). Still **OPEN**: the
  actuator-side physical-state check, the dispatcher (lease length, tool registry), and
  whether a real actuator deduplicates on the key. This remains the residual TOCTOU risk.
- **Redis compare-and-swap and outbox** — specified (Lua or `WATCH`/`MULTI`), not
  implemented or tested. **SPECIFIED**.
- **Node.js verifier** — token verification and RFC 8785 canonicalization on the
  Execution Plane; an ADR-0016 test vector exists. **OPEN**.
- **Gateway cache configuration** — semantic cache must be off on packet-producing
  routes ([ADR-0012](decisions/0012-ai-gateway-egress-implementation.md)). The rule is now
  declared in `config/ai_gateway.yaml` and checked by `python3 -m src.config`
  (**REFERENCE** for the declaration). That the manifest is translated into a real
  gateway's settings and enforced on the wire is **OPEN / unverified**.
- **Breaker calibration** — `max_steps`, `max_steps_per_state_version`, cycle repeats and
  bucket sizes are starting values; they need measurement on real traces. **OPEN**.
- **Clock assumptions** — capability expiry assumes the sidecar and the
  Execution Plane share a host clock. **OPEN** for multi-host deployments.
