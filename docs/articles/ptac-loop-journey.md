# The Journey of a Safe Decision: Understanding the PTAC Loop

## The big picture

In industrial AI architecture, autonomy must be bounded by deterministic
constraints. META-CORE ensures the "brain" of the system never has the
direct "hands" required to modify the physical environment without
oversight. Three operational planes enforce a zero-trust security model:

| Plane | Tech stack | Role & safety boundary |
|---|---|---|
| Execution | Node.js v20+ (LTS), Next.js, SQLite (WAL), Redis, BullMQ | **The hands.** Manages all state mutations and database credentials, physically isolated from the reasoning sandbox to prevent prompt-injection "write" attacks. |
| Reasoning | Python 3.12, `agent.py`, ADK 2.0 | **The brain.** Orchestrates the PTAC planning cycle in a compute-isolated environment with blocked network sockets and zero access to database keys. |
| Control | Python, `validator.py`, sidecar container | **The guardian.** An air-gapped sidecar that intercepts and validates every decision, remaining physically separate so a compromised Reasoning Plane cannot disable its own safety rules. |

Without this separation, an LLM subject to a prompt-injection attack could
directly execute a destructive command or exfiltrate sensitive keys. In
META-CORE, the agent can only *propose* an action — it possesses no inherent
authority to execute one.

This architectural layout is the track upon which the agent's internal
cycle runs.

## Introducing the PTAC cycle

The Perceive-Think-Act-Check (PTAC) loop is the heartbeat of the Neocortex
Engine. While LLMs are probabilistic, PTAC is a rigid, deterministic
container that forces AI reasoning into a predictable, machine-readable, and
— most importantly — safe workflow.

## Step 1: PERCEIVE — reading the Clean Trail

The cycle begins with observation. Rather than querying a live, mutable
database — which introduces race conditions and security risk — the agent
reads from `event_stream.jsonl`.

This is the "Clean Trail" principle: an append-only log gives the agent a
perfectly preserved historical context. The architectural trade-off: to
maintain sub-100ms local performance, there's a 5-minute synchronization gap
(eventual consistency) for *remote* telemetry, keeping the system responsive
while still delivering a full audit trail to remote observers over time.

`transponder.py` uses `flush` and `os.fsync` at the OS level to guarantee
every event is physically committed to disk, preventing data loss even
during sudden power failures.

## Step 2: THINK — reasoning in the sandbox

Once the agent has context, it moves into the Reasoning Plane — not a
standard execution environment, but a zero-trust sandbox designed to
neutralize threats:

1. **Blocked network sockets** — the agent is physically incapable of
   reaching the external internet to exfiltrate data.
2. **Zero database credentials** — the sandbox holds no keys or connection
   strings for SQLite or Redis.
3. **Strict Python 3.12** — required for native `timezone.utc` handling and
   the `cryptography` primitives behind Ed25519 signature verification.

Within this sandbox, facts are managed as Knowledge Objects. Every fact has a
deterministic logical key (`fact_key`, a SHA-256 over its subject and predicate),
and every version of it a content-addressed `ko_id` that includes its provenance,
keeping the agent's internal world-model semantically consistent and
mathematically anchored.

## Step 3: ACT — compiling the Decision Packet

In the Act phase, the agent does not perform a physical action — it
translates internal reasoning into a Decision Packet, a strictly formatted
JSON object conforming to JSON Schema 2020-12:

```json
{
  "action": "inventory_correction",
  "parameters": {
    "sku": "META-001",
    "adjustment": -10
  },
  "justification": "Correcting discrepancy noted in event log #882; actual count 40 vs system count 50."
}
```

This step bridges probabilistic AI reasoning and deterministic machine
execution. By forcing the AI's "thoughts" into a strict schema, we make them
verifiable — if the model hallucinates a non-existent parameter, the Check
phase catches it instantly.

## Step 4: CHECK — the ultimate safety gate

This is the climax of the loop, running in an isolated sidecar container so
a compromised agent can't simply patch the validator's memory to force
"success."

**The Validator (L-E-J-D-A-S)** evaluates the Decision Packet against six
dimensions — Legal, Ethical, Jurisdictional, Duty, Accountability, Safety —
and assigns an escalation level:

| Level | Status | Consequence |
|---|---|---|
| L0 | Success | Payload released to the Execution Plane for immediate commit |
| L3 | Interdict | Fatal failure — immediate system-wide rollback and emergency halt |

**The AI Kill-Switch** monitors resources rather than intent: execution
time, recursive logic loops, memory, and tokens. Any breach triggers an
immediate emergency stop, terminating the process before it can impact the
host system.

## Conclusion: the power of deterministic safety

The PTAC loop ensures industrial AI agents are not just capable, but
fundamentally reliable. By forcing every cognitive cycle through a gauntlet
of Clean Trail perception, sandboxed reasoning, and air-gapped sidecar
validation, META-CORE transforms a probabilistic model into a deterministic
industrial component.

- **Isolation is absolute** — compute isolation ensures the Reasoning Plane
  never possesses the credentials to cause unauthorized state changes.
- **Schema-driven authority** — the Decision Packet is a rigid bridge,
  ensuring AI reasoning is translated into verifiable, machine-safe
  instructions.
- **The sidecar is the guardian** — by placing safety checks in an
  air-gapped sidecar, the system's rules stay immune to prompt-injection or
  reasoning compromises.
