# ADR-0006: Dual schema validation (Zod + JSON Schema 2020-12)

**Status:** Accepted

## Context

The Reasoning Plane (Python) needs to validate raw LLM output into a
structured Decision Packet; the Execution Plane (TypeScript) needs to
validate that packet's fields before a database write. Using one schema
language across a Python/Node.js language boundary isn't practical, but
maintaining two independent schema definitions by hand risks drift — a tool
argument added on one side and forgotten on the other silently breaks
validation.

## Decision

Use **JSON Schema 2020-12** in the Reasoning/Control planes to validate
model output into a Decision Packet, and **Zod** in the Execution Plane to
give the Node.js API compile-time and runtime type safety. Treat the Zod
(TypeScript) definitions as the single source of truth, generating JSON
Schema from them at build time rather than maintaining both by hand.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Hand-maintain duplicate schemas in both frameworks | Simple for an MVP, but prone to drift and validation mismatches as tools evolve |
| **Single source of truth, Zod → JSON Schema compilation (chosen)** | Requires build-time tooling, but guarantees the two planes can never disagree on a tool's shape |

## Consequences

- Tool schema changes only need to be made once, in TypeScript.
- Build tooling for the Zod-to-JSON-Schema compilation step needs to exist
  and be kept current — not yet specified (see
  [`docs/open-questions.md`](../open-questions.md)).
- Error-reporting from a failed validation back to the agent for
  self-correction is still an open design question.

See [Data Architecture](../architecture/data-architecture.md).
