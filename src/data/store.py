"""SQLite access helpers for the reference schema (src/data/schema.sql).

* ``open_writer``         - Execution Plane connection (WAL, synchronous=FULL).
* ``open_trusted_reader`` - RAG / analytics connection that can only read the
  trusted views. SQLite has no GRANT, so the guarantee comes from a read-only
  connection plus an authorizer that denies base-table reads on knowledge_objects
  unless they are made on behalf of an allow-listed view. It protects against
  accidental or injected raw reads through this connection; it is not a boundary
  against code that opens the database file by other means (that boundary is
  ADR-0002: only the Execution Plane holds the file).
* ``insert_knowledge_object`` / ``audit_ko_ids`` - the writer computes ko_id; the
  schema's ``ko_verify_integrity`` trigger re-derives it in the database at INSERT
  time through the SQL function ``sha256_hex`` registered here, and the audit is the
  second line of defence.

SQL functions are per-connection. ``open_writer`` registers them; a connection that
did not cannot insert into the guarded tables (fail-closed).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from src.control.validator import (
    GENESIS_KO_ID,
    Provenance,
    canonicalize,
    compute_fact_key,
    compute_ko_id,
    loads_i_json,
    sha256_hex,
)


class SqliteIntegrityError(RuntimeError):
    """The SQLite build cannot reproduce the canonical forms the triggers rely on."""


SCHEMA_PATH: Final[Path] = Path(__file__).with_name("schema.sql")
_JSON_QUOTE_PROBES: Final[tuple[str, ...]] = (
    "",
    "plain",
    'quote " and backslash \\ and slash /',
    "\b\t\n\f\r",
    "".join(chr(i) for i in range(0x20)),
    "del \x7f, nel \x85",
    "\u2028\u2029\ufeff",
    "\u20ac \U0001f600 \ufb33 \u00e9",
)
TRUSTED_VIEWS: Final[frozenset[str]] = frozenset(
    {"v_trusted_knowledge", "v_trusted_knowledge_current"}
)
RAW_KO_TABLE: Final[str] = "knowledge_objects"


def _sql_sha256_hex(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode("utf-8")
    elif not isinstance(value, bytes):
        raise TypeError("sha256_hex accepts text or blob")
    return sha256_hex(value)


def _sql_jcs_is_canonical(text: str | None) -> int | None:
    """1 if ``text`` is valid JSON that is byte-identical to its RFC 8785 form."""
    if text is None:
        return None
    try:
        return int(canonicalize(loads_i_json(text)).decode("utf-8") == text)
    except (ValueError, RecursionError):
        return 0


def register_integrity_functions(conn: sqlite3.Connection) -> None:
    """Register ``sha256_hex`` and ``jcs_is_canonical`` and self-test ``json_quote``.

    The triggers build canonical JSON text with ``json_quote``; if this SQLite build
    escapes any probe string differently from RFC 8785, refuse to open rather than
    accept a database whose ids cannot be verified.
    """
    conn.create_function("sha256_hex", 1, _sql_sha256_hex, deterministic=True)
    conn.create_function("jcs_is_canonical", 1, _sql_jcs_is_canonical, deterministic=True)
    try:
        for probe in _JSON_QUOTE_PROBES:
            quoted = conn.execute("SELECT json_quote(?)", (probe,)).fetchone()[0]
            if quoted != canonicalize(probe).decode("utf-8"):
                raise SqliteIntegrityError(
                    f"json_quote differs from RFC 8785 on SQLite {sqlite3.sqlite_version}"
                )
    except sqlite3.OperationalError as exc:
        raise SqliteIntegrityError(f"json_quote unavailable: {exc}") from exc


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def open_writer(path: str | Path) -> sqlite3.Connection:
    """Open the read-write connection and apply the schema."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    register_integrity_functions(conn)
    init_schema(conn)
    return conn


def _reader_authorizer(
    action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None
) -> int:
    if action == sqlite3.SQLITE_READ:
        if arg1 == RAW_KO_TABLE and source not in TRUSTED_VIEWS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_trusted_reader(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    conn.set_authorizer(_reader_authorizer)
    return conn


def insert_knowledge_object(
    conn: sqlite3.Connection,
    *,
    subject: str,
    predicate: str,
    obj: JsonValue,
    provenance: Provenance,
    created_epoch: int,
    parent_ko_id: str = GENESIS_KO_ID,
    embedding: bytes | None = None,
) -> str:
    """Insert one immutable version and return its ko_id."""
    fact_key = compute_fact_key(subject, predicate)
    ko_id = compute_ko_id(
        subject=subject,
        predicate=predicate,
        obj=obj,
        provenance=provenance.value,
        parent_ko_id=parent_ko_id,
        created_epoch=created_epoch,
    )
    conn.execute(
        "INSERT INTO knowledge_objects (ko_id, fact_key, parent_ko_id, subject, predicate,"
        " object, embedding, provenance, created_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ko_id,
            fact_key,
            parent_ko_id,
            subject,
            predicate,
            canonicalize(obj).decode("utf-8"),
            embedding,
            provenance.value,
            created_epoch,
        ),
    )
    return ko_id


def audit_ko_ids(conn: sqlite3.Connection) -> list[str]:
    """Return ko_ids whose stored fields do not hash to the stored ko_id/fact_key."""
    bad: list[str] = []
    rows = conn.execute(
        "SELECT ko_id, fact_key, parent_ko_id, subject, predicate, object,"
        " provenance, created_epoch FROM knowledge_objects"
    ).fetchall()
    for ko_id, fact_key, parent, subject, predicate, obj, prov, epoch in rows:
        expected = compute_ko_id(
            subject=subject,
            predicate=predicate,
            obj=loads_i_json(obj),
            provenance=prov,
            parent_ko_id=parent,
            created_epoch=epoch,
        )
        if expected != ko_id or compute_fact_key(subject, predicate) != fact_key:
            bad.append(ko_id)
    return bad
