# Architecture Decision Records

Each ADR documents one trade-off point in the META-CORE design: the
conflict that forced a choice, the alternatives considered, and why one was
picked. They follow a lightweight format: **Context → Decision →
Alternatives & trade-offs → Consequences**.

| ID | Title | Status |
|---|---|---|
| [0001](0001-decoupled-telemetry-sync.md) | Decoupled local telemetry with 5-minute batched remote sync | Accepted |
| [0002](0002-execution-plane-exclusive-db-access.md) | Execution Plane holds exclusive database write credentials | Accepted |
| [0003](0003-sidecar-isolated-safety-modules.md) | Validator and Kill-Switch run in an isolated sidecar container | Accepted |
| [0004](0004-sqlite-wal-mode.md) | SQLite in Write-Ahead Logging mode | Accepted |
| [0005](0005-redis-bullmq-execution-only.md) | Redis/BullMQ access restricted to the Execution Plane | Accepted |
| [0006](0006-dual-schema-validation.md) | Dual schema validation (Zod + JSON Schema 2020-12) | Accepted |
| [0007](0007-multi-container-deployment.md) | Multi-container deployment via Docker Compose | Accepted |
| [0008](0008-sequential-health-gated-bootstrap.md) | Sequential, health-gated stage bootstrap | Accepted |
| [0009](0009-egress-proxy-for-llm-calls.md) | Egress proxy whitelisting for outbound LLM API calls | Accepted |
| [0010](0010-sqlite-for-vector-storage-mvp.md) | SQLite-hosted vector storage for the MVP | Accepted |
| [0011](0011-loop-detection-algorithm.md) | Loop detection and admission control for the Kill-Switch | Accepted (rev. 2, amended v3) |
| [0012](0012-ai-gateway-egress-implementation.md) | AI Gateway egress, per-request ceilings and cache policy | Accepted (rev. 2, amended v3) — extends 0009 |
| [0013](0013-provenance-typed-knowledge-objects.md) | Provenance bound into Knowledge Object identity | Accepted (rev. 2, amended v3) |
| [0014](0014-context-budgeting.md) | Waterfilling context allocator with an inelastic reserve | Accepted (rev. 2) |
| [0015](0015-claim-level-trace-evaluation.md) | Two-tier observability: online robust telemetry, offline evaluation | Accepted (rev. 2, Tier 1 code in v3) |
| [0016](0016-capability-tokens-and-cas.md) | Cryptographic capability tokens and optimistic concurrency (TOCTOU) | Accepted (amended v3: outbox) |

The first ten share a single recurring theme: wherever a performance or
convenience win required trusting the Reasoning Plane with more access, the
decision instead pays an isolation or latency cost to keep that plane
untrusted. ADRs 0011–0016 harden specific mechanisms that were previously
left as open questions — loop detection, spend control, data provenance,
context assembly, observability and the approval-to-execution gap — without
changing that underlying trust boundary. Revision 2 of 0011–0015 follows an
architectural review of the first versions; v3 amends 0011, 0012, 0013, 0015 and 0016.
0011–0014 and 0016 have reference code and tests under `src/` and `tests/`; of 0015 only
Tier 1 does, Tier 2 is specification. See
[`docs/architecture/overview.md`](../architecture/overview.md) for how these
decisions compose into the full system.
