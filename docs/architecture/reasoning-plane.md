# Reasoning Plane

The Reasoning Plane is the isolated cognitive layer of META-CORE. It runs
`agent.py` — the `NeocortexSystem1Engine`, built on Python 3.12 and ADK 2.0 —
inside a sandbox that is structurally incapable of touching production state.
It communicates with the rest of the system in exactly two ways: reading the
append-only `event_stream.jsonl`, and emitting a schema-validated Decision
Packet.

## Environment

- **Python 3.12**, strictly required for native `datetime`
  `timezone.utc` handling and the `cryptography` primitives used for Ed25519
  signature verification of inbound AIDL commands.
- Runs inside a **Python virtual environment (venv)**, isolating dependencies
  such as `cryptography` and `numpy`.
- **Blocked raw network sockets** — no path to the open internet, except
  through the whitelisted egress proxy
  ([ADR-0009](../decisions/0009-egress-proxy-for-llm-calls.md)), which is
  itself implemented as an AI Gateway enforcing per-request spend ceilings
  ([ADR-0012](../decisions/0012-ai-gateway-egress-implementation.md)).
- **Zero database credentials** — no keys, no connection strings, for
  either SQLite or Redis.

## The PTAC cycle

```mermaid
flowchart TD
    P["1. PERCEIVE\nIngest event_stream.jsonl"] --> T["2. THINK\nSandboxed reasoning\n(no network / DB)"]
    T --> A["3. ACT\nCompile Decision Packet\n(JSON Schema 2020-12)"]
    A --> C["4. CHECK\nRoute to Control Plane sidecar"]
    C -->|fail| P
    C -->|pass| D[Dispatch to Execution Plane]
```

| Phase | Action | Output |
|---|---|---|
| Perceive | Reads current environmental/task context from the append-only event log | Contextual snapshot |
| Think | Processes the payload inside the sandboxed runtime | Logical strategy |
| Act | Compiles the strategy into a strict JSON-Schema 2020-12 structure | Decision Packet |
| Check | Hands the packet to `validator.py` in the Control Plane sidecar | Approve / escalate signal |

## Context budgeting

Perceive does not hand Think an unbounded replay of `event_stream.jsonl`. The
window is split into an **inelastic reserve** in absolute tokens (system prompt,
tool JSON schemas, last user intent, output buffer, protected anchors), which is
never truncated, and an **elastic pool** shared by RAG chunks and conversation
history by max-min fair (waterfilling) allocation with deterministic eviction:
oldest history pairs first, lowest-relevance chunks first. If the reserve exceeds
`HARD_INELASTIC_CAP` the allocator raises a configuration error before any LLM
call. Algorithm and limits: [ADR-0014](../decisions/0014-context-budgeting.md);
reference code: [`src/reasoning/context_budget.py`](../../src/reasoning/context_budget.py).

## Example Decision Packet

The contract is [`schemas/decision_packet.schema.json`](../../schemas/decision_packet.schema.json)
(JSON Schema 2020-12). The Reasoning Plane emits `lejdas_attestation` and
`capability_token` as `null`; the Control Plane fills them in, and a token may only
exist together with an `L0` attestation.

```json
{
  "packet_id": "123e4567-e89b-42d3-a456-426614174000",
  "parent_state_version": 7,
  "provenance": "SYNTHETIC_AGENT",
  "tool_call": {
    "tool_name": "ledger.adjust",
    "args": { "sku": "META-001", "delta": -10 }
  },
  "justification": "Correcting discrepancy noted in event log #882; actual count 40 vs system count 50.",
  "lejdas_attestation": null,
  "capability_token": null
}
```

`parent_state_version` is the ledger version the agent saw while planning; the
Execution Plane will refuse the mutation if it has moved
([ADR-0016](../decisions/0016-capability-tokens-and-cas.md)). `provenance` is
declared by the agent but enforced by the trusted side: an agent-channel packet
with any value other than `SYNTHETIC_AGENT` is rejected.

If a field is missing, malformed, or hallucinated, schema validation rejects
the packet before it ever leaves the sandbox boundary — it never reaches the
database layer.

## Security properties

- **Network/DB air-gap** — a prompt-injection compromise here has nothing to
  exfiltrate and nowhere to write.
- **Command authenticity** — inbound AIDL instructions from the Node.js
  orchestrator are verified with Ed25519 signatures using the `cryptography`
  library.
- **Fact determinism** — Knowledge Objects are versioned and content-addressed
  (`fact_key`, `ko_id`) with provenance bound into the id; see
  [Data Architecture](data-architecture.md).
- **No self-disable path** — the Validator and Kill-Switch live in a
  separate sidecar container, so nothing the agent does inside its own
  sandbox can touch them. See [Control Plane](control-plane.md).
- **Intent pre-filtering** — before unstructured external content (a
  scraped page, an uploaded document, a comment) enters the Think-phase
  prompt, a lightweight classifier screens it for injection/manipulation
  patterns; flagged content is excluded from the prompt rather than passed
  through for the main model to reason about. (Architectural extrapolation
  — no specific classifier model or score threshold is fixed yet; see Open
  questions.)

## Open questions

- Fault-tolerance / retry logic for failed LLM API calls inside the PTAC
  cycle.
- Whether a validation failure's detail is fed back to the agent to drive an
  automatic corrective Perceive-Think-Act retry, or requires external
  intervention.
- Wiring the context allocator into `agent.py` (the allocator and tests exist;
  tokenizer choice and a counting safety margin are unspecified).
- The transport format `agent.py` uses to reach the Node.js AIDL IPC bridge.
- Which classifier model and score threshold the intent pre-filter uses,
  and what happens to content it flags (dropped silently vs. logged to
  `event_stream.jsonl` as a rejected input).

See [`docs/open-questions.md`](../open-questions.md) for the full,
consolidated list.
