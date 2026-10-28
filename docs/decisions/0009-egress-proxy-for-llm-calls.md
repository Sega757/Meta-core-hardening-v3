# ADR-0009: Egress proxy whitelisting for outbound LLM API calls

**Status:** Accepted

## Context

The Reasoning Plane sandbox has blocked raw network sockets — but `agent.py`
still needs to reach an external LLM API to actually run the Think phase of
the PTAC loop. A flat "no network" rule and a functioning agent are mutually
exclusive.

## Decision

Block raw outbound sockets on the `reasoning-sandbox` container entirely,
but route all model calls through a secure **egress proxy** that whitelists
only pre-approved LLM API endpoints (e.g. specific provider API hosts). The
sandbox never gets an open network; it gets a narrow, audited pipe to one
destination class.

## Alternatives considered

| Option | Trade-off |
|---|---|
| Open all outbound TCP from the sandbox, trust prompt-engineering to prevent misuse | Trivial to deploy, but leaves the host vulnerable to data exfiltration via a crafted payload |
| **Egress proxy with a fixed LLM-endpoint whitelist (chosen)** | Adds proxy configuration and maintenance, but keeps a zero-trust network boundary while the agent still functions |

## Consequences

- Any new LLM provider or endpoint requires an explicit whitelist update —
  intentional friction against silent scope creep.
- A compromised prompt still cannot open an arbitrary outbound connection;
  the blast radius of data exfiltration is capped by what the proxy will
  relay.

See [Reasoning Plane](../architecture/reasoning-plane.md).
