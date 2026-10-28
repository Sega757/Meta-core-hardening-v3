# ADR-0001: Decoupled local telemetry with 5-minute batched remote sync

**Status:** Accepted

## Context

High-frequency step logging (every Perceive/Think/Act/Check transition)
needs to be durable and, ideally, immediately visible off-site for audit
purposes. But synchronous remote Git commits on every event introduce
network latency and hit GitHub/GitLab API rate limits, which directly
conflicts with the Execution Plane's sub-100ms CRUD latency target.

## Decision

Write events locally and atomically (`transponder.py`, `flush + os.fsync`)
to `event_stream.jsonl` in real time. Sync that file to a remote Git
repository in batches, on a fixed 5-minute `systemd` timer
(`log_sync.sh` / `log-sync.timer`), independent of the request path.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Synchronous remote commit per event | Guarantees instant off-site visibility, but introduces network-latency bottlenecks and GitHub API rate-limiting on every request |
| **Decoupled local write + 5-minute batched push (chosen)** | Introduces a 5-minute remote-audit lag, but guarantees sub-100ms local task performance |

## Consequences

- Local execution stays fast and available even if the remote Git host is
  down.
- Remote telemetry is only eventually consistent (worst case ~5 minutes
  stale).
- A local disk failure between two sync ticks is a real, accepted exposure
  window for the audit trail — mitigated by the atomic `fsync` write, not
  eliminated.

See [Observability Plane](../architecture/observability-plane.md) for the
implementation.
