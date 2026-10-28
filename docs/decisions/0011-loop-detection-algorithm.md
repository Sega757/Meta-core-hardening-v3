# ADR-0011: Loop detection and admission control for the Kill-Switch

**Status:** Accepted — revision 2, amended in v3 (read-only exploration)

## Context

The first version of this ADR hashed `(tool_name, sorted(args))` with SHA-256 and
added a `$/min` spend limiter. Review found four defects:

1. **Hash bypass.** Key order, `1` vs `1.0`, or a one-character change in an
   argument produces a different hash, so an agent can loop without ever
   repeating a hash.
2. **Reactive cost limiter.** Cost is known only after the call, so billing lag
   and parallel bursts get through before the window sums up.
3. **No structural loop detection.** `A → B → A → B` with changing arguments is
   invisible to a hash of the arguments.
4. **No error-loop handling.** A tool that keeps failing is retried until the step
   budget runs out.

## Decision

`LoopCircuitBreaker` ([`src/control/validator.py`](../../src/control/validator.py))
runs in the Control Plane sidecar. `admit()` is called before a tool call is
dispatched, `record_outcome()` after it. Either raises `KillSwitchTrigger`.

| Check | Rule | Default (starting value, not calibrated) |
|---|---|---|
| Step limit | more than `max_steps` admitted calls in a run | 25 |
| Exact repeat | key = `(SHA-256(JCS({tool_name, args})), state_version)`; a call whose key is in flight or already succeeded is killed | — |
| Structural cycle | the last `period × repeats` calls are one tool-name pattern repeated, all at the same `state_version`; periods 2..4 | repeats = 2 |
| Consecutive errors | the same tool fails (`status == "error"` or an `error_code` is present) N times in a row; any success of that tool resets the count | N = 3 |
| Pre-flight token bucket | reserve `input_tokens + max_output_tokens` atomically **before** dispatch; settle with actual usage afterwards; denial leaves no state behind | capacity and refill set per deployment |

Arguments are canonicalized with **RFC 8785 (JCS)** before hashing, so key order and
`1` vs `1.0` no longer change the hash. A repeat after an *error* is a retry, not a
repeat: it is limited by the consecutive-error rule. A repeat after the state moved
(`state_version` changed) is legitimate and allowed.

## Alternatives considered

| Option | Why not |
|---|---|
| Revision 1: hash of raw args + `$/min` | The four defects above |
| Embedding similarity to catch near-duplicate args | Needs a model in the critical path, is non-deterministic and has no explainable threshold |
| Billing-based breaker (alert on spend) | Reactive by construction; kept only as a secondary alarm in the gateway ([ADR-0012](0012-ai-gateway-egress-implementation.md)) |
| **Canonical hash + structural cycle + error breaker + pre-flight bucket (chosen)** | Deterministic, no LLM call, state kept per run in memory |

## Consequences

- Money is not a unit here: the bucket counts tokens. Token → currency conversion
  and price changes belong to the gateway.
- **False positives are possible and the rule is fail-closed.** Four consecutive
  read-only calls of one tool at an unchanged `state_version` match the
  period-2 pattern and are killed; so is a legitimate `A → B → A → B` exploration.
  Tune `cycle_min_repeats` against real traces. The false-positive rate is
  **UNVERIFIED** (no real traces exist yet).
- Breaker state lives in the sidecar process; a sidecar restart starts a new run
  with empty state.
- Closes the open question "active loop-detection algorithm". The numeric ceilings
  for execution time and memory are still open; the defaults above are starting
  values.

## Evidence

- Reference code and tests: `tests/test_control_invariants.py` (`JcsTests`,
  `TokenBucketTests`, `LoopBreakerTests`). Each rule has a test that fails when
  the rule is removed (mutation-checked in the session that wrote it).
- JCS output matched an independent RFC 8785 implementation (the PyPI package
  `jcs`, used for verification only, not a dependency) on 20 000 random
  structures with 0 mismatches, and reproduces the RFC 8785 §3.2.2 example.
- Not verified: behavior on real agent traces; multi-process sidecar deployment.

## Amendment (v3): legitimate read-only exploration

**Problem.** Revision 2 treated any repeating tool-name pattern at an unchanged
`state_version` as a loop, so legitimate pagination
`A(page=1) → B(id=1) → A(page=2) → B(id=2)` was halted.

**Decision.**

- Tools can be declared read-only in `BreakerConfig.read_only_tools`. The set comes
  from the **trusted side** (tool registry or deployment config). It is deliberately
  *not* a field of `ProposedCall` (extra fields are rejected): a flag the agent could
  set itself would let a compromised agent opt out of cycle detection.
- A repeating pattern is **not** a cycle when every call in the cycle window is a
  declared read-only tool **and** no `(tool, args)` hash repeats inside the window.
- A mutating tool in the window, or a repeated args hash, still halts.
- New bound `max_steps_per_state_version` (default 20, all tools, reason
  `version_step_limit`) caps the exploration the exemption lets through; the counter
  resets when `state_version` moves.

**Consequences.** A tool wrongly listed as read-only bypasses the cycle rule (the
per-version cap, `max_steps`, the exact-repeat rule and the token bucket still
apply), so the registry's correctness is a deployment concern. The default of 20 is
uncalibrated.

**Evidence.** `LoopBreakerTests`: legitimate pagination passes; the same sequence
over undeclared tools halts; mixed windows halt; a repeated args hash inside the
window halts; the per-version cap bounds read-only exploration; an agent cannot mark
its own call read-only. Mutation-checked.
