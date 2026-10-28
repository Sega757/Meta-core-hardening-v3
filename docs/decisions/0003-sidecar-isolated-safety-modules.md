# ADR-0003: Validator and Kill-Switch run in an isolated sidecar container

**Status:** Accepted

## Context

Running `validator.py` and the AI Kill-Switch inside the same process as
`agent.py` would minimize IPC overhead. But if the Reasoning Plane is
compromised via prompt injection (or gains any form of code execution), an
in-process validator's memory — including its resource limits and ethical
bounds — becomes reachable and potentially patchable by the compromised
model.

## Decision

Deploy the Validator and the AI Kill-Switch inside a physically separate
**sidecar container**, with its own process and memory space, independent of
the reasoning sandbox's health.

## Alternatives considered

| Option | Trade-off |
|---|---|
| In-process validation inside `agent.py` | Sub-millisecond validation latency, but a compromised model can potentially disable or bypass its own safety checks |
| **Out-of-process sidecar (chosen)** | Adds serialization/IPC latency, but guarantees safety-rule immutability even under full sandbox compromise |

## Consequences

- The Kill-Switch can terminate the reasoning sandbox even if that
  sandbox's interpreter is unresponsive or CPU-saturated.
- Every Decision Packet incurs an extra IPC hop before it can reach the
  Execution Plane.
- The sidecar's own transport protocol and signaling mechanism must be
  hardened, since it is now the single most safety-critical boundary in the
  system (see open questions in [Control Plane](../architecture/control-plane.md)).

See [Control Plane](../architecture/control-plane.md) and
[L-E-J-D-A-S framework](../security/l-e-j-d-a-s-framework.md).
