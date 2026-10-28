# Security Audit Protocol: Isolation & Failure Integrity

This protocol is the sign-off checklist for a META-CORE deployment. It
treats "safety" as a property of the *architecture*, verified independently
of how capable or well-behaved the underlying model is. Failing any item
marked **Critical** blocks production release.

## 1. Multi-plane isolation

| Feature | Execution Plane (Node.js stack) | Reasoning & Observability planes |
|---|---|---|
| Components | Next.js, Node.js v20, SQLite (WAL), Redis, BullMQ | `agent.py`, `observer.py`, `log_sync.sh` |
| Coupling | Tight — integrated for <100ms CRUD latency | Loose — isolated, no shared memory |
| Data authority | Exclusive DB credentials | No database access; read-only telemetry |
| Communication | Internal IPC (AIDL) | Append-only `event_stream.jsonl` |

**Audit requirement:** verify `transponder.py` performs an atomic `flush`
followed by `os.fsync` on every event write — this is the non-negotiable
hardware-durability guarantee behind the "Clean Trail."

## 2. Reasoning sandbox & sidecar integrity

Interrogate the sandbox directly:

- **Socket blockage** — confirm at the container/OS level that raw network
  sockets are blocked; the sandbox must be unable to make unauthorized
  external calls (outside the approved egress proxy, per
  [ADR-0009](../decisions/0009-egress-proxy-for-llm-calls.md)).
- **Credential blacklisting** — confirm the absolute absence of database
  credentials or API keys in the Python environment or memory.
- **PTAC enforcement** — confirm no "Act" command can be issued without
  passing "Check."
- **Command authenticity** — confirm Ed25519 signature verification runs on
  every inbound AIDL instruction.

Interrogate the sidecar:

- Confirm the Validator and Kill-Switch run in a **physically separate
  container**, not a shared memory space with the LLM.
- Confirm the sidecar can terminate the reasoning sandbox even when the
  Python interpreter is fully CPU-saturated or unresponsive.

**High-priority open finding:** the AIDL IPC bridge's interface contract and
the L0–L3 escalation rules are undefined in the current design. Both must be
specified and verified before Stage 3 sign-off.

## 3. Deterministic data logic

- **Knowledge Object hashing** — confirm `fact_key = SHA-256(JCS([subject,
  predicate]))` (excludes `object`, so a changed value is a new version of the
  same fact), that `ko_id` covers content, provenance, parent and time, and run
  `audit_ko_ids` to re-derive every stored id (SQLite cannot).
- **SQLite WAL mode** — confirm it's enabled, allowing `observer.py`
  non-blocking parallel reads while the Execution Plane writes.
- **Dual gatekeeping** — confirm every state change passes both JSON Schema
  2020-12 (structural) and Zod (type-safe, final gate before commit).

## 4. Failure mode analysis

| Threat | Mitigation | Audit command (negative test) |
|---|---|---|
| Prompt injection | Air-gapped sidecar validator | Feed a jailbreak prompt to the agent; confirm the sidecar rejects the resulting Decision Packet |
| Resource exhaustion | OS-level Kill-Switch monitoring time/memory/tokens | Simulate an infinite loop in `agent.py`; confirm the sidecar kills the process within defined thresholds |
| DB corruption | Idempotent BullMQ handlers | Re-submit a completed task ID; confirm the system rejects the duplicate write |
| Race conditions | Redis distributed locks (1–2 tasks/agent) | Launch 5 simultaneous tasks for one agent; confirm 3 are queued or blocked |

## 5. Verification checklist

### Stage 1 — Execution Plane (Critical)
- [ ] SQLite WAL mode confirmed and load-tested for parallel read/write
- [ ] BullMQ workers verified idempotent against re-submitted tasks
- [ ] Redis distributed locks enforced (max 2 concurrent tasks/agent)

### Stage 2 — Reasoning Plane (Critical)
- [ ] Python 3.12 venv isolated; zero DB credentials present
- [ ] Container-level network-socket block verified via negative test
- [ ] Ed25519 signature verification confirmed on all inbound AIDL commands

### Stage 3 — Control Plane (Critical)
- [ ] Validator and Kill-Switch confirmed in a separate sidecar container
- [ ] **Open finding:** L0–L3 escalation rules documented and verified
- [ ] **Open finding:** AIDL IPC bridge protocol specified and inspected for
      injection vulnerabilities
- [ ] Capability tokens and CAS ([ADR-0016](../decisions/0016-capability-tokens-and-cas.md)):
      stale `state_version` aborts with no mutation, a redelivered packet is
      applied once, a token for another action is rejected (`ToctouTests`,
      `CapabilityTokenTests`); the Node.js verifier passes the ADR test vector

### Stage 4 — Telemetry (High)
- [ ] `transponder.py` verified for atomic `flush` + `os.fsync` durability
- [ ] `log_sync.sh` systemd timer confirmed at 5-minute intervals
- [ ] `observer.py` generating trace trees (Tier 1); the robust-statistics detector exists as reference code (`src/observability/observer.py`, not wired to a running process) and the Tier 2 offline audit of [ADR-0015](../decisions/0015-claim-level-trace-evaluation.md) is specified, not implemented

### Stage 5 — Structural integrity (Standard)
- [ ] Air Coefficient check conducted — redundant abstractions and naming
      drift pruned

## Final assessment framing

A deployment that passes this checklist is **fail-secure**: isolating the
Reasoning Plane and anchoring safety in a physically separate sidecar means
operational integrity does not depend on the model's intelligence or good
behavior. Security here is a property of the architecture, not of the
agent's judgment.
