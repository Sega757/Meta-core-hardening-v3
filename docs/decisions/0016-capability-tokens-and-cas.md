# ADR-0016: Cryptographic capability tokens and optimistic concurrency (TOCTOU)

**Status:** Accepted — amended in v3 (transactional outbox)

## Context

[Audit 0001](../audits/0001-go-grpc-tlaplus-stack-audit.md) names the
time-of-check to time-of-use gap: the Validator approves a Decision Packet against
the state it saw, the Execution Plane applies it later, and the state may have
changed in between (an approved "open the valve" executes at the wrong pressure).
Until now nothing bound an approval to a state version.

## Decision

After the L-E-J-D-A-S gate passes with **L0**, the Control Plane issues a
short-lived token; the Execution Plane applies the mutation only if the token
verifies **and** a compare-and-swap on the state version succeeds.

### Token

```
v1.<base64url(JCS(claims))>.<base64url(HMAC-SHA256(secret_key, JCS(claims)))>
claims = { typ: "meta-core/capability/v1", packet_id, expected_state_version,
           action_hash, expires_at_ms }
action_hash = SHA-256(JCS({tool_name, args}))
```

The MAC covers the canonical claims bytes (an injective encoding of
`packet_id || expected_state_version || action_hash || expires_at_ms`). Verification
order: layout → MAC with `hmac.compare_digest` → claims parse → expiry
(`now_ms >= expires_at_ms` is expired) → `packet_id`, `action_hash` and
`expected_state_version` equal the packet's. Key ≥ 32 bytes; TTL ≤ 60 s.

### Commit ([`src/execution/state_ledger.py`](../../src/execution/state_ledger.py))

In one `BEGIN IMMEDIATE` transaction:

1. `packet_id` already in `applied_packets` → `ALREADY_APPLIED` (idempotent
   redelivery); a different `action_hash` for that id → `ReplayConflictError`.
2. `UPDATE state_ledger SET state_version = state_version + 1, state_root = … WHERE
   scope = ? AND state_version = :expected`; zero rows → `StaleStateError`.
3. Run the mutation, insert into `applied_packets`, `COMMIT`.

`StaleStateError` means **ABORT and return to Perceive**. A failing mutation rolls
the version bump back. The `state_ledger` triggers allow only `+1` steps and forbid
delete and `REPLACE`.

`state_root = SHA-256(JCS([prev_root, packet_id, action_hash]))` is a hash-chain head
per scope, used as the state binding for caches ([ADR-0012](0012-ai-gateway-egress-implementation.md)).

### Cross-language contract (Node.js Execution Plane)

Verify the MAC over the exact payload bytes **before** parsing them; recompute
`action_hash` with an RFC 8785 implementation. Regression vector (key = bytes
`00 01 … 1f`, `packet_id = 123e4567-e89b-42d3-a456-426614174000`, version 7, action
`ledger.adjust {"delta": -10, "sku": "META-001"}`, issued at 1700000000000 ms with a
5000 ms TTL):

```
action_hash  20ce506c808dbb285cce5560f8eb0387c0691b54226ebc13716a3fdfeacacb59
token        v1.eyJhY3Rpb25faGFzaCI6IjIwY2U1MDZjODA4ZGJiMjg1Y2NlNTU2MGY4ZWIwMzg3YzA2OTFiNTQyMjZlYmMxMzcxNmEzZmRmZWFjYWNiNTkiLCJleHBlY3RlZF9zdGF0ZV92ZXJzaW9uIjo3LCJleHBpcmVzX2F0X21zIjoxNzAwMDAwMDA1MDAwLCJwYWNrZXRfaWQiOiIxMjNlNDU2Ny1lODliLTQyZDMtYTQ1Ni00MjY2MTQxNzQwMDAiLCJ0eXAiOiJtZXRhLWNvcmUvY2FwYWJpbGl0eS92MSJ9.RRgffFEzXMHncmQge3_I6hA2nkfG6Rw6dMuDBDIOx_o
```

## Alternatives considered

| Option | Why not |
|---|---|
| Ed25519 signature (verify-only Execution Plane) | Better key separation, but the standard library has no Ed25519 and this reference core is stdlib + pydantic. Revisit when `cryptography` is allowed in the sidecar |
| Pessimistic locks held from Check to Execute | Holds a lock across an LLM-latency window; deadlock and starvation risk |
| Re-validate the whole packet at execution time | Duplicates the Validator in the Execution Plane and still has a window |
| **HMAC capability + CAS (chosen)** | Cheap, stateless verification, no held locks |

## Consequences and residual risk

- **The CAS protects database state only; the outbox (amendment below) records the
  intent atomically but does not make the outside world transactional.** A physical or external side effect (an
  HTTP call, a valve) outside the SQLite transaction is not covered. Such effects
  must be emitted through an outbox after commit, keyed by `packet_id`, and the
  actuator must check the state itself. This risk is **not** closed by this ADR.
- The key is shared by the sidecar and the Execution Plane and must never reach the
  Reasoning Plane. Provisioning and rotation are open questions.
- Expiry uses the wall clock; the design assumes both planes share a host (Docker
  Compose, [ADR-0007](0007-multi-container-deployment.md)). Cross-host skew is not
  handled.
- The Redis path needs the same CAS (a Lua script, or `WATCH`/`MULTI`). It is
  specified here and **not implemented or tested**.
- Under at-least-once delivery, a redelivery of a committed packet returns
  `ALREADY_APPLIED`; this is also a partial answer to the BullMQ idempotency-key
  question (`packet_id` is the key).

## Evidence

`tests/test_control_invariants.py::CapabilityTokenTests` and `ToctouTests`:
stale version aborts with no mutation, replay is applied once, an 8-thread race
across separate connections has exactly one winner, a failed mutation rolls back
the bump. The MAC in the vector above matches `openssl dgst -sha256 -mac HMAC`.

## Amendment (v3): transactional outbox for external actions

**Problem.** The CAS protects database state only. An actuator (valve, HTTP call) driven
straight from the planner can act without a committed intent, or act twice on redelivery.

**Decision.** In the *same* `BEGIN IMMEDIATE` transaction as the CAS, the mutation and
the replay record, `execute_with_capability` inserts a row into `execution_outbox`
(`PENDING`):

| Column | Meaning |
|---|---|
| `packet_id` (unique), `scope` | the committed packet |
| `state_version` | the **expected** version the capability was bound to |
| `resulting_version` | `state_version + 1`, the ledger version this commit created |
| `action_json`, `action_hash` | `JCS({tool_name, args})` and its SHA-256 |
| `idempotency_key` (unique) | `SHA-256(JCS([packet_id, expected_state_version]))` |
| `status` | `PENDING → DISPATCHED → ACKED \| FAILED`; `DISPATCHED → PENDING` (lease expired) and `FAILED → PENDING` (retry) allowed; `ACKED` final |

A dispatcher reads `PENDING` rows, marks them `DISPATCHED`, calls the actuator with the
`idempotency_key`, then marks `ACKED` or `FAILED`. A crash between the effect and the
ack leaves the row `DISPATCHED`; after the lease it returns to `PENDING`, and an actuator
that deduplicates on the key does not act again (tested with a simulated actuator).

**API change.** `execute_with_capability` now takes `tool_name` and `args` and
**recomputes** the action hash instead of accepting one from the caller, so the executed
action is provably the one the token was issued for. A mismatching action is rejected
with `action_mismatch`.

**Schema rules.** Insert triggers verify `action_hash` and `idempotency_key` in the
database (the same SQL functions as ADR-0013) and require `PENDING`; a guard blocks
`REPLACE`/duplicates; only `status` and `updated_epoch` may change, along the graph
above; rows are never deleted. Status updates need no SQL functions, so an actuator
process can use a plain connection.

**What it does not do.** The outbox makes the *intent* atomic and replayable. It does
not verify the physical world: the actuator must still check the actual state before
acting (the open-valve-at-6-atm case), and it must really deduplicate on the key. Rows
are created for every committed packet, including DB-only actions; deciding which
rows an actuator handles is the dispatcher's tool registry, not implemented here. The
Redis path has no outbox yet.
