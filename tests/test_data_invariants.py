"""Data-layer invariants: append-only KO store, trusted read path, packet schema.

``jsonschema`` is NOT a project dependency; the tests that use it are skipped
when it is not installed.
"""

import importlib.util
import json
import os
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.control import validator as v  # noqa: E402
from src.control.validator import GENESIS_KO_ID, Provenance  # noqa: E402
from src.data import store  # noqa: E402
from src.data.store import (  # noqa: E402
    audit_ko_ids,
    insert_knowledge_object,
    open_trusted_reader,
    open_writer,
)

SCHEMA = json.loads((ROOT / "schemas" / "decision_packet.schema.json").read_text(encoding="utf-8"))
PID = "123e4567-e89b-42d3-a456-426614174000"
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "core.db")
        self.conn = open_writer(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def put(self, obj, prov=Provenance.ORGANIC_USER, epoch=100, parent=GENESIS_KO_ID, subject="pump-1", predicate="state"):
        return insert_knowledge_object(
            self.conn, subject=subject, predicate=predicate, obj=obj, provenance=prov, created_epoch=epoch, parent_ko_id=parent
        )

    def drop_integrity_trigger(self):
        """Isolate the other rules: remove the in-database hash check for one test."""
        self.conn.execute("DROP TRIGGER ko_verify_integrity")

    def test_writer_pragmas(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

    def test_ko_id_binds_provenance_content_parent_and_time(self):
        base = dict(subject="s", predicate="p", obj={"v": 1}, provenance="ORGANIC_USER", parent_ko_id=GENESIS_KO_ID, created_epoch=1)
        ids = {
            v.compute_ko_id(**base),
            v.compute_ko_id(**{**base, "provenance": "SYNTHETIC_AGENT"}),
            v.compute_ko_id(**{**base, "obj": {"v": 2}}),
            v.compute_ko_id(**{**base, "created_epoch": 2}),
            v.compute_ko_id(**{**base, "parent_ko_id": "1" * 64}),
        }
        self.assertEqual(len(ids), 5)
        # fact_key ignores the value and the provenance: it is the logical identity.
        self.assertEqual(v.compute_fact_key("s", "p"), v.compute_fact_key("s", "p"))

    def test_rows_cannot_be_updated_or_deleted(self):
        ko = self.put({"v": 1})
        for sql in (
            "UPDATE knowledge_objects SET provenance = 'ORGANIC_USER'",
            "UPDATE knowledge_objects SET ko_id = '" + "a" * 64 + "'",
            "UPDATE knowledge_objects SET object = '2'",
            "DELETE FROM knowledge_objects",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        self.assertEqual(self.conn.execute("SELECT ko_id FROM knowledge_objects").fetchall(), [(ko,)])

    def test_replace_and_ignore_cannot_bypass_append_only(self):
        # Risk: INSERT OR REPLACE deletes the old row without firing a DELETE trigger by default.
        self.drop_integrity_trigger()  # forged copies below would otherwise stop at the hash check
        ko = self.put({"v": 1})
        row = self.conn.execute("SELECT * FROM knowledge_objects").fetchone()
        placeholders = ",".join("?" * len(row))
        forged = list(row)
        forged[7] = "SYNTHETIC_AGENT"
        for verb in ("INSERT OR REPLACE", "REPLACE", "INSERT OR IGNORE"):
            with self.subTest(verb=verb), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(f"{verb} INTO knowledge_objects VALUES ({placeholders})", forged)
        self.assertEqual(self.conn.execute("SELECT provenance FROM knowledge_objects WHERE ko_id = ?", (ko,)).fetchone()[0], "ORGANIC_USER")

    def test_constraints_reject_bad_values(self):
        self.drop_integrity_trigger()  # rows below have made-up ids; test the CHECK/parent rules
        good = self.put({"v": 1})
        bad_rows = [
            ("bad provenance", ("b" * 64, "c" * 64, GENESIS_KO_ID, "s", "p", "1", None, "ADMIN", 1)),
            ("short ko_id", ("abc", "c" * 64, GENESIS_KO_ID, "s", "p", "1", None, "ORGANIC_USER", 1)),
            ("uppercase hex", ("A" * 64, "c" * 64, GENESIS_KO_ID, "s", "p", "1", None, "ORGANIC_USER", 1)),
            ("negative epoch", ("d" * 64, "c" * 64, GENESIS_KO_ID, "s", "p", "1", None, "ORGANIC_USER", -1)),
            ("text epoch", ("e" * 64, "c" * 64, GENESIS_KO_ID, "s", "p", "1", None, "ORGANIC_USER", "x")),
            ("empty subject", ("f" * 64, "c" * 64, GENESIS_KO_ID, "", "p", "1", None, "ORGANIC_USER", 1)),
            ("orphan parent", ("9" * 64, "c" * 64, "8" * 64, "s", "p", "1", None, "ORGANIC_USER", 1)),
        ]
        for name, row in bad_rows:
            with self.subTest(name), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute("INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)", row)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 1)
        self.assertIsNotNone(good)

    def test_version_chain_rules(self):
        first = self.put({"v": 1}, epoch=100)
        second = self.put({"v": 2}, epoch=200, parent=first)
        with self.assertRaises(sqlite3.DatabaseError):  # fork: second child of `first`
            self.put({"v": 3}, epoch=300, parent=first)
        with self.assertRaises(sqlite3.DatabaseError):  # parent belongs to a different fact
            self.put({"v": 1}, epoch=300, parent=second, subject="pump-2")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 2)

    def test_forged_ids_are_blocked_at_insert_by_the_database(self):
        # Risk: a row whose ids are not a correct derivation of its content/provenance.
        fk = v.compute_fact_key("x", "y")
        honest = dict(subject="x", predicate="y", obj={"v": 1}, provenance="ORGANIC_USER",
                      parent_ko_id=GENESIS_KO_ID, created_epoch=5)
        right = v.compute_ko_id(**honest)
        cases = {
            "random ko_id": ("a" * 64, fk, "ORGANIC_USER", 5, '{"v":1}'),
            "wrong fact_key": (right, "b" * 64, "ORGANIC_USER", 5, '{"v":1}'),
            "provenance swapped after hashing": (right, fk, "VERIFIED_EXTERNAL", 5, '{"v":1}'),
            "epoch changed after hashing": (right, fk, "ORGANIC_USER", 6, '{"v":1}'),
            "object changed after hashing": (right, fk, "ORGANIC_USER", 5, '{"v":2}'),
        }
        for name, (ko_id, fact_key, prov, epoch, obj) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(
                sqlite3.IntegrityError, "Cryptographic integrity violation: invalid ko_id or fact_key"
            ):
                self.conn.execute(
                    "INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)",
                    (ko_id, fact_key, GENESIS_KO_ID, "x", "y", obj, None, prov, epoch),
                )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 0)
        # the same row with correct derivations is accepted
        self.conn.execute(
            "INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)",
            (right, fk, GENESIS_KO_ID, "x", "y", '{"v":1}', None, "ORGANIC_USER", 5),
        )

    def test_object_text_must_be_canonical_json(self):
        # Risk: hash computed over a non-canonical spelling of the same value.
        import hashlib

        fk = v.compute_fact_key("x", "y")
        text = '{"v": 1}'  # valid JSON, not RFC 8785
        forged = hashlib.sha256(
            ('{"content":{"object":' + text + ',"predicate":"y","subject":"x"},"created_epoch":5,'
             '"parent_ko_id":"' + GENESIS_KO_ID + '","provenance":"ORGANIC_USER"}').encode()
        ).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)",
                (forged, fk, GENESIS_KO_ID, "x", "y", text, None, "ORGANIC_USER", 5),
            )

    def test_audit_is_the_second_line_when_the_trigger_is_removed(self):
        # A connection with DDL rights can drop the trigger; the audit still finds the forgery.
        honest = self.put({"v": 1})
        self.drop_integrity_trigger()
        self.conn.execute(
            "INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)",
            ("a" * 64, v.compute_fact_key("x", "y"), GENESIS_KO_ID, "x", "y", "1", None, "ORGANIC_USER", 5),
        )
        self.assertEqual(audit_ko_ids(self.conn), ["a" * 64])
        self.assertNotIn(honest, audit_ko_ids(self.conn))

    def test_a_connection_without_the_sql_functions_cannot_insert(self):
        # Fail-closed: only store.open_writer registers sha256_hex / jcs_is_canonical.
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such function"):
                raw.execute(
                    "INSERT INTO knowledge_objects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("a" * 64, "b" * 64, GENESIS_KO_ID, "x", "y", "1", None, "ORGANIC_USER", 1),
                )
            self.assertEqual(raw.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 0)
        finally:
            raw.close()

    def test_sql_side_hashes_agree_with_python_on_random_facts(self):
        # Risk: the SQL-built canonical form drifts from the Python one on odd input.
        rnd = random.Random(9)
        alphabet = ['a', 'Z', ' ', '"', '\\', '/', '\n', '\t', '\x01', '\x1f', '\x7f',
                    '\u00e9', '\u2028', '\u20ac', '\U0001f600', '\ufb33', '\ufeff']

        def text():
            return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 8)))

        def value(depth=0):
            kind = rnd.choice(["s", "i", "f", "b", "n", "l", "o"] if depth < 3 else ["s", "i", "f", "b", "n"])
            return {
                "s": text, "i": lambda: rnd.randint(-2**53 + 1, 2**53 - 1),
                "f": lambda: rnd.uniform(-1e9, 1e9) * 10 ** rnd.randint(-12, 12),
                "b": lambda: rnd.random() < 0.5, "n": lambda: None,
                "l": lambda: [value(depth + 1) for _ in range(rnd.randint(0, 3))],
                "o": lambda: {text(): value(depth + 1) for _ in range(rnd.randint(0, 3))},
            }[kind]()

        for i in range(300):
            self.put(value(), rnd.choice(list(Provenance)), epoch=rnd.randint(0, 2**53 - 1),
                     subject=text(), predicate=text() + str(i))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 300)
        self.assertEqual(audit_ko_ids(self.conn), [])

    def test_large_float_values_survive_insert_and_audit(self):
        # Regression: canonical text '-38978575049818260' must verify in SQL and in the audit.
        ko = self.put({"x": -3.897857504981826e16, "y": 1e21})
        self.assertEqual(audit_ko_ids(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT object FROM knowledge_objects WHERE ko_id=?", (ko,)).fetchone()[0],
                         '{"x":-38978575049818260,"y":1e+21}')

    def test_open_refuses_a_sqlite_whose_json_quote_differs_from_rfc8785(self):
        with mock.patch.object(store, "canonicalize", return_value=b'"different"'):
            with self.assertRaises(store.SqliteIntegrityError):
                open_writer(os.path.join(self.tmp.name, "other.db")).close()

    def test_trusted_reader_hides_raw_table_and_synthetic_rows(self):
        organic = self.put({"v": 1}, Provenance.ORGANIC_USER, subject="a")
        self.put({"v": 2}, Provenance.SYNTHETIC_AGENT, subject="b")
        external = self.put({"v": 3}, Provenance.VERIFIED_EXTERNAL, subject="c")
        reader = open_trusted_reader(self.path)
        try:
            rows = {r[0] for r in reader.execute("SELECT ko_id FROM v_trusted_knowledge")}
            self.assertEqual(rows, {organic, external})
            for sql in (
                "SELECT * FROM knowledge_objects",
                "SELECT ko_id FROM knowledge_objects WHERE provenance = 'SYNTHETIC_AGENT'",
                "INSERT INTO knowledge_objects SELECT * FROM v_trusted_knowledge",
                "DELETE FROM knowledge_objects",
                "ATTACH DATABASE ':memory:' AS other",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    reader.execute(sql)
        finally:
            reader.close()

    def test_current_view_returns_latest_trusted_version_and_ignores_synthetic_overrides(self):
        v1 = self.put({"v": 1}, epoch=100)
        v2 = self.put({"v": 2}, epoch=200, parent=v1)
        self.put({"v": 999}, Provenance.SYNTHETIC_AGENT, epoch=300, parent=v2)  # agent override
        reader = open_trusted_reader(self.path)
        try:
            rows = reader.execute("SELECT ko_id, object FROM v_trusted_knowledge_current").fetchall()
            self.assertEqual(rows, [(v2, '{"v":2}')])
            self.assertEqual(reader.execute("SELECT count(*) FROM v_trusted_knowledge").fetchone()[0], 2)
        finally:
            reader.close()


# --------------------------------------------------------------------------- #
# Decision Packet schema <-> pydantic model
# --------------------------------------------------------------------------- #


def packet(**over):
    base = {
        "packet_id": PID,
        "parent_state_version": 3,
        "provenance": "SYNTHETIC_AGENT",
        "tool_call": {"tool_name": "ledger.adjust", "args": {"sku": "META-001", "delta": -10}},
        "lejdas_attestation": None,
        "capability_token": None,
    }
    base.update(over)
    return base


ATTESTATION_L0 = {
    "legal": "PASS", "ethical": "PASS", "jurisdictional": "PASS", "duty": "PASS",
    "accountability": "PASS", "safety": "PASS", "escalation_level": "L0",
    "validator_version": "ref-0", "evaluated_at_ms": 1,
}
TOKEN = "v1.e30." + "A" * 43


class SchemaConsistencyTests(unittest.TestCase):
    def test_schema_declares_draft_2020_12(self):
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_schema_and_model_have_the_same_fields_and_required_set(self):
        model_fields = set(v.DecisionPacket.model_fields)
        self.assertEqual(set(SCHEMA["properties"]), model_fields)
        model_required = {n for n, f in v.DecisionPacket.model_fields.items() if f.is_required()}
        self.assertEqual(set(SCHEMA["required"]), model_required)
        self.assertFalse(SCHEMA["additionalProperties"])

    def test_enums_and_patterns_are_shared(self):
        defs = SCHEMA["$defs"]
        self.assertEqual(set(defs["provenance"]["enum"]), {p.value for p in v.Provenance})
        self.assertEqual(set(defs["check_result"]["enum"]), {c.value for c in v.CheckResult})
        self.assertEqual(set(defs["lejdas_attestation"]["properties"]["escalation_level"]["enum"]), {e.value for e in v.EscalationLevel})
        self.assertEqual(SCHEMA["properties"]["packet_id"]["pattern"], v._UUID_PATTERN)
        token_branch = [b for b in SCHEMA["properties"]["capability_token"]["oneOf"] if b.get("type") == "string"][0]
        self.assertEqual(token_branch["pattern"], v._TOKEN_PATTERN)

    def test_model_accepts_and_rejects_the_same_basic_packets(self):
        v.DecisionPacket.model_validate(packet())
        v.DecisionPacket.model_validate(packet(lejdas_attestation=ATTESTATION_L0, capability_token=TOKEN))
        for bad in (
            packet(parent_state_version=-1),
            packet(provenance="ADMIN"),
            packet(packet_id="not-a-uuid"),
            packet(extra_field=1),
            packet(tool_call={"tool_name": "Bad Name", "args": {}}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                v.DecisionPacket.model_validate(bad)


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed (optional verification only)")
class JsonSchemaValidationTests(unittest.TestCase):
    def setUp(self):
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(SCHEMA)
        self.validator = jsonschema.Draft202012Validator(SCHEMA)

    def ok(self, doc):
        return not list(self.validator.iter_errors(doc))

    def test_valid_packets(self):
        self.assertTrue(self.ok(packet()))
        self.assertTrue(self.ok(packet(lejdas_attestation=ATTESTATION_L0, capability_token=TOKEN, justification="why")))

    def test_token_requires_an_l0_attestation(self):
        self.assertFalse(self.ok(packet(capability_token=TOKEN)))  # token without attestation
        self.assertFalse(self.ok(packet(capability_token=TOKEN, lejdas_attestation={**ATTESTATION_L0, "escalation_level": "L1"})))
        self.assertTrue(self.ok(packet(lejdas_attestation={**ATTESTATION_L0, "escalation_level": "L2"})))  # attested, no token

    def test_schema_rejects_malformed_packets_like_the_model_does(self):
        for bad in (
            packet(parent_state_version=-1),
            packet(provenance="ADMIN"),
            packet(packet_id="not-a-uuid"),
            packet(extra_field=1),
            packet(tool_call={"tool_name": "Bad Name", "args": {}}),
            packet(capability_token="v1.x"),
        ):
            with self.subTest(bad=bad):
                self.assertFalse(self.ok(bad))
        missing = packet()
        del missing["capability_token"]
        self.assertFalse(self.ok(missing))


if __name__ == "__main__":
    unittest.main()
