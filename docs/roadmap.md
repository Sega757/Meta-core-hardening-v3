# Implementation Roadmap

META-CORE deploys in a five-stage cascade so that foundational stability and
zero-trust security boundaries are physically verified before the system's
cognitive capabilities are enabled. Each stage is described in full in
[`docs/deployment/assembly-stages.md`](deployment/assembly-stages.md); this
page is the narrative version — the reasoning behind the ordering.

## Stage 1 — Foundational infrastructure (Execution Plane)

The Execution Plane is the system's primary state manager. Here, and only
here, we accept "tight coupling" — a deliberate exception to the system's
otherwise zero-trust posture, made solely to hit the <100ms CRUD latency
target.

- **Node.js v20+ (LTS) API backend** — central orchestrator; hosts the
  AIDL-based IPC gateway, the singular bridge to the agent executor.
- **SQLite with WAL** — satisfies the Observability Plane's need for
  non-blocking reads while the Execution Plane writes continuously.
- **Redis + BullMQ** — background task lifecycle with distributed locks
  restricting each agent to 1–2 concurrent tasks, preventing rate-limit
  exhaustion via idempotent handlers.

**Success criteria:** asynchronous task processing verified with
at-least-once delivery; sub-100ms CRUD confirmed; zero race conditions under
parallel writes. The cognitive layer depends entirely on this stage — the
Reasoning Plane has no internal database of its own.

## Stage 2 — Cognitive layer integration (Reasoning Plane)

Deployed only after Stage 1 is verified, so reasoning is always grounded in
a stable state-management environment.

`agent.py` (`NeocortexSystem1Engine`, ADK 2.0) runs the deterministic PTAC
cycle specifically to prevent non-deterministic "hallucination loops" — the
model is forced through structured state transitions rather than free-form
looping.

Sandbox isolation is enforced at both the process and container level:
blocked network namespaces, credential-stripped environment variables, and
structural validation (JSON Schema 2020-12 + Zod) on every "Act" output.
Python 3.12 is mandatory for native `timezone.utc` handling and Ed25519
signature primitives that authenticate every command crossing the IPC
bridge.

## Stage 3 — Governance and safeguards (Control Plane)

The safety gateway. The Validator and Kill-Switch live in an out-of-process
sidecar specifically so that a total memory-level compromise of the Python
interpreter in Stage 2 cannot reach the safety modules' memory space to
disable them.

Every Decision Packet is assessed against
[L-E-J-D-A-S](security/l-e-j-d-a-s-framework.md) before execution. Failures
cascade through **L0–L3**, from minor warnings to fatal interdicts that
purge the current task. The Kill-Switch separately monitors execution time,
memory, token consumption, and loop detection, recording every safety
decision to the Clean Trail for post-hoc forensic auditing.

## Stage 4 — Observability and telemetry pipeline

Founded on the "Clean Trace" (Чистый след) principle: total transparency
without remote-logging latency.

`transponder.py` mandates `flush + os.fsync` for atomic event logging —
guaranteeing the trail survives a hard crash. `log_sync.sh`, on a systemd
timer, absorbs the resulting conflict between high-frequency local logging
and remote Git synchronization latency by accepting a 5-minute sync gap (see
[ADR-0001](decisions/0001-decoupled-telemetry-sync.md)). `observer.py`
parses the stream asynchronously: Tier 1 builds trace trees and flags
behavioral outliers with robust statistics (Modified Z-score, Huber), and an
offline Tier 2 audit evaluates sampled runs (see
[ADR-0015](decisions/0015-claim-level-trace-evaluation.md)).

## Stage 5 — System self-audit and the "Air Coefficient"

The final, *recurring* stage — a defense against code-base decay. The "Air
Coefficient" is a qualitative and quantitative lean-ness metric applied on a
bi-weekly cadence:

- **Abstraction pruning** — remove unused interfaces or speculative
  "future-proofing" code that complicates the Execution Plane.
- **Naming alignment** — keep class/module names matched to the current
  blueprint.
- **Blueprint verification** — cross-reference the deployed state against
  the deterministic Knowledge Object model.

## The deterministic Knowledge Object model

Underpinning all five stages, facts are stored deterministically:

| Field | Value / formula |
|---|---|
| `fact_key` | `SHA-256(JCS([subject, predicate]))` — logical identity of the fact |
| `ko_id` | `SHA-256(JCS({content, provenance, parent_ko_id, created_epoch}))` — identity of one immutable version |
| `parent_ko_id` | Previous version of the same fact (64 zeros for the first) |
| `subject` | The primary entity of the fact |
| `predicate` | The relationship or action |
| `object` | The value or target entity |
| `embedding` | Vector representation (semantic search) |
| `provenance` | `ORGANIC_USER`, `SYNTHETIC_AGENT` or `VERIFIED_EXTERNAL`; part of `ko_id` |
| `created_epoch` | Unix time of the version; part of `ko_id` |

See [Data Architecture](architecture/data-architecture.md) for the full
model.

## Conclusion

The cascade gets META-CORE to an industrial equilibrium that's unusual to
combine: sub-100ms latency alongside multi-container air-gaps. As cognitive
complexity scales in later phases, the foundational safety and execution
integrity established in Stages 1–3 remain structurally uncompromised —
they don't degrade as the model gets smarter or the workload gets larger.
