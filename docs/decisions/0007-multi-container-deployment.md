# ADR-0007: Multi-container deployment via Docker Compose

**Status:** Accepted

## Context

Packaging Next.js, the Python reasoning sandbox, and the validator sidecar
inside a single host container would simplify orchestration and local
networking. But it would also expose the SQLite database and the AI
Kill-Switch daemon to the same filesystem/process namespace as the LLM
runtime — a shell-execution exploit inside the Python environment could then
reach both.

## Decision

Deploy the architecture as separate containers with distinct network
boundaries, orchestrated with Docker Compose
(`/containerization/docker-compose.yml`): `execution-api`,
`reasoning-sandbox`, `control-sidecar`, and `redis-queue`, each with its own
mount and network policy.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Single monolithic container | Simplest configuration and local logging, but eliminates the security isolation the architecture depends on |
| **Multi-container Docker Compose (chosen)** | Adds container networking and multi-build complexity, but keeps an LLM compromise from reaching the database or the kill-switch |

## Consequences

- The `reasoning-sandbox` container carries no SQLite volume at all — it
  physically cannot mount what it doesn't have.
- Inter-container IPC must be explicitly designed and secured; the current
  transport for the sidecar link is still an open question.
- Local development requires running the full multi-container stack rather
  than a single process, raising the barrier to a quick local run.

See [Deployment Requirements](../deployment/requirements.md).
