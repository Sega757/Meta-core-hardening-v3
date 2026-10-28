# ADR-0008: Sequential, health-gated stage bootstrap

**Status:** Accepted

## Context

Booting all five stages' services in parallel is faster to start, but the
Reasoning Plane depends on the Execution Plane's database/queues already
existing, and it would be unsafe for the Execution API to accept traffic
before the Control Plane's validator sidecar is healthy — a window would
exist where Decision Packets could be dispatched with nothing checking them.

## Decision

Boot the five stages in strict dependency order (Execution → Reasoning →
Control → Telemetry → Self-Audit), and gate each stage's public entry point
on a health check against the previous stage, rather than a fixed startup
delay. `agent.py` specifically polls for `event_stream.jsonl` availability
on startup instead of assuming it already exists.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Independent parallel container boot, with retry/backoff on connection | Simpler orchestrator configuration, but creates a window where the agent could act before the safety sidecar is online |
| **Sequential, health-gated boot (chosen)** | Increases orchestrator logic and total boot time, but guarantees zero-trust safety from process start |
| Bundle the Python reasoning runner as a Node.js child process | Simplifies process tracking, but violates the Reasoning Plane's isolation | 

## Consequences

- Deployment scripts need explicit health-check dependencies, not just
  container start-order in Compose.
- `agent.py` stays resilient to Execution Plane restarts by polling for
  file availability instead of crashing on a missing dependency.
- Total cold-start time is longer than a naive parallel boot.

See [Assembly Stages](../deployment/assembly-stages.md).
