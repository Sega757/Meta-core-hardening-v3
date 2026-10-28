# Execution Plane

The Execution Plane is the only layer in META-CORE with write privileges. It
is a deliberately **tightly coupled** web stack — the one place in the
architecture where we trade isolation for latency, because it has to
guarantee sub-100ms CRUD transactions and every other plane depends on it
for persistence (the Reasoning Plane holds no state of its own).

## Components

| Component | Technology | Role |
|---|---|---|
| API Backend | Node.js v20+ (LTS), Next.js, TypeScript | Runs HTTP endpoints, hosts the AIDL IPC gateway to `agent.py`, orchestrates BullMQ workers |
| Transaction database | SQLite, Write-Ahead Logging (WAL) mode | Persists transactional metadata, queue state, and configuration |
| Deterministic knowledge store | SQLite tables (EGDS), append-only | Stores versioned Knowledge Objects — see [Data Architecture](data-architecture.md) |
| Task queue | Redis + BullMQ | Asynchronous parsing / generation / planning jobs, at-least-once delivery |
| Validation | Zod (TypeScript) | Rigid runtime type-checking of inbound arguments before any DB write |

## Transaction flow

```mermaid
flowchart TD
    A[Client / IPC Gateway] --> B[Enqueue BullMQ task]
    B --> C{Acquire Redis lock\n1-2 tasks / agent}
    C -->|granted| D[Idempotent worker executes]
    C -->|denied| B
    D --> E[Verify capability token,\nCAS on state_version,\nSQLite WAL transaction]
    E --> F[Append event_stream.jsonl\n"clean trail"]
```

## Capability-gated commit

Every mutation that comes from a Decision Packet commits in one transaction:
verify the capability token (signature, expiry, packet, action hash), advance
`state_ledger.state_version` with `WHERE state_version = expected`, run the
mutation, record the `packet_id` in `applied_packets`. A stale version aborts and
sends the agent back to Perceive; a redelivered packet returns
`ALREADY_APPLIED`. Reference implementation:
[`src/execution/state_ledger.py`](../../src/execution/state_ledger.py); design and
limits: [ADR-0016](../decisions/0016-capability-tokens-and-cas.md).

**Transactional outbox.** The same transaction writes the intent to act outside the
database into `execution_outbox` with `idempotency_key = SHA-256(JCS([packet_id,
expected_state_version]))`. A dispatcher drives actuators from `PENDING` rows
(`PENDING → DISPATCHED → ACKED | FAILED`), so an actuator never runs a command that
was not atomically committed, and can deduplicate a redelivery on the key. The
actuator must still check the physical state itself. The executed action hash is
recomputed from `tool_name` and `args`, not taken from the caller.

The Redis path needs the same compare-and-swap and outbox (Lua or `WATCH`/`MULTI`); it
is specified, not implemented.

## Why it's tightly coupled (and why that's safe)

Unlike the Reasoning and Observability planes, the Execution stack is
integrated in-process to eliminate serialization and network hops on the hot
path. This is safe *only* because the plane that could abuse that speed —
the LLM — has no credentials to reach it directly; every inbound mutation
still has to arrive as a validated Decision Packet through the IPC bridge.

## Security properties

- **Prompt-injection air-gap** — database credentials live exclusively in
  the Node.js process; `agent.py` never has them.
- **Idempotency** — BullMQ's at-least-once delivery means workers must be
  idempotent, so a network retry can never double-apply a state change.
  `packet_id` is the idempotency key, enforced by `applied_packets`.
- **No stale approvals** — a mutation applies only if the ledger head still
  equals the version the packet was validated against.
- **Rate-limit defense** — Redis distributed locks cap each agent to 1–2
  concurrent tasks, preventing a runaway loop from exhausting external API
  quotas.
- **Schema gate** — Zod (runtime) and JSON Schema 2020-12 (from the
  Reasoning Plane) both validate before a write is attempted.

## Open questions

- Exact schema/contract of the AIDL IPC bridge between this plane and
  `agent.py`.
- The BullMQ-side wiring of `packet_id` as the deduplication key (the key
  itself is fixed by ADR-0016; the Node.js implementation does not exist yet).
- The Node.js implementation of token verification, RFC 8785 canonicalization and the
  outbox dispatcher (a token test vector is in ADR-0016).
- The dispatcher's lease length and tool registry (which `tool_name`s need an
  external actuator).

See [`docs/open-questions.md`](../open-questions.md) for the full list, and
[ADR-0002](../decisions/0002-execution-plane-exclusive-db-access.md) for the
decision to keep DB credentials exclusively here.
