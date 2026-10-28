# Audit 0001: Go/gRPC/TLA+ Stack — An Alternative Realization of the Control Plane

**Status:** External review, recorded for reference — **not adopted**.
**Scope:** Proposes a different implementation stack for the safety triad
than the one currently documented in [`docs/architecture/`](../architecture/overview.md).

## Relationship to the documented architecture

This audit evaluates META-CORE's core engineering problem — taming
stochastic LLM drift in loops where an error is expensive — and endorses the
project's fundamental split: **the agent is an unreliable hypothesis
generator (Reasoning); execution is a deterministic finite-state machine
with formal safety invariants.** That framing matches this repository's own
[Operational Planes model](../architecture/overview.md).

Where it diverges is the **realization**. The rest of this repository
documents a Node.js/Python stack (Next.js execution API, `agent.py`
Reasoning Plane, `validator.py` + Kill-Switch Control Plane sidecar, AIDL
IPC, JSON Schema 2020-12, the L-E-J-D-A-S safety fields — see
[`docs/architecture/control-plane.md`](../architecture/control-plane.md) and
[`docs/security/l-e-j-d-a-s-framework.md`](../security/l-e-j-d-a-s-framework.md)).
This audit instead evaluates — and largely proposes — a **Go + gRPC + TLA+**
realization with a renamed/restructured triad. The two are **not** currently
reconciled; this document is recorded as an alternative viewpoint and a
source of concrete hardening ideas, several of which apply regardless of
which stack is chosen. See [Open questions](#open-questions-raised-by-this-audit)
below for what would need to be resolved to merge the two.

## 1. The architectural triad, as evaluated here

```
[ Stochastic Domain ]               [ Deterministic Control Plane (Go) ]
+---------------------+             +--------------------+     +---------------------+
| 1. Reasoning Engine | --gRPC-->   | 2. Empathy Layer   | --> | 3. Kill-Switch      | --> [Actuator / IPC]
|   (ADK / LLM / MCP) | (Intent)    |   (Impact Lattice) |     |   (TLA+ State Gate) |     (Execution)
+---------------------+             +--------------------+     +---------------------+
                                               |                          |
                                        [Degrade: Safe Mode]       [Hard Cutoff: Panic]
```

### Layer 1 — Reasoning Engine (probabilistic core)

- **Role:** intent formation, task decomposition, context analysis via
  LLM/ADK.
- **Strength:** isolation — the layer has no direct I/O into the system. It
  produces an **Intent Proposal**, not an execution command. (This matches
  this repo's [Decision Packet](../glossary.md) concept, though the
  proposed transport here is gRPC/Protobuf rather than the documented
  AIDL/JSON-Schema path.)
- **Risk flagged:** format drift. Without strict Protobuf schema validation
  with bounds-checking *before* the payload reaches business logic, invalid
  states can be injected.

### Layer 2 — Empathy Layer (contextual-harm assessment & degradation)

- **Role:** assess semantic and systemic impact on people, equipment, and
  process; drop the system into Safe Mode / Fallback on anomaly.
- **Strength:** graduated degradation instead of a binary
  works/crashed. In industrial settings, an Emergency Stop often carries
  large financial losses; a controlled Safe Mode preserves process
  continuity. This is directionally the same idea as this repo's
  [L1/L2 escalation tiers](../security/l-e-j-d-a-s-framework.md), which
  already separate "log and self-correct" from "halt and await
  authorization" rather than jumping straight to a hard kill.
- **Critical concern:** "empathy" must not be a second LLM acting as a
  checker ("ask another model if this is safe") — that creates recursive
  uncertainty and doubles round-trip latency. This layer must be a
  **combination of semantic constraints and formal policy/rule engines**,
  not another probabilistic hop.

### Layer 3 — Kill-Switch Layer (deterministic safety gate)

- **Role:** the final barrier before the actuator; a fully deterministic
  finite-state machine.
- **Strength:** hard fail-safe. Proposed to be written in native Go, no
  external async dependencies, guaranteed O(1) response time. If an
  invariant is violated, the layer fails closed instantly.
- Directionally the same responsibility as this repo's documented
  [AI Kill-Switch](../architecture/control-plane.md#ai-kill-switch), but
  specified here as a formally-verified FSM rather than a monitoring
  daemon watching time/memory/token/loop signals.

## 2. Proposed implementation stack: Go + gRPC + TLA+

This is assessed as the most mature part of the proposal:

1. **Go** — low memory overhead, predictable GC, high throughput under
   channel multiplexing; goroutines/`select` are well suited to hardware
   watchdogs and `context.WithTimeout`-style deadlines. Flagged
   requirement: strict goroutine hygiene (no leaks on stream interruption)
   and mandatory `go test -race` across the pipeline.
2. **gRPC / Protocol Buffers** — binary protocol, strict typing, backward
   compatibility; removes the implicit type coercion and floating-point
   precision loss that JSON-RPC/REST are prone to for telemetry.
3. **TLA+ formal verification** — proof of safety (`□Safe`) and liveness
   (`◇Progress` / deadlock-freedom) for the triad's state transitions. This
   is called out as the property that would move the project from
   "well-built wrapper" to **industrial-grade**: the only way to guarantee
   that under network degradation or an LLM hang, the system does not stall
   in an undefined intermediate state.

## 3. The core risk vector: TOCTOU

**Time-of-Check to Time-of-Use** is identified as the primary architectural
risk for any deterministic-gate-over-stochastic-planner design — including
this repository's own PTAC/Check flow, not just the Go variant:

1. **Reasoning** proposes: *open valve V-102*.
2. **Empathy + Kill-Switch** check: *circuit pressure = 2.1 atm — nominal.
   Approved.* (CHECK)
3. *Inference/streaming latency (50–300 ms)…*
4. Pressure spikes to 6.0 atm from an unrelated fault.
5. **Actuator** executes the already-approved command and opens the valve
   under critical pressure. (EXECUTE)

### Proposed mitigation: Atomic CHECK-BIND-EXECUTE

Invariant checking must not happen at planning time — it must happen at the
moment of binding to the physical world.

**State-Bound Capability Tokens:** the Kill-Switch issues a one-time,
cryptographically signed token, valid only for *N* milliseconds and only
while the live environment state vector `S_t` still satisfies the
pre-condition predicate:

```
PreCondition(S_t) ∧ Valid(τ)  ⟹  Exec(Action)
```

i.e. the token is re-checked against fresh state immediately before
execution, not against the state that was true when the plan was approved.

This is a genuinely relevant gap against the documented architecture too:
this repo's [PTAC Check phase](../architecture/reasoning-plane.md) and
[L-E-J-D-A-S validation](../security/l-e-j-d-a-s-framework.md) are both
described as evaluating a Decision Packet once, with no explicit
re-validation against live state at the moment the Execution Plane actually
commits it. See [open questions](#open-questions-raised-by-this-audit)
below.

## 4. Six-field integrity scorecard (as assessed by this audit)

This audit uses its own six-field naming, distinct from this repo's
[L-E-J-D-A-S](../security/l-e-j-d-a-s-framework.md) (Legal, Ethical,
Jurisdictional, Duty, Accountability, Safety). Mapped side by side for
reference:

| This audit's field | Status | Analysis | Nearest L-E-J-D-A-S field |
|---|---|---|---|
| **Logic** | High | TLA+ spec mathematically closes FSM contradictions; logic is moved out of the stochastic domain into the formal one. | *(new — no formal-verification equivalent documented yet)* |
| **Ethics / Safety** | Medium | The Empathy Layer sets the right direction, but needs a rigorous mathematical model (lattice-based access control), not heuristic checks. | E (Ethical), S (Safety) |
| **Law / Compliance** | High | The architecture fits EU AI Act requirements for High-Risk AI Systems well: mandatory human-in-the-loop, logging, deterministic oversight. | L (Legal) |
| **Economics** | High | Go-based microservice isolation minimizes compute overhead; the control loop consumes almost no tokens — inference is paid only for planning. | *(new — no cost field documented yet)* |
| **Autonomy** | High | No lock-in to proprietary cloud frameworks; a statically-compiled Go binary can run air-gapped. | D (Duty) — partial overlap only |
| **Vulnerability** | Needs focus | Defense against mesa-optimization and prompt injection at the Reasoning layer; rights issuance should follow Least Privilege via an "Intent Kernel." | A (Accountability) — partial overlap only |

Two fields here — **Logic** (formal-verification status) and
**Economics** (token/compute cost) — aren't represented at all in the
current L-E-J-D-A-S framework and are worth considering as additions or a
sibling scorecard, independent of whether the Go/TLA+ stack itself is
adopted.

## 5. Engineering checklist: MVP → production

1. **Deterministic replay log** — every triad decision recorded to a ring
   buffer capturing the generator seed, weights/prompt hash, external state,
   and gRPC telemetry, enabling 100% incident reproduction post-mortem. This
   is a stricter version of this repo's
   [Clean Trail](../architecture/observability-plane.md) principle — same
   goal (forensic reconstruction), tighter reproducibility bar (seed +
   weights hash, not just the event and its justification).
2. **Hard time-budget watchdog** — enforce a strict SLA on Reasoning Engine
   response time; if inference exceeds `T_max`, the Kill-Switch
   auto-invalidates the transaction without waiting for a reply and drops
   the system into Degraded Standby. This is a more precisely specified
   version of the documented but under-specified
   [Kill-Switch execution-time bound](../security/l-e-j-d-a-s-framework.md#the-ai-kill-switch-monitoring-how-not-what).
3. **Formal-spec alignment** — regularly verify the TLA+ model against the
   real Go implementation using property-based testing (`gopter` or
   `rapid`), so the spec and the code cannot silently diverge.

## Open questions raised by this audit

These are additions to [`docs/open-questions.md`](../open-questions.md),
specific to reconciling this audit with the documented architecture:

- **Stack reconciliation** — whether META-CORE standardizes on the
  documented Node.js/Python/AIDL stack, the Go/gRPC/TLA+ stack proposed
  here, or defines a formal boundary where one could be swapped for the
  other (e.g. the Control Plane sidecar as a pluggable component behind a
  stable contract).
- **TOCTOU in the current design** — the documented PTAC Check phase and
  L-E-J-D-A-S validation are not currently specified as re-checking live
  environment state at the moment of Execution Plane commit. Whether to
  adopt a Capability-Token-style atomic CHECK-BIND-EXECUTE pattern (or an
  equivalent) is unresolved. **Update:** the token-plus-CAS part is now
  [ADR-0016](../decisions/0016-capability-tokens-and-cas.md) with reference code;
  external side effects outside the database transaction and formal
  verification remain open.
- **Formal verification** — no TLA+ (or equivalent) specification exists
  yet for the documented Control Plane's state transitions; whether formal
  verification is in scope for this project at all is undecided.
- **Cost/telemetry field** — whether an "Economics" (compute/token cost)
  dimension should be added alongside L-E-J-D-A-S.
- **Replay determinism bar** — whether the documented
  `event_stream.jsonl` trail needs to be extended to capture seed and
  model/prompt hashes for full deterministic replay, as proposed here.

## Summary (as submitted)

> META-CORE is a rare and correct engineering approach to agent autonomy:
> it strips out the probabilistic nature of neural networks with a strict
> formal core, protecting hardware and business from hallucinations.
