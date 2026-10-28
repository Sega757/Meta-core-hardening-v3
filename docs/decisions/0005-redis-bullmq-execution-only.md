# ADR-0005: Redis/BullMQ access restricted to the Execution Plane

**Status:** Accepted

## Context

Letting the Reasoning Plane enqueue and manage BullMQ jobs directly would
simplify dynamic multi-agent task routing. But it would also let a
compromised agent loop spam task creation, exhausting host resources and
external API quotas with no gate in between.

## Decision

Redis and BullMQ are reachable only from the Node.js Execution Plane. Any
task the Reasoning Plane wants performed must arrive as a Decision Packet
over the AIDL IPC bridge and be translated into a queue operation by the
Execution Plane — never enqueued by the agent directly.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Direct Redis/BullMQ access from `agent.py` | Simpler multi-agent task routing, but a compromised loop can spam task creation without limit |
| **Execution-Plane-mediated queueing (chosen)** | Adds an IPC translation step, but keeps the queue infrastructure behind schema validation and Redis concurrency locks |

## Consequences

- Distributed Redis locks (1–2 concurrent tasks per agent) are enforceable
  at a single choke point instead of needing to be trusted client-side.
- The agent can only ever cause work to happen by producing a verified
  Decision Packet.
- Lock expiry/TTL policy for those distributed locks is not yet specified —
  see [`docs/open-questions.md`](../open-questions.md).

See [Execution Plane](../architecture/execution-plane.md).
