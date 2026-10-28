"""Execution Plane reference: capability-gated compare-and-swap commit (ADR-0016).

The production Execution Plane is Node.js; this module is the executable
specification of the same algorithm. A mutation is applied only if

1. the capability token verifies (signature, expiry, packet, action, version), and
2. the ledger head still equals ``expected_state_version`` (compare-and-swap),

and the version bump, the business mutation, the replay record AND the outbox
intent all commit in ONE transaction. On ``StaleStateError`` the caller aborts and
returns the agent to Perceive.

The action hash is recomputed here from ``tool_name`` and ``args``: the executed
action is the one the token was issued for, not a hash the caller asserts.

Transactional outbox: an external actuator (valve, HTTP call) is driven from
``execution_outbox`` rows, never directly from the planner. A row exists only if the
commit succeeded, and carries ``idempotency_key = SHA-256(JCS([packet_id,
expected_state_version]))`` so a redelivery or a crash-and-retry cannot act twice at
an actuator that deduplicates on that key. The outbox covers the intent; the
actuator must still check the physical state itself (ADR-0016, residual risk).

Only the SQLite path is implemented here. The Redis path (same CAS via a Lua
script or WATCH/MULTI) is specified in ADR-0016 but has no reference code yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Final

from src.control.validator import (
    action_hash as compute_action_hash,
    canonicalize,
    compute_idempotency_key,
    sha256_hex,
    verify_capability_token,
)

GENESIS_STATE_ROOT: Final[str] = sha256_hex(b"meta-core/state-root/genesis")


class ExecutionOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"  # idempotent redelivery of a committed packet


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    ACKED = "ACKED"
    FAILED = "FAILED"


class StaleStateError(Exception):
    """The ledger head moved since planning: ABORT and re-Perceive."""

    def __init__(self, scope: str, expected: int, actual: int) -> None:
        super().__init__(f"scope {scope!r}: expected version {expected}, found {actual}")
        self.scope = scope
        self.expected = expected
        self.actual = actual


class UnknownScopeError(LookupError):
    """No ledger row exists for the scope."""


class ReplayConflictError(Exception):
    """A packet_id was already applied with a different action hash."""


@dataclass(frozen=True)
class OutboxRow:
    outbox_id: int
    scope: str
    state_version: int
    packet_id: str
    action_hash: str
    action_json: str
    idempotency_key: str
    status: OutboxStatus


_OUTBOX_COLUMNS: Final[str] = (
    "outbox_id, scope, state_version, packet_id, action_hash, action_json,"
    " idempotency_key, status"
)


def _outbox_row(row: tuple[Any, ...]) -> OutboxRow:
    return OutboxRow(*row[:7], OutboxStatus(row[7]))


def register_scope(conn: sqlite3.Connection, scope: str, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO state_ledger (scope, state_version, state_root, updated_epoch)"
        " VALUES (?, 0, ?, ?)",
        (scope, GENESIS_STATE_ROOT, now_ms // 1000),
    )


def read_head(conn: sqlite3.Connection, scope: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT state_version, state_root FROM state_ledger WHERE scope = ?", (scope,)
    ).fetchone()
    if row is None:
        raise UnknownScopeError(scope)
    return int(row[0]), str(row[1])


def execute_with_capability(
    conn: sqlite3.Connection,
    *,
    secret_key: bytes,
    token: str,
    packet_id: str,
    scope: str,
    expected_state_version: int,
    tool_name: str,
    args: dict[str, Any],
    now_ms: int,
    mutation: Callable[[sqlite3.Connection], None],
) -> ExecutionOutcome:
    """Verify the token, then CAS-advance the ledger, run ``mutation`` and record the
    outbox intent atomically.

    ``conn`` must come from ``store.open_writer`` (it registers the SQL functions the
    outbox integrity trigger needs) and be in autocommit mode
    (``isolation_level=None``) so the explicit BEGIN IMMEDIATE owns the transaction.

    Raises ``CapabilityRejected`` (nothing was touched), ``StaleStateError``,
    ``UnknownScopeError`` or ``ReplayConflictError``; if ``mutation`` or any insert
    raises, the version bump is rolled back with it.
    """
    digest = compute_action_hash(tool_name, args)
    verify_capability_token(
        secret_key=secret_key,
        token=token,
        now_ms=now_ms,
        packet_id=packet_id,
        expected_state_version=expected_state_version,
        action_hash=digest,
    )
    action_json = canonicalize({"tool_name": tool_name, "args": args}).decode("utf-8")
    idempotency_key = compute_idempotency_key(packet_id, expected_state_version)
    now_s = now_ms // 1000

    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute(
            "SELECT action_hash FROM applied_packets WHERE packet_id = ?", (packet_id,)
        ).fetchone()
        if done is not None:
            conn.execute("ROLLBACK")
            if done[0] != digest:
                raise ReplayConflictError(packet_id)
            return ExecutionOutcome.ALREADY_APPLIED

        version, root = read_head(conn, scope)
        if version != expected_state_version:
            raise StaleStateError(scope, expected_state_version, version)
        new_root = sha256_hex(canonicalize([root, packet_id, digest]))
        cur = conn.execute(
            "UPDATE state_ledger SET state_version = state_version + 1,"
            " state_root = ?, updated_epoch = ? WHERE scope = ? AND state_version = ?",
            (new_root, now_s, scope, expected_state_version),
        )
        if cur.rowcount != 1:  # defensive: the WHERE guard is the CAS contract
            raise StaleStateError(scope, expected_state_version, -1)
        mutation(conn)
        conn.execute(
            "INSERT INTO applied_packets (packet_id, scope, resulting_version,"
            " action_hash, applied_epoch) VALUES (?, ?, ?, ?, ?)",
            (packet_id, scope, expected_state_version + 1, digest, now_s),
        )
        conn.execute(
            "INSERT INTO execution_outbox (scope, state_version, resulting_version,"
            " packet_id, action_hash, action_json, idempotency_key, status,"
            " created_epoch, updated_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
            (
                scope,
                expected_state_version,
                expected_state_version + 1,
                packet_id,
                digest,
                action_json,
                idempotency_key,
                now_s,
                now_s,
            ),
        )
        conn.execute("COMMIT")
        return ExecutionOutcome.APPLIED
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def get_outbox(conn: sqlite3.Connection, packet_id: str) -> OutboxRow | None:
    row = conn.execute(
        f"SELECT {_OUTBOX_COLUMNS} FROM execution_outbox WHERE packet_id = ?", (packet_id,)
    ).fetchone()
    return None if row is None else _outbox_row(row)


def fetch_pending_outbox(conn: sqlite3.Connection, limit: int = 50) -> list[OutboxRow]:
    """Oldest PENDING intents first (commit order)."""
    rows = conn.execute(
        f"SELECT {_OUTBOX_COLUMNS} FROM execution_outbox WHERE status = 'PENDING'"
        " ORDER BY outbox_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [_outbox_row(r) for r in rows]


def transition_outbox(
    conn: sqlite3.Connection, outbox_id: int, new_status: OutboxStatus, now_ms: int
) -> None:
    """Move a row along the allowed status graph; the schema trigger enforces it.

    Raises ``sqlite3.IntegrityError`` on an illegal transition. Does not need the
    integrity SQL functions, so an actuator process can use a plain connection.
    """
    cur = conn.execute(
        "UPDATE execution_outbox SET status = ?, updated_epoch = ? WHERE outbox_id = ?",
        (new_status.value, now_ms // 1000, outbox_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"no outbox row {outbox_id}")
