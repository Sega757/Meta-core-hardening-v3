# ADR-0014: Waterfilling context allocator with an inelastic reserve

**Status:** Accepted — revision 2. Replaces the fixed percentage bands of
revision 1 (10–15 / 15–20 / 30–40 / 20–30 / 10–15 %).

## Context

Static percentages break in two ways. In a tight window a percentage cut can
truncate a tool JSON schema, and a truncated schema crashes the parser. In a large
window (128k and up) fixed bands leave most of the window unused. The percentage
values also had no source.

## Decision

[`WaterfillingContextAllocator`](../../src/reasoning/context_budget.py) splits the
window into two parts.

**Inelastic reserve**, in absolute tokens, never truncated: system prompt, tool
JSON schemas, last user intent, output buffer (`max_tokens`) and history *anchors*
(protected pairs). If the reserve exceeds `HARD_INELASTIC_CAP`, or leaves no room
in the window, the allocator raises `ContextConfigurationError` **before any LLM
call**.

**Elastic pool** = `context_window − inelastic_used`, shared by RAG chunks and
conversation history:

1. **Waterfilling (max-min fairness).** Each class asks for its full demand; no
   class gets more than it asked for; if all fits, nothing is cut; otherwise the
   smaller demand is served first and the remainder goes to the larger.
2. **Eviction inside each class**, deterministic: history loses its *oldest pairs*
   first (a pair is never split; the kept history is a contiguous recent
   suffix); RAG loses the *lowest `relevance_score`* first (ties by id).
3. **Slack pass.** Unused tokens go back, RAG chunks first, then the next-older
   history pairs.
4. The plan lists every eviction so the caller can write truncation events to
   `event_stream.jsonl` (the clean trail records what the model did not see).

The same inputs give the same plan regardless of input order.

## Alternatives considered

| Option | Why not |
|---|---|
| Fixed percentage bands (revision 1) | Truncated schemas; wasted large windows |
| Full history replay | No cost or attention ceiling |
| Strict priority (history always before RAG) | Starves one class entirely; waterfilling gives each a fair share first |
| **Inelastic reserve + waterfilling + deterministic eviction (chosen)** | Schemas safe, big windows used, plan reproducible |

## Consequences

- Token counts are inputs. Use the target model's tokenizer; a counting error
  consumes the output buffer. A safety margin is not specified (open).
- `relevance_score` values must be comparable across retrievers; this is not
  enforced.
- Anchors count toward the hard cap; too many anchors is a configuration error,
  not a truncation.
- Closes the open question "context token budgeting" at specification and
  reference-code level; wiring into `agent.py` is not done.

## Evidence

`tests/test_control_invariants.py::WaterfillTests` and `ContextAllocatorTests`,
including 400 randomized scenarios (window ≤ budget, anchors preserved, kept and
evicted sets partition the input, order independence).
