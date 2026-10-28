-- META-CORE reference schema (SQLite >= 3.38: JSON functions are built in).
--
-- Per-connection SQL functions required by the integrity triggers (registered by
-- src/data/store.py::open_writer): sha256_hex(text) and jcs_is_canonical(text).
-- A connection that has not registered them cannot INSERT into the guarded tables
-- ("no such function"): the failure is closed by construction. Reads, and the
-- status UPDATEs on execution_outbox, do not need them.
--
-- Connection-level settings. journal_mode=WAL is persisted in the database file;
-- the others are per-connection and MUST be set by every writer connection
-- (src/data/store.py::open_writer does this).
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;  -- REPLACE conflict deletes must fire DELETE triggers

-- ---------------------------------------------------------------------------
-- Knowledge Objects: append-only, content-addressed versions (ADR-0013).
--   fact_key = SHA-256(JCS([subject, predicate]))            logical identity
--   ko_id    = SHA-256(JCS({content, provenance, parent_ko_id, created_epoch}))
-- ko_verify_integrity recomputes both ids at INSERT time through the registered
-- sha256_hex function (json_quote matches JCS string escaping on the tested SQLite
-- build; store.open_writer self-tests that at open). store.audit_ko_ids is the second
-- line of defence. The trigger proves an id is a correct derivation of the row, not
-- that the writer was entitled to write it: a writer that can run arbitrary INSERTs
-- can also compute valid hashes. Authorization stays with ADR-0002.
-- parent_ko_id is 64 zeros for the first version of a fact.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_objects (
    ko_id         TEXT    NOT NULL PRIMARY KEY
                  CHECK (length(ko_id) = 64 AND ko_id NOT GLOB '*[^0-9a-f]*'),
    fact_key      TEXT    NOT NULL
                  CHECK (length(fact_key) = 64 AND fact_key NOT GLOB '*[^0-9a-f]*'),
    parent_ko_id  TEXT    NOT NULL
                  CHECK (length(parent_ko_id) = 64 AND parent_ko_id NOT GLOB '*[^0-9a-f]*'),
    subject       TEXT    NOT NULL CHECK (length(subject) > 0),
    predicate     TEXT    NOT NULL CHECK (length(predicate) > 0),
    object        TEXT    NOT NULL,   -- JCS-canonical JSON text
    embedding     BLOB,               -- supplied at insert time; rows are immutable
    provenance    TEXT    NOT NULL
                  CHECK (provenance IN ('ORGANIC_USER', 'SYNTHETIC_AGENT', 'VERIFIED_EXTERNAL')),
    created_epoch INTEGER NOT NULL
                  CHECK (typeof(created_epoch) = 'integer'
                         AND created_epoch >= 0 AND created_epoch <= 9007199254740991),
    UNIQUE (fact_key, parent_ko_id)   -- a version has at most one child: no forks
);

CREATE INDEX IF NOT EXISTS idx_ko_fact ON knowledge_objects (fact_key, created_epoch);

-- In-database hash verification (the JCS form built here is the one compute_fact_key /
-- compute_ko_id hash in Python; object must already be canonical JSON text).
CREATE TRIGGER IF NOT EXISTS ko_verify_integrity
BEFORE INSERT ON knowledge_objects
WHEN NEW.fact_key IS NOT sha256_hex(
         '[' || json_quote(NEW.subject) || ',' || json_quote(NEW.predicate) || ']')
  OR NEW.ko_id IS NOT sha256_hex(
         '{"content":{"object":' || NEW.object
         || ',"predicate":' || json_quote(NEW.predicate)
         || ',"subject":' || json_quote(NEW.subject)
         || '},"created_epoch":' || NEW.created_epoch
         || ',"parent_ko_id":"' || NEW.parent_ko_id
         || '","provenance":"' || NEW.provenance || '"}')
  OR NOT jcs_is_canonical(NEW.object)
BEGIN
    SELECT RAISE(ABORT, 'Cryptographic integrity violation: invalid ko_id or fact_key');
END;

-- Guards INSERT (incl. INSERT OR REPLACE / OR IGNORE, which would otherwise
-- bypass the UPDATE/DELETE triggers).
CREATE TRIGGER IF NOT EXISTS ko_guard_insert
BEFORE INSERT ON knowledge_objects
WHEN EXISTS (SELECT 1 FROM knowledge_objects
             WHERE ko_id = NEW.ko_id
                OR (fact_key = NEW.fact_key AND parent_ko_id = NEW.parent_ko_id))
BEGIN
    SELECT RAISE(ABORT, 'knowledge_objects is append-only: duplicate or forked version');
END;

CREATE TRIGGER IF NOT EXISTS ko_guard_parent
BEFORE INSERT ON knowledge_objects
WHEN NEW.parent_ko_id <> '0000000000000000000000000000000000000000000000000000000000000000'
 AND NOT EXISTS (SELECT 1 FROM knowledge_objects
                 WHERE ko_id = NEW.parent_ko_id AND fact_key = NEW.fact_key)
BEGIN
    SELECT RAISE(ABORT, 'parent_ko_id must be an existing version of the same fact');
END;

CREATE TRIGGER IF NOT EXISTS ko_no_update
BEFORE UPDATE ON knowledge_objects
BEGIN
    SELECT RAISE(ABORT, 'knowledge_objects is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS ko_no_delete
BEFORE DELETE ON knowledge_objects
BEGIN
    SELECT RAISE(ABORT, 'knowledge_objects is append-only: DELETE forbidden');
END;

-- Read path for RAG / analytics / adaptation. Raw knowledge_objects must not be
-- read from that path; store.open_trusted_reader enforces it with an authorizer.
CREATE VIEW IF NOT EXISTS v_trusted_knowledge AS
SELECT ko_id, fact_key, parent_ko_id, subject, predicate, object,
       embedding, provenance, created_epoch
FROM knowledge_objects
WHERE provenance IN ('ORGANIC_USER', 'VERIFIED_EXTERNAL');

-- Latest trusted version of each fact (ties broken by ko_id for determinism).
CREATE VIEW IF NOT EXISTS v_trusted_knowledge_current AS
SELECT k.ko_id, k.fact_key, k.parent_ko_id, k.subject, k.predicate, k.object,
       k.embedding, k.provenance, k.created_epoch
FROM knowledge_objects AS k
WHERE k.provenance IN ('ORGANIC_USER', 'VERIFIED_EXTERNAL')
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_objects AS n
      WHERE n.fact_key = k.fact_key
        AND n.provenance IN ('ORGANIC_USER', 'VERIFIED_EXTERNAL')
        AND (n.created_epoch > k.created_epoch
             OR (n.created_epoch = k.created_epoch AND n.ko_id > k.ko_id)));

-- ---------------------------------------------------------------------------
-- State ledger: one head row per scope. state_version only moves forward by 1,
-- and only through a compare-and-swap (src/execution/state_ledger.py) (ADR-0016).
-- state_root is a hash-chain head, SHA-256(JCS([prev_root, packet_id, action_hash])).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_ledger (
    scope         TEXT    NOT NULL PRIMARY KEY CHECK (length(scope) > 0),
    state_version INTEGER NOT NULL
                  CHECK (typeof(state_version) = 'integer' AND state_version >= 0),
    state_root    TEXT    NOT NULL
                  CHECK (length(state_root) = 64 AND state_root NOT GLOB '*[^0-9a-f]*'),
    updated_epoch INTEGER NOT NULL CHECK (typeof(updated_epoch) = 'integer')
);

CREATE TRIGGER IF NOT EXISTS ledger_guard_insert
BEFORE INSERT ON state_ledger
WHEN NEW.state_version <> 0
  OR EXISTS (SELECT 1 FROM state_ledger WHERE scope = NEW.scope)
BEGIN
    SELECT RAISE(ABORT, 'state_ledger: scope must start at version 0 and exist once');
END;

CREATE TRIGGER IF NOT EXISTS ledger_monotonic_update
BEFORE UPDATE ON state_ledger
WHEN NEW.scope <> OLD.scope OR NEW.state_version <> OLD.state_version + 1
BEGIN
    SELECT RAISE(ABORT, 'state_ledger: state_version must advance by exactly 1');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON state_ledger
BEGIN
    SELECT RAISE(ABORT, 'state_ledger: DELETE forbidden');
END;

-- Applied packets: replay / at-least-once protection, append-only.
CREATE TABLE IF NOT EXISTS applied_packets (
    packet_id         TEXT    NOT NULL PRIMARY KEY CHECK (length(packet_id) = 36),
    scope             TEXT    NOT NULL,
    resulting_version INTEGER NOT NULL CHECK (resulting_version >= 1),
    action_hash       TEXT    NOT NULL
                      CHECK (length(action_hash) = 64 AND action_hash NOT GLOB '*[^0-9a-f]*'),
    applied_epoch     INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS packets_guard_insert
BEFORE INSERT ON applied_packets
WHEN EXISTS (SELECT 1 FROM applied_packets WHERE packet_id = NEW.packet_id)
BEGIN
    SELECT RAISE(ABORT, 'applied_packets is append-only: duplicate packet_id');
END;

CREATE TRIGGER IF NOT EXISTS packets_no_update
BEFORE UPDATE ON applied_packets
BEGIN
    SELECT RAISE(ABORT, 'applied_packets is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS packets_no_delete
BEFORE DELETE ON applied_packets
BEGIN
    SELECT RAISE(ABORT, 'applied_packets is append-only: DELETE forbidden');
END;

-- ---------------------------------------------------------------------------
-- Transactional outbox (ADR-0016): the intent to act outside the database is
-- written in the SAME transaction as the CAS commit, so an external actuator can
-- never run a command that was not atomically committed, and it deduplicates a
-- redelivery on idempotency_key = SHA-256(JCS([packet_id, expected_state_version])).
--   state_version  = expected_state_version the packet was validated against
--   resulting_version = state_version + 1, the ledger version this commit created
--   action_json    = JCS({tool_name, args}); action_hash = SHA-256(action_json)
-- Status moves PENDING -> DISPATCHED -> ACKED | FAILED; DISPATCHED -> PENDING (lease
-- expired) and FAILED -> PENDING (retry) are allowed; ACKED is final. Rows are never
-- deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_outbox (
    outbox_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope             TEXT    NOT NULL CHECK (length(scope) > 0),
    state_version     INTEGER NOT NULL
                      CHECK (typeof(state_version) = 'integer' AND state_version >= 0),
    resulting_version INTEGER NOT NULL CHECK (resulting_version = state_version + 1),
    packet_id         TEXT    NOT NULL UNIQUE
                      CHECK (length(packet_id) = 36 AND packet_id NOT GLOB '*[^0-9a-f-]*'),
    action_hash       TEXT    NOT NULL
                      CHECK (length(action_hash) = 64 AND action_hash NOT GLOB '*[^0-9a-f]*'),
    action_json       TEXT    NOT NULL CHECK (length(action_json) > 0),
    idempotency_key   TEXT    NOT NULL UNIQUE
                      CHECK (length(idempotency_key) = 64 AND idempotency_key NOT GLOB '*[^0-9a-f]*'),
    status            TEXT    NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'DISPATCHED', 'ACKED', 'FAILED')),
    created_epoch     INTEGER NOT NULL CHECK (typeof(created_epoch) = 'integer'),
    updated_epoch     INTEGER NOT NULL CHECK (typeof(updated_epoch) = 'integer')
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON execution_outbox (status, outbox_id);

CREATE TRIGGER IF NOT EXISTS outbox_verify_integrity
BEFORE INSERT ON execution_outbox
WHEN NEW.status IS NOT 'PENDING'
  OR NEW.action_hash IS NOT sha256_hex(NEW.action_json)
  OR NEW.idempotency_key IS NOT sha256_hex('["' || NEW.packet_id || '",' || NEW.state_version || ']')
  OR NOT jcs_is_canonical(NEW.action_json)
BEGIN
    SELECT RAISE(ABORT, 'execution_outbox: integrity violation (status, action_hash or idempotency_key)');
END;

CREATE TRIGGER IF NOT EXISTS outbox_guard_insert
BEFORE INSERT ON execution_outbox
WHEN EXISTS (SELECT 1 FROM execution_outbox
             WHERE packet_id = NEW.packet_id OR idempotency_key = NEW.idempotency_key)
BEGIN
    SELECT RAISE(ABORT, 'execution_outbox: duplicate packet_id or idempotency_key');
END;

CREATE TRIGGER IF NOT EXISTS outbox_immutable_columns
BEFORE UPDATE ON execution_outbox
WHEN NEW.outbox_id IS NOT OLD.outbox_id OR NEW.scope IS NOT OLD.scope
  OR NEW.state_version IS NOT OLD.state_version
  OR NEW.resulting_version IS NOT OLD.resulting_version
  OR NEW.packet_id IS NOT OLD.packet_id OR NEW.action_hash IS NOT OLD.action_hash
  OR NEW.action_json IS NOT OLD.action_json
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_epoch IS NOT OLD.created_epoch
  OR NEW.updated_epoch < OLD.updated_epoch
BEGIN
    SELECT RAISE(ABORT, 'execution_outbox: only status and updated_epoch may change');
END;

CREATE TRIGGER IF NOT EXISTS outbox_status_transitions
BEFORE UPDATE OF status ON execution_outbox
WHEN NOT (   (OLD.status = 'PENDING'    AND NEW.status = 'DISPATCHED')
          OR (OLD.status = 'DISPATCHED' AND NEW.status IN ('ACKED', 'FAILED', 'PENDING'))
          OR (OLD.status = 'FAILED'     AND NEW.status = 'PENDING'))
BEGIN
    SELECT RAISE(ABORT, 'execution_outbox: illegal status transition');
END;

CREATE TRIGGER IF NOT EXISTS outbox_no_delete
BEFORE DELETE ON execution_outbox
BEGIN
    SELECT RAISE(ABORT, 'execution_outbox: DELETE forbidden');
END;
