# The Anatomy of a Safe Agent: Understanding Operational Planes

In industrial AI deployment, safety is not an elective feature; it is an
architectural requirement. META-CORE enforces a rigorous physical decoupling
of control logic from the state-mutation engine. This "Operational Planes"
model ensures that even a highly capable reasoning engine is structurally
prohibited from executing unauthorized or unvetted actions.

By separating the system into distinct planes, we move away from
traditional "black box" agents and toward deterministic, verifiable AI
systems.

## Why planes matter

The fundamental philosophy of META-CORE is the strict separation of
concerns. We achieve system integrity by ensuring the plane that "thinks"
(Reasoning) is physically unable to communicate with the environment without
passing through a "gatekeeper" (Control) that finally signals the "hands"
(Execution).

This architecture mitigates three primary industrial risks:

1. **Single points of failure** — architectural decoupling ensures that a
   crash in the LLM-driven reasoning engine does not cascade into the
   transactional database.
2. **Prompt injection** — even if a malicious prompt "hijacks" the Reasoning
   Plane, the engine lacks the network sockets and database credentials
   required to exfiltrate data or mutate state.
3. **Unauthorized data mutation** — every state change must be serialized
   into a schema-validated packet, making "hallucinated" database writes
   impossible.

> **Concept spotlight**
> - **Reasoning Plane** — an isolated sandbox for cognitive processing and
>   planning.
> - **Control Plane** — an out-of-process sidecar that validates decisions
>   against safety and financial bounds.
> - **Execution Plane** — a high-performance, tightly coupled stack for
>   state persistence and API orchestration.
> - **Observability Plane** — a passive, append-only telemetry layer
>   providing a "Clean Trail" of all events.

## The Reasoning Plane: the isolated brain

The Neocortex Planning Engine is the core of the Reasoning Plane. It runs on
Python 3.12, a version mandated specifically for its native `timezone.utc`
handling and the `cryptography` primitives required for Ed25519 signature
verification of incoming commands.

To maintain a zero-trust environment, this plane is subject to two critical
constraints: a **network air-gap** (blocked raw sockets — it cannot reach
the open internet directly) and **credential isolation** (zero database
credentials or API keys for the Execution Plane).

| Stage | Action | Output |
|---|---|---|
| Perceive | Ingest `event_stream.jsonl` to understand current environmental context | Contextual snapshot |
| Think | Execute LLM-driven reasoning inside the isolated sandbox | Logical strategy |
| Act | Compile the strategy into strict JSON Schema 2020-12 format | Decision Packet |
| Check | Hand off the Decision Packet to the Control Plane for safety verification | Approval / rejection signal |

## The Control Plane: the safety gatekeeper

The Control Plane lives in an isolated sidecar container. This physical
separation is vital: if the Reasoning Plane is compromised, it cannot reach
into the sidecar to "turn off its own brakes."

The **Validator** evaluates every Decision Packet against the
[L-E-J-D-A-S](../security/l-e-j-d-a-s-framework.md) framework, assigning an
L0–L3 escalation level. The **AI Kill-Switch** separately monitors *how* the
agent is behaving — execution time, memory footprint, token count, and
cyclic loops — and executes an emergency stop if any bound is breached.

## The Execution Plane: the hands of the system

The Execution Plane is the only layer with write privileges, built on a
high-performance Node.js v20+ (LTS) stack targeting sub-100ms CRUD latency.
It is deliberately "tightly coupled" to eliminate transactional lag —
SQLite runs in WAL mode so it can write at speed while the Observability
Plane reads in parallel, and BullMQ workers are idempotent so an "at least
once" delivery retry never corrupts state.

## The Event Stream: building the "Clean Trail"

The Observability Plane captures every system heartbeat in
`event_stream.jsonl`, following the "Clean Trail" principle — an
append-only, immutable record of every perception, thought, and action.
`transponder.py` performs an atomic `flush + os.fsync` for every log entry,
forcing the OS to commit to physical disk immediately, so the trail survives
even a hard crash.

There is a fundamental conflict between high-frequency logging and remote
Git synchronization latency. META-CORE accepts a 5-minute synchronization
gap: telemetry is written locally in real time and pushed to the remote Git
repository in batches, via a systemd timer.

```
[Reasoning/Execution] -> transponder.py (os.fsync) -> event_stream.jsonl -> log_sync.sh (5-min timer) -> Remote Git
```

## The power of determinism: Knowledge Objects

Hallucinations are mitigated through deterministic data structures. Facts
are handled as Knowledge Objects:
a versioned tuple (see [Data Architecture](../architecture/data-architecture.md)).

To prevent "semantic duplication" — the pollution of the database with
redundant or conflicting facts — every fact has a logical key computed
mathematically: `fact_key = SHA-256(JCS([subject, predicate]))`. If the agent
tries to "re-learn" an existing relationship, the key is identical, so the write
can only become a new *version* of that same fact, never a second copy. Each
version's `ko_id` also covers its provenance, so facts written by the agent can
be told apart from verified ones. The agent's memory stays clean, unique, and
verifiable.

## Key takeaway

META-CORE represents a shift toward industrial-grade AI safety through three
pillars:

- **Isolation** — Python 3.12 sandboxes with zero database credentials.
- **Validation** — out-of-process sidecars using the L-E-J-D-A-S framework.
- **Determinism** — SQLite WAL for performance, SHA-256 for fact integrity.

By enforcing these boundaries, agents can perform complex reasoning without
compromising the stability or security of the underlying infrastructure.
