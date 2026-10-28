# ADR-0012: AI Gateway egress, per-request ceilings and cache policy

**Status:** Accepted — revision 2, amended in v3 (manifest and check); extends [ADR-0009](0009-egress-proxy-for-llm-calls.md)

## Context

ADR-0009 fixes *which* LLM endpoints the sandbox may reach. Revision 1 of this
ADR added a gateway with per-request budgets and a **semantic cache** (reuse an
answer for a vector-similar prompt). The cache is unsafe in the PTAC loop:

- A near-identical prompt can be built from a *different* database state. The
  cache returns the `Decision Packet` computed for the old state, which the
  Control Plane would then validate and the Execution Plane would apply: a
  **state-poisoning / TOCTOU** path through the cache.
- Vector similarity has no explainable threshold, so "close enough" cannot be
  audited.

## Decision

1. **Per-request ceilings.** The gateway rejects any request whose
   `max_tokens` exceeds the reservation made by the sidecar
   ([ADR-0011](0011-loop-detection-algorithm.md)), meters actual usage and reports
   it back so the sidecar can `settle` the bucket. Spend alarms in currency stay
   as a secondary, reactive control.
2. **Semantic cache is forbidden** for every call that generates a `Decision
   Packet` or can influence the Control or Execution Plane (all Think/Act calls and
   any call with mutation tools in scope).
3. **Caching is off by default.** If enabled for those calls, only a
   **state-bound exact cache** is allowed:

   `cache_key = SHA-256(JCS([model_id, system_prompt_hash, state_root, canonical_prompt_payload]))`

   (`compute_cache_key` in `src/control/validator.py`). Any change to the model,
   the system prompt, the committed state or any byte of the canonical payload
   changes the key. The cache stores the raw completion only; `packet_id`,
   attestation and capability token are created fresh and never cached.
4. A semantic cache is allowed **only** for read-only reference queries against
   static external documentation, with no tool access and no path to state
   mutation.

| Call class | Cache |
|---|---|
| Produces or influences a Decision Packet | none (default) or state-bound exact |
| Read-only lookup in static public docs, no tools | semantic allowed |

`state_root` is the hash-chain head in `state_ledger`
([ADR-0016](0016-capability-tokens-and-cas.md)); it changes on every committed
mutation of its scope. It is a hash chain, not a Merkle tree. Anything the prompt
depends on that is **not** covered by a ledger scope (event-stream excerpts, tool
output) must be inside `canonical_prompt_payload`.

## Alternatives considered

| Option | Why not |
|---|---|
| Semantic cache everywhere (revision 1) | State poisoning, unauditable threshold |
| Exact cache keyed on the prompt only | Stale after a commit; same hazard with a lower hit rate |
| **No cache by default; state-bound exact when measured to pay off (chosen)** | In PTAC the state changes after every commit, so the expected hit rate is low; the component is not justified without a measured hit rate |

## Consequences

- Gateway products whose semantic-cache feature is on by default must have it
  disabled for these routes. The gateway configuration is not part of this
  repository, so this is **UNVERIFIED** until a deployment config and a test
  exist.
- `state_root` must be read in the same transaction snapshot that produced the
  prompt inputs; otherwise the key can match a state the prompt did not see.

## Evidence

`compute_cache_key` is covered by `tests/test_control_invariants.py::CacheKeyTests`
(key changes with each component, is stable under payload key order). The gateway
itself is not implemented here.

## Amendment (v3): declared manifest and a machine check

The cache and budget rules are now declared in
[`config/ai_gateway.yaml`](../../config/ai_gateway.yaml) and checked by
[`src/config.py`](../../src/config.py) (`python3 -m src.config`, `make check-config`):
deny-by-default egress with plain hostnames only (no wildcard), TLS required;
`reasoning-decision` must exist, declare that it produces packets and has tools,
and have semantic cache **off**; no route that produces a packet or has tools may
enable the semantic cache; an exact cache, if enabled, must be state-bound with the
four key fields of this ADR; timeouts finite and ordered (`connect < read ≤ total`);
retries ≤ 2 (0 on the decision route); each route's per-request ceiling must fit
`MAX_TOKEN_BUDGET`.

**Still unverified.** The manifest is META-CORE's own schema, **not** a LiteLLM
configuration. Nothing in this repository translates it into a gateway product's
settings or tests that a deployed gateway honours it, so "enforced on the wire"
remains unproven; this amendment turns "no config, no check" into "a declared
config with a check". Limits and timeouts are uncalibrated starting values; the
provider host is a placeholder (`llm-provider.example`).
