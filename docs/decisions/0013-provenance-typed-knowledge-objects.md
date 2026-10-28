# ADR-0013: Provenance bound into Knowledge Object identity

**Status:** Accepted — revision 2, amended in v3 (in-database verification). Supersedes the earlier definition
`id = SHA-256(subject || predicate)` (see "Identity model").

## Context

Revision 1 typed `provenance` as an enum but relied on every reader remembering a
`WHERE provenance = ...` filter, and left the field outside the object's identity.
A field that is not part of the id can be rewritten in place, and one forgotten
filter lets agent-authored facts feed analytics and adaptation (model
autophagy).

## Decision

### Identity model

| Name | Formula | Role |
|---|---|---|
| `fact_key` | `SHA-256(JCS([subject, predicate]))` | Logical identity of a fact; what the earlier text called `id`. Deduplication and lookup |
| `ko_id` | `SHA-256(JCS({content: {subject, predicate, object}, provenance, parent_ko_id, created_epoch}))` | Immutable id of one **version** of a fact |

`||` in the formulas means an injective encoding (a JCS object or array), not raw
concatenation, so field boundaries cannot be shifted. A new value for a known fact
is a **new row** whose `parent_ko_id` is the previous version (64 zeros for the
first). `UNIQUE (fact_key, parent_ko_id)` forbids forks.

### Enforcement ([`src/data/schema.sql`](../../src/data/schema.sql))

- `CHECK (provenance IN ('ORGANIC_USER','SYNTHETIC_AGENT','VERIFIED_EXTERNAL'))`,
  hex-format checks on ids, integer check on `created_epoch`.
- `BEFORE UPDATE` and `BEFORE DELETE` triggers abort: the table is append-only.
- A `BEFORE INSERT` guard aborts duplicate ids and forks. Without it
  `INSERT OR REPLACE` removes the old row and bypasses the delete trigger.
  `recursive_triggers = ON` is set as a second layer.
- A parent must exist and belong to the same `fact_key`.
- `v_trusted_knowledge` (all trusted versions) and
  `v_trusted_knowledge_current` (latest trusted version per fact) expose only
  `ORGANIC_USER` and `VERIFIED_EXTERNAL` rows.
- RAG and analytics read through `open_trusted_reader`
  ([`src/data/store.py`](../../src/data/store.py)): a read-only connection whose
  SQLite authorizer denies reads of `knowledge_objects` unless made on behalf of
  a trusted view.
- Provenance comes from the **ingress channel**, never from the agent's claim:
  `require_agent_provenance` rejects an agent packet that declares anything but
  `SYNTHETIC_AGENT`.

## Alternatives considered

| Option | Why not |
|---|---|
| Revision 1: enum plus a read-side filter convention | One forgotten filter defeats it; provenance mutable |
| One physical table per provenance class | Strongest separation, but cross-class queries need `UNION` and double the schema; revisit if triggers prove insufficient |
| **Provenance in the id, append-only triggers, trusted views and a restricted reader (chosen)** | Several cheap layers, all testable |

## Limits (stated, not hidden)

- **SQLite has no built-in SHA-256** (this limit was lifted in v3: see the amendment
  below, where a registered SQL function lets the database check `ko_id` at INSERT).
  The writer computes the ids and `audit_ko_ids` re-derives them as the second line;
  with the trigger removed a forged id inserts and the audit finds it (there is a test).
- **DDL cannot forbid reads.** SQLite has no `GRANT`. The authorizer protects the
  connection it is installed on; it is not a boundary against code that opens the
  file another way. That boundary is [ADR-0002](0002-execution-plane-exclusive-db-access.md)
  (only the Execution Plane holds the file).
- Triggers do not stop `DROP TRIGGER` / `DROP TABLE` by a connection with DDL
  rights; the same file-level isolation applies.
- `created_epoch` is part of `ko_id`, so re-asserting an identical value at a new
  time creates a new version. A no-op policy ("skip if the head already equals the
  new content") is not implemented and is an open question.
- `embedding` is fixed at insert time (rows are immutable); where re-embedding
  lives is open.

## Evidence

`tests/test_data_invariants.py::KnowledgeStoreTests` covers each rule above and was
mutation-checked: removing the insert guard, the update trigger, the parent
guard, the authorizer check or the view filter each fails a test.

## Amendment (v3): ids are verified inside the database

Revision 2 said SQLite "cannot compute SHA-256". It can, through a SQL function
registered per connection.

- `open_writer` registers `sha256_hex(text)` and `jcs_is_canonical(text)`. The trigger
  `ko_verify_integrity` (`BEFORE INSERT`) rebuilds the canonical text of the row in SQL
  and aborts with `Cryptographic integrity violation: invalid ko_id or fact_key` if
  `fact_key` or `ko_id` is not the correct derivation, or if `object` is not already
  canonical JSON. A forged or stale id is refused at the door; `audit_ko_ids` stays as
  the second line.
- The SQL form relies on `json_quote` escaping strings exactly like RFC 8785. That was
  checked on SQLite 3.45.1 (30 000 random strings, 0 differences) and is **self-tested at
  every `open_writer`** on a probe set; a SQLite build that differs refuses to open.
  SQLite ≥ 3.38 (JSON built in) is required; other versions are untested.
- `created_epoch` is limited to the IEEE-754 safe range so SQL and Python agree.

**What this does and does not give.**

- It stops wrong, stale, edited or non-canonical ids. It does **not** stop a writer that
  can run arbitrary `INSERT`s from computing a valid hash for a false claim: that is an
  authorization problem, answered by ADR-0002 and by assigning provenance from the
  ingress channel, not by hashing.
- A connection without the functions cannot insert (`no such function`): fail-closed.
- A connection with DDL rights can drop the trigger. `open_writer` re-creates a missing
  trigger but does not detect a *modified* one (`CREATE TRIGGER IF NOT EXISTS`); a schema
  drift check is an open item.

**A bug the fuzz test found (it also affected the v2 audit).** The canonical text of a
large float, e.g. `-38978575049818260`, parsed with `json.loads` as an out-of-range
integer and failed to re-canonicalize. `loads_i_json` now reads numbers the way
RFC 8785 does; the SQL function and `audit_ko_ids` use it, with regression tests.
