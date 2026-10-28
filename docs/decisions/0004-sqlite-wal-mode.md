# ADR-0004: SQLite in Write-Ahead Logging mode

**Status:** Accepted

## Context

Under standard SQLite rollback-journal mode, active writes block reads.
`observer.py` needs to run analytical queries against transactional state
continuously; if those queries block on the Execution Plane's writes (or
vice versa), either observability stalls or CRUD latency blows past the
sub-100ms target.

## Decision

Configure SQLite in **Write-Ahead Logging (WAL) mode** during initial schema
bootstrap (Stage 1). Writers append to a WAL journal instead of locking the
main database file, so readers can proceed concurrently without contention.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Standard rollback journaling | Maximum single-thread consistency, but blocks telemetry reads during writes, degrading API performance under load |
| **WAL mode (chosen)** | Introduces secondary `-wal`/`-shm` journal files, but allows fully concurrent, lockless reads and writes |

## Consequences

- `observer.py` can query live transactional data with zero write-lock
  contention.
- The deployment must manage WAL checkpointing to prevent unbounded
  `-wal` file growth — checkpoint policy is currently an open question (see
  [`docs/open-questions.md`](../open-questions.md)).

See [Data Architecture](../architecture/data-architecture.md).
