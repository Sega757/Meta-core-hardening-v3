"""Transactional outbox tests (ADR-0016): atomic intent, dedupe key, status graph."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.control import validator as v  # noqa: E402
from src.data.store import open_writer  # noqa: E402
from src.execution.state_ledger import (  # noqa: E402
    ExecutionOutcome,
    OutboxStatus,
    StaleStateError,
    execute_with_capability,
    fetch_pending_outbox,
    get_outbox,
    read_head,
    register_scope,
    transition_outbox,
)

KEY = bytes(range(32))
NOW = 1_700_000_000_000


def pid(i):
    return f"{i:08x}-e89b-42d3-a456-426614174000"


class OutboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "core.db")
        self.conn = open_writer(self.path)
        self.conn.execute("CREATE TABLE valve (id TEXT PRIMARY KEY, opening INTEGER NOT NULL)")
        self.conn.execute("INSERT INTO valve VALUES ('V-102', 0)")
        register_scope(self.conn, "plant", NOW)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def commit(self, packet_id, expected=0, opening=40, conn=None, mutate=True):
        args = {"valve": "V-102", "opening": opening}
        ah = v.action_hash("valve.set", args)
        token = v.issue_capability_token(
            secret_key=KEY, packet_id=packet_id, expected_state_version=expected,
            action_hash=ah, now_ms=NOW, ttl_ms=5000,
        )

        def mutation(c):
            if mutate:
                c.execute("UPDATE valve SET opening = ? WHERE id = 'V-102'", (opening,))

        return execute_with_capability(
            conn or self.conn, secret_key=KEY, token=token, packet_id=packet_id, scope="plant",
            expected_state_version=expected, tool_name="valve.set", args=args, now_ms=NOW + 1,
            mutation=mutation,
        )

    def opening(self):
        return self.conn.execute("SELECT opening FROM valve").fetchone()[0]

    def count_outbox(self):
        return self.conn.execute("SELECT count(*) FROM execution_outbox").fetchone()[0]


class OutboxCommitTests(OutboxBase):
    def test_commit_writes_one_pending_intent_with_a_derivable_key(self):
        self.assertIs(self.commit(pid(1)), ExecutionOutcome.APPLIED)
        row = get_outbox(self.conn, pid(1))
        self.assertEqual(row.status, OutboxStatus.PENDING)
        self.assertEqual((row.scope, row.state_version), ("plant", 0))
        # independent derivations (stdlib only)
        expected_key = hashlib.sha256(json.dumps([pid(1), 0], separators=(",", ":")).encode()).hexdigest()
        action_json = json.dumps({"args": {"opening": 40, "valve": "V-102"}, "tool_name": "valve.set"},
                                 separators=(",", ":"))
        self.assertEqual(row.idempotency_key, expected_key)
        self.assertEqual(row.action_json, action_json)
        self.assertEqual(row.action_hash, hashlib.sha256(action_json.encode()).hexdigest())
        self.assertEqual(self.conn.execute("SELECT resulting_version FROM execution_outbox").fetchone()[0], 1)

    def test_idempotency_key_binds_packet_and_expected_version_unambiguously(self):
        a = v.compute_idempotency_key("ab", 1)
        self.assertNotEqual(a, v.compute_idempotency_key("a", "b1"))
        self.assertNotEqual(a, v.compute_idempotency_key("ab", 2))
        self.assertEqual(a, v.compute_idempotency_key("ab", 1))

    def test_intent_commits_atomically_with_the_state_change(self):
        # Risk: the ledger moves but the external command is lost (or the reverse).
        self.conn.execute("ALTER TABLE execution_outbox RENAME TO outbox_gone")  # make the insert fail
        with self.assertRaises(sqlite3.DatabaseError):
            self.commit(pid(1))
        self.assertEqual(self.opening(), 0)
        self.assertEqual(read_head(self.conn, "plant")[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM applied_packets").fetchone()[0], 0)

    def test_no_intent_without_a_commit(self):
        self.commit(pid(1))
        with self.assertRaises(StaleStateError):
            self.commit(pid(2), expected=0, opening=90)  # stale
        self.assertEqual(self.count_outbox(), 1)
        self.assertEqual(self.opening(), 40)

    def test_redelivery_does_not_create_a_second_intent(self):
        self.commit(pid(1))
        self.assertIs(self.commit(pid(1)), ExecutionOutcome.ALREADY_APPLIED)
        self.assertEqual(self.count_outbox(), 1)

    def test_pending_intents_come_out_in_commit_order(self):
        for i in range(3):
            self.commit(pid(i + 1), expected=i, opening=10 * (i + 1))
        rows = fetch_pending_outbox(self.conn)
        self.assertEqual([r.packet_id for r in rows], [pid(1), pid(2), pid(3)])
        self.assertEqual([r.state_version for r in rows], [0, 1, 2])
        self.assertEqual(len(fetch_pending_outbox(self.conn, limit=2)), 2)


class FakeActuator:
    """External device that deduplicates on the idempotency key."""

    def __init__(self):
        self.effects = []
        self._seen = set()

    def execute(self, idempotency_key, action_json):
        if idempotency_key in self._seen:
            return "duplicate-ignored"
        self._seen.add(idempotency_key)
        self.effects.append(json.loads(action_json))
        return "executed"


class ActuatorFlowTests(OutboxBase):
    def dispatch_once(self, actuator, crash_before_ack=False):
        for row in fetch_pending_outbox(self.conn):
            transition_outbox(self.conn, row.outbox_id, OutboxStatus.DISPATCHED, NOW + 10)
            actuator.execute(row.idempotency_key, row.action_json)
            if crash_before_ack:
                return row
            transition_outbox(self.conn, row.outbox_id, OutboxStatus.ACKED, NOW + 20)

    def test_normal_flow_executes_once_and_ends_acked(self):
        self.commit(pid(1))
        actuator = FakeActuator()
        self.dispatch_once(actuator)
        self.assertEqual(len(actuator.effects), 1)
        self.assertEqual(get_outbox(self.conn, pid(1)).status, OutboxStatus.ACKED)
        self.assertEqual(fetch_pending_outbox(self.conn), [])

    def test_crash_after_the_effect_but_before_the_ack_does_not_act_twice(self):
        # Risk: at-least-once redispatch re-opens the valve.
        self.commit(pid(1))
        actuator = FakeActuator()
        row = self.dispatch_once(actuator, crash_before_ack=True)
        self.assertEqual(get_outbox(self.conn, pid(1)).status, OutboxStatus.DISPATCHED)
        transition_outbox(self.conn, row.outbox_id, OutboxStatus.PENDING, NOW + 30)  # lease expired
        self.dispatch_once(actuator)
        self.assertEqual(len(actuator.effects), 1)  # the second dispatch was deduplicated
        self.assertEqual(get_outbox(self.conn, pid(1)).status, OutboxStatus.ACKED)

    def test_a_plain_connection_can_run_the_actuator_side(self):
        # The dispatcher/actuator process needs no integrity SQL functions for status updates.
        self.commit(pid(1))
        plain = sqlite3.connect(self.path, isolation_level=None)
        try:
            (row,) = fetch_pending_outbox(plain)
            transition_outbox(plain, row.outbox_id, OutboxStatus.DISPATCHED, NOW + 1)
            transition_outbox(plain, row.outbox_id, OutboxStatus.FAILED, NOW + 2)
            transition_outbox(plain, row.outbox_id, OutboxStatus.PENDING, NOW + 3)  # retry
            self.assertEqual(get_outbox(plain, pid(1)).status, OutboxStatus.PENDING)
        finally:
            plain.close()


class OutboxSchemaRuleTests(OutboxBase):
    def row_id(self):
        self.commit(pid(1))
        return get_outbox(self.conn, pid(1)).outbox_id

    def test_illegal_status_transitions_are_rejected(self):
        oid = self.row_id()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "illegal status transition"):
            transition_outbox(self.conn, oid, OutboxStatus.ACKED, NOW)  # PENDING -> ACKED
        with self.assertRaises(sqlite3.IntegrityError):
            transition_outbox(self.conn, oid, OutboxStatus.PENDING, NOW)  # no-op
        transition_outbox(self.conn, oid, OutboxStatus.DISPATCHED, NOW)
        transition_outbox(self.conn, oid, OutboxStatus.ACKED, NOW)
        for target in OutboxStatus:
            with self.subTest(target=target), self.assertRaises(sqlite3.IntegrityError):
                transition_outbox(self.conn, oid, target, NOW)  # ACKED is final

    def test_only_status_and_updated_epoch_may_change(self):
        self.row_id()
        for sql in (
            "UPDATE execution_outbox SET action_json = '{}'",
            "UPDATE execution_outbox SET idempotency_key = '" + "a" * 64 + "'",
            "UPDATE execution_outbox SET packet_id = '" + pid(9) + "'",
            "UPDATE execution_outbox SET state_version = 5, resulting_version = 6",
            "UPDATE execution_outbox SET updated_epoch = 0",
            "DELETE FROM execution_outbox",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)

    def test_forged_intents_are_blocked_at_insert(self):
        good_args = {"valve": "V-102", "opening": 100}
        text = v.canonicalize({"tool_name": "valve.set", "args": good_args}).decode()
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = v.compute_idempotency_key(pid(7), 3)
        base = dict(scope="plant", state_version=3, resulting_version=4, packet_id=pid(7), action_hash=digest,
                    action_json=text, idempotency_key=key, status="PENDING")
        insert = ("INSERT INTO execution_outbox (scope, state_version, resulting_version, packet_id, action_hash,"
                  " action_json, idempotency_key, status, created_epoch, updated_epoch)"
                  " VALUES (:scope,:state_version,:resulting_version,:packet_id,:action_hash,:action_json,"
                  ":idempotency_key,:status,1,1)")
        forged = {
            "wrong key": {**base, "idempotency_key": "b" * 64},
            "key for another version": {**base, "idempotency_key": v.compute_idempotency_key(pid(7), 2)},
            "hash of another action": {**base, "action_hash": "c" * 64},
            "inserted already ACKED": {**base, "status": "ACKED"},
            "non-canonical action text": {**base, "action_json": text.replace(",", ", "),
                                          "action_hash": hashlib.sha256(text.replace(",", ", ").encode()).hexdigest()},
        }
        for name, params in forged.items():
            with self.subTest(name), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(insert, params)
        self.assertEqual(self.count_outbox(), 0)
        self.conn.execute(insert, base)  # the honest row is accepted
        self.assertEqual(self.count_outbox(), 1)
        for verb in ("INSERT OR REPLACE", "INSERT OR IGNORE", "REPLACE"):
            with self.subTest(verb=verb), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(insert.replace("INSERT", verb, 1), {**base, "action_hash": digest})

    def test_a_connection_without_the_functions_cannot_insert_intents(self):
        plain = sqlite3.connect(self.path, isolation_level=None)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such function"):
                plain.execute(
                    "INSERT INTO execution_outbox (scope, state_version, resulting_version, packet_id,"
                    " action_hash, action_json, idempotency_key, status, created_epoch, updated_epoch)"
                    " VALUES ('plant', 0, 1, ?, ?, '{}', ?, 'PENDING', 1, 1)",
                    (pid(5), "a" * 64, "b" * 64),
                )
        finally:
            plain.close()

    def test_status_check_constraint(self):
        oid = self.row_id()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE execution_outbox SET status = 'DONE' WHERE outbox_id = ?", (oid,))


if __name__ == "__main__":
    unittest.main()
