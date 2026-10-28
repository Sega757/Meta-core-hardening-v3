# ADR-0002: Execution Plane holds exclusive database write credentials

**Status:** Accepted

## Context

Granting the Reasoning Plane (`agent.py`) direct database write access would
remove a serialization hop and reduce IPC overhead. But the Reasoning Plane
runs untrusted LLM-driven logic — a successful prompt-injection attack
against a directly-connected agent could execute arbitrary writes, including
schema-destructive ones.

## Decision

Database credentials (SQLite, Redis) live exclusively inside the Node.js
Execution Plane process. The Reasoning Plane never receives them. All state
changes from the Reasoning Plane must arrive as a JSON Schema
2020-12-validated Decision Packet, routed through the Control Plane
validator, and applied by the Execution Plane on the agent's behalf.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Direct DB write access from the Reasoning sandbox | Lower architectural latency, but a prompt-injection compromise can execute arbitrary SQL / state mutation |
| **Schema-gated, credential-isolated write path (chosen)** | Adds a JSON-Schema/IPC serialization step, but a compromised sandbox has no channel to mutate state directly |

## Consequences

- Every state mutation carries IPC and validation overhead the direct-write
  alternative would not have.
- The Reasoning Plane cannot corrupt the database even under full
  compromise — the worst case is a rejected or escalated Decision Packet.
- Zod (Execution side) and JSON Schema 2020-12 (Reasoning side) must stay in
  sync — see [ADR-0006](0006-dual-schema-validation.md).

See [Execution Plane](../architecture/execution-plane.md) and
[Reasoning Plane](../architecture/reasoning-plane.md).
