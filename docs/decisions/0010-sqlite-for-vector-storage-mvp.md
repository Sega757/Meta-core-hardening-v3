# ADR-0010: SQLite-hosted vector storage for the MVP

**Status:** Accepted (MVP scope — revisit trigger defined below)

## Context

Knowledge Objects carry an `embedding` field for semantic search. Storing
and querying high-dimensional vectors with cosine-similarity search inside
plain SQLite risks violating the sub-100ms CRUD target once dataset size or
query volume grows, but standing up a dedicated vector database (Faiss, an
embedded vector library, etc.) adds a second storage engine and a
synchronization problem for an MVP that doesn't need it yet.

## Decision

For the MVP, store raw embedding values inside SQLite (BLOB or equivalent)
and compute similarity in-application, keeping data unified in a single
relational container. Revisit and migrate to a dedicated embedded vector
index once vector dimensionality or query volume measurably degrades CRUD
latency.

## Alternatives considered

| Option | Trade-off |
|---|---|
| **Raw embeddings in SQLite, app-level similarity math (chosen for MVP)** | Avoids added system complexity, but introduces search latency as data scales |
| Independent embedded vector index (e.g. Faiss) synced with SQLite | Preserves sub-100ms search at scale, but adds a second process dependency and file-locking/sync overhead |

## Consequences

- The Observability Plane should monitor query latency on embedding lookups
  as an explicit signal for when to trigger the migration.
- The embedding pipeline itself (which model, local vs. API) is not yet
  specified — see [`docs/open-questions.md`](../open-questions.md).

See [Data Architecture](../architecture/data-architecture.md).
