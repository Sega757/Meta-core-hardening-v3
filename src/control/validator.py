"""Control Plane reference core (sidecar side).

Contents
--------
* RFC 8785 (JCS) canonicalization and the deterministic SHA-256 hashes built on it.
* ``LoopCircuitBreaker``: exact-repeat guard, structural n-gram cycle detector,
  consecutive-error breaker and a pre-flight token bucket (ADR-0011).
* HMAC-SHA256 capability tokens binding a validated Decision Packet to a state
  version and an action hash (ADR-0016).
* Pydantic models mirroring ``schemas/decision_packet.schema.json``.

Only the standard library and ``pydantic`` (v2) are used. Time is always injected
(``clock`` / ``now_ms``) so every check is deterministic under test.

The L-E-J-D-A-S evaluation rules themselves are NOT implemented here: their
thresholds are still an open question (docs/open-questions.md).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
from collections import deque
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CanonicalizationError(ValueError):
    """Value cannot be represented in RFC 8785 canonical form."""


class KillReason(StrEnum):
    STEP_LIMIT = "step_limit"
    EXACT_REPEAT = "exact_repeat"
    STRUCTURAL_CYCLE = "structural_cycle"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    TOKEN_BUDGET = "token_budget"
    VERSION_STEP_LIMIT = "version_step_limit"


class KillSwitchTrigger(Exception):
    """Raised when the breaker decides the run must be terminated."""

    def __init__(self, reason: KillReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


class CapabilityRejection(StrEnum):
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    PACKET_MISMATCH = "packet_mismatch"
    ACTION_MISMATCH = "action_mismatch"
    STATE_VERSION_MISMATCH = "state_version_mismatch"


class CapabilityRejected(Exception):
    """Raised when a capability token fails verification."""

    def __init__(self, reason: CapabilityRejection, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# RFC 8785 (JCS) canonicalization
# --------------------------------------------------------------------------- #

_MAX_SAFE_INT: Final[int] = 2**53 - 1
_MAX_DEPTH: Final[int] = 64
_SHORT_ESCAPES: Final[dict[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _format_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogate
        raise CanonicalizationError("string contains a lone surrogate") from exc
    out: list[str] = ['"']
    for ch in value:
        esc = _SHORT_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_float(value: float) -> str:
    """ECMAScript Number::toString for a finite double (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(value):
        raise CanonicalizationError("NaN/Infinity are not valid JSON numbers")
    if value == 0.0:
        return "0"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(map(str, digit_tuple))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k  # value = 0.DIGITS * 10**n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp_part = f"e{'+' if e >= 0 else '-'}{abs(e)}"
        body = digits + exp_part if k == 1 else f"{digits[0]}.{digits[1:]}{exp_part}"
    return sign + body


def _serialize(value: Any, depth: int, out: list[str]) -> None:
    if depth > _MAX_DEPTH:
        raise CanonicalizationError(f"nesting deeper than {_MAX_DEPTH}")
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_format_string(value))
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise CanonicalizationError(
                "integer outside IEEE-754 safe range; encode it as a string"
            )
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_format_float(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, depth + 1, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
        try:
            ordered = sorted(value, key=lambda k: k.encode("utf-16-be"))
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("key contains a lone surrogate") from exc
        out.append("{")
        for i, key in enumerate(ordered):
            if i:
                out.append(",")
            out.append(_format_string(key))
            out.append(":")
            _serialize(value[key], depth + 1, out)
        out.append("}")
    else:
        raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of a JSON-compatible value."""
    out: list[str] = []
    _serialize(value, 0, out)
    return "".join(out).encode("utf-8")


# --------------------------------------------------------------------------- #
# Deterministic hashes
# --------------------------------------------------------------------------- #

_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
GENESIS_KO_ID: Final[str] = "0" * 64


def _parse_int_i_json(token: str) -> int | float:
    """Integers beyond the IEEE-754 safe range are doubles in JCS, not Python ints."""
    value = int(token)
    return value if abs(value) <= _MAX_SAFE_INT else float(token)


def _reject_non_finite(name: str) -> None:
    raise CanonicalizationError(f"non-finite number literal: {name}")


def loads_i_json(text: str) -> Any:
    """Parse JSON text the way RFC 8785 reads numbers (every number is a double).

    ``canonicalize(loads_i_json(t)) == t`` holds exactly for canonical text ``t``;
    plain ``json.loads`` breaks that for floats such as ``-3.897857504981826e16``,
    whose canonical text ``-38978575049818260`` would parse as an out-of-range int.
    """
    return json.loads(text, parse_int=_parse_int_i_json, parse_constant=_reject_non_finite)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def action_hash(tool_name: str, args: dict[str, Any]) -> str:
    """SHA-256 over the JCS form of ``{tool_name, args}`` (injective encoding)."""
    return sha256_hex(canonicalize({"tool_name": tool_name, "args": args}))


def compute_fact_key(subject: str, predicate: str) -> str:
    """Logical identity of a fact: ``SHA-256(subject || predicate)`` (JCS pair)."""
    return sha256_hex(canonicalize([subject, predicate]))


def compute_ko_id(
    *,
    subject: str,
    predicate: str,
    obj: JsonValue,
    provenance: str,
    parent_ko_id: str,
    created_epoch: int,
) -> str:
    """Immutable version id of a Knowledge Object (ADR-0013).

    ``SHA-256(canonical_content || provenance || parent_ko_id || created_epoch)``,
    encoded as a JCS object so field boundaries are unambiguous.
    """
    if provenance not in {p.value for p in Provenance}:
        raise ValueError(f"unknown provenance: {provenance!r}")
    if not _HEX64.match(parent_ko_id):
        raise ValueError("parent_ko_id must be 64 lowercase hex chars")
    payload = {
        "content": {"subject": subject, "predicate": predicate, "object": obj},
        "provenance": provenance,
        "parent_ko_id": parent_ko_id,
        "created_epoch": created_epoch,
    }
    return sha256_hex(canonicalize(payload))


def compute_idempotency_key(packet_id: str, expected_state_version: int) -> str:
    """Key an external actuator deduplicates on (ADR-0016 outbox).

    ``SHA-256(packet_id || expected_state_version)`` encoded as a JCS array, so
    ``("ab", 1)`` and ``("a", "b1")`` cannot collide.
    """
    return sha256_hex(canonicalize([packet_id, expected_state_version]))


def compute_cache_key(
    *,
    model_id: str,
    system_prompt_hash: str,
    state_root: str,
    canonical_prompt_payload: JsonValue,
) -> str:
    """State-bound exact cache key (ADR-0012). No semantic similarity is involved."""
    return sha256_hex(
        canonicalize(
            [model_id, system_prompt_hash, state_root, canonical_prompt_payload]
        )
    )


# --------------------------------------------------------------------------- #
# Models (mirror schemas/decision_packet.schema.json)
# --------------------------------------------------------------------------- #

# Strict scalars (no "1" -> 1 coercion); enum fields opt out so JSON strings parse.
_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class Provenance(StrEnum):
    ORGANIC_USER = "ORGANIC_USER"
    SYNTHETIC_AGENT = "SYNTHETIC_AGENT"
    VERIFIED_EXTERNAL = "VERIFIED_EXTERNAL"


class EscalationLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class CheckResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class ToolCall(BaseModel):
    model_config = _STRICT
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    args: dict[str, JsonValue]


class LejdasAttestation(BaseModel):
    model_config = _STRICT
    legal: CheckResult = Field(strict=False)
    ethical: CheckResult = Field(strict=False)
    jurisdictional: CheckResult = Field(strict=False)
    duty: CheckResult = Field(strict=False)
    accountability: CheckResult = Field(strict=False)
    safety: CheckResult = Field(strict=False)
    escalation_level: EscalationLevel = Field(strict=False)
    validator_version: str = Field(min_length=1, max_length=64)
    evaluated_at_ms: int = Field(ge=0)


_UUID_PATTERN: Final = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TOKEN_PATTERN: Final = r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$"


class DecisionPacket(BaseModel):
    model_config = _STRICT
    packet_id: str = Field(pattern=_UUID_PATTERN)
    parent_state_version: int = Field(ge=0)
    provenance: Provenance = Field(strict=False)
    tool_call: ToolCall
    justification: str | None = Field(default=None, max_length=2000)
    lejdas_attestation: LejdasAttestation | None  # null until the Control Plane fills it
    capability_token: str | None = Field(pattern=_TOKEN_PATTERN)  # null until issued


class ProvenanceViolation(ValueError):
    """An agent-channel packet claimed a provenance other than SYNTHETIC_AGENT."""


def require_agent_provenance(packet: DecisionPacket) -> None:
    """Provenance is assigned by the trusted side from the ingress channel.

    A packet arriving from the Reasoning Plane must declare SYNTHETIC_AGENT; any
    other value is a forgery attempt and must be rejected before validation.
    """
    if packet.provenance is not Provenance.SYNTHETIC_AGENT:
        raise ProvenanceViolation(
            f"agent channel declared provenance {packet.provenance.value}"
        )


# --------------------------------------------------------------------------- #
# Pre-flight token bucket
# --------------------------------------------------------------------------- #


class TokenBucket:
    """Thread-safe token bucket; budget is reserved *before* a call is dispatched."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second < 0:
            raise ValueError("refill_per_second must be >= 0")
        self._capacity = capacity
        self._rate = refill_per_second
        self._clock = clock
        self._available = float(capacity)
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._available = min(float(self._capacity), self._available + elapsed * self._rate)

    @property
    def capacity(self) -> int:
        return self._capacity

    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._available

    def try_reserve(self, tokens: int) -> bool:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        with self._lock:
            self._refill()
            if tokens > self._available:
                return False
            self._available -= tokens
            return True

    def settle(self, reserved: int, actual: int) -> None:
        """Refund unused reservation, or charge the overrun (debt is floored)."""
        if reserved < 0 or actual < 0:
            raise ValueError("token counts must be >= 0")
        with self._lock:
            self._refill()
            self._available = max(
                -float(self._capacity),
                min(float(self._capacity), self._available + (reserved - actual)),
            )


# --------------------------------------------------------------------------- #
# Loop circuit breaker
# --------------------------------------------------------------------------- #


class BreakerConfig(BaseModel):
    """Starting values only; they are NOT calibrated against real traces."""

    model_config = _STRICT
    max_steps: int = Field(default=25, gt=0)
    # Calls allowed at one state_version, for every tool. Bounds read-only
    # exploration that the cycle rule deliberately lets through.
    max_steps_per_state_version: int = Field(default=20, gt=0)
    # Tools declared read-only by the TRUSTED side (tool registry / deployment
    # config). It is deliberately not a field of ProposedCall: a flag the agent
    # could set itself would let a compromised agent opt out of cycle detection.
    read_only_tools: frozenset[str] = frozenset()
    cycle_min_period: int = Field(default=2, ge=1)
    cycle_max_period: int = Field(default=4, ge=1)
    cycle_min_repeats: int = Field(default=2, ge=2)
    max_consecutive_errors: int = Field(default=3, ge=1)


class ProposedCall(BaseModel):
    model_config = _STRICT
    tool_name: str = Field(min_length=1)
    args: dict[str, JsonValue]
    state_version: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)


class ToolOutcome(BaseModel):
    model_config = _STRICT
    status: Literal["ok", "error"]
    error_code: str | None = None


class Reservation(BaseModel):
    model_config = _STRICT
    call_key: str
    tokens: int = Field(ge=0)


class LoopCircuitBreaker:
    """Per-run guard evaluated before every tool call (ADR-0011).

    ``admit`` must be called before dispatch and ``record_outcome`` after it.
    Both raise ``KillSwitchTrigger`` when the run has to be terminated.
    """

    def __init__(self, config: BreakerConfig, bucket: TokenBucket) -> None:
        if config.cycle_min_period > config.cycle_max_period:
            raise ValueError("cycle_min_period must be <= cycle_max_period")
        self._cfg = config
        self._bucket = bucket
        self._lock = threading.Lock()
        self._steps = 0
        # (tool_name, state_version, args_hash) of the most recent admitted calls
        self._history: deque[tuple[str, int, str]] = deque(
            maxlen=config.cycle_max_period * config.cycle_min_repeats
        )
        self._version_steps: dict[int, int] = {}
        self._pending: set[tuple[str, int]] = set()
        self._succeeded: set[tuple[str, int]] = set()
        self._error_streak: dict[str, int] = {}

    def admit(self, call: ProposedCall) -> Reservation:
        with self._lock:
            if self._steps >= self._cfg.max_steps:
                raise KillSwitchTrigger(
                    KillReason.STEP_LIMIT, f"more than {self._cfg.max_steps} steps"
                )
            if self._version_steps.get(call.state_version, 0) >= self._cfg.max_steps_per_state_version:
                raise KillSwitchTrigger(
                    KillReason.VERSION_STEP_LIMIT,
                    f"more than {self._cfg.max_steps_per_state_version} calls at "
                    f"state_version={call.state_version}",
                )
            digest = action_hash(call.tool_name, call.args)
            key = (digest, call.state_version)
            if key in self._pending or key in self._succeeded:
                raise KillSwitchTrigger(
                    KillReason.EXACT_REPEAT,
                    f"{call.tool_name} repeated at state_version={call.state_version}",
                )
            candidate = (call.tool_name, call.state_version, digest)
            period = self._find_cycle(candidate)
            if period is not None:
                raise KillSwitchTrigger(
                    KillReason.STRUCTURAL_CYCLE,
                    f"call pattern of period {period} repeated without state change",
                )
            needed = call.input_tokens + call.max_output_tokens
            if not self._bucket.try_reserve(needed):
                raise KillSwitchTrigger(
                    KillReason.TOKEN_BUDGET,
                    f"reservation of {needed} tokens exceeds available budget",
                )
            self._steps += 1
            self._version_steps[call.state_version] = (
                self._version_steps.get(call.state_version, 0) + 1
            )
            self._history.append(candidate)
            self._pending.add(key)
            return Reservation(call_key=f"{digest}:{call.state_version}", tokens=needed)

    def record_outcome(
        self,
        call: ProposedCall,
        reservation: Reservation,
        outcome: ToolOutcome,
        actual_tokens: int | None = None,
    ) -> None:
        with self._lock:
            key = (action_hash(call.tool_name, call.args), call.state_version)
            self._pending.discard(key)
            if actual_tokens is not None:
                self._bucket.settle(reservation.tokens, actual_tokens)
            failed = outcome.status == "error" or outcome.error_code is not None
            if not failed:
                self._succeeded.add(key)
                self._error_streak[call.tool_name] = 0
                return
            streak = self._error_streak.get(call.tool_name, 0) + 1
            self._error_streak[call.tool_name] = streak
            if streak >= self._cfg.max_consecutive_errors:
                raise KillSwitchTrigger(
                    KillReason.CONSECUTIVE_ERRORS,
                    f"{call.tool_name} failed {streak} times in a row",
                )

    def _find_cycle(self, candidate: tuple[str, int, str]) -> int | None:
        """Return the period of a halting cycle ending at ``candidate``, if any.

        A pattern of tool names that repeats with no state change is a cycle,
        unless every call in the window is a declared read-only tool AND no
        (tool, args) hash repeats inside the window: that is exploration
        (pagination, lookups by different keys), bounded by
        ``max_steps_per_state_version``. Repeated arguments or any mutating
        tool in the window still halt.
        """
        window_all = [*self._history, candidate]
        repeats = self._cfg.cycle_min_repeats
        for period in range(self._cfg.cycle_min_period, self._cfg.cycle_max_period + 1):
            size = period * repeats
            if len(window_all) < size:
                continue
            window = window_all[-size:]
            if len({version for _, version, _ in window}) != 1:
                continue  # state moved: that is progress, not a loop
            names = [name for name, _, _ in window]
            if not all(names[i] == names[i % period] for i in range(size)):
                continue
            all_read_only = all(name in self._cfg.read_only_tools for name in names)
            distinct_args = len({digest for _, _, digest in window}) == size
            if all_read_only and distinct_args:
                continue
            return period
        return None


# --------------------------------------------------------------------------- #
# Capability tokens (ADR-0016)
# --------------------------------------------------------------------------- #

MIN_KEY_BYTES: Final[int] = 32
MAX_TTL_MS: Final[int] = 60_000
TOKEN_TYPE: Final[str] = "meta-core/capability/v1"
_B64URL: Final = re.compile(r"^[A-Za-z0-9_-]+$")


class CapabilityClaims(BaseModel):
    model_config = _STRICT
    typ: Literal["meta-core/capability/v1"]
    packet_id: str = Field(pattern=_UUID_PATTERN)
    expected_state_version: int = Field(ge=0)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at_ms: int = Field(gt=0)


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    if not _B64URL.match(text):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_capability_token(
    *,
    secret_key: bytes,
    packet_id: str,
    expected_state_version: int,
    action_hash: str,
    now_ms: int,
    ttl_ms: int,
) -> str:
    """Wire format: ``v1.<b64url(JCS(claims))>.<b64url(HMAC-SHA256(key, claims))>``."""
    if len(secret_key) < MIN_KEY_BYTES:
        raise ValueError(f"secret_key must be at least {MIN_KEY_BYTES} bytes")
    if not 0 < ttl_ms <= MAX_TTL_MS:
        raise ValueError(f"ttl_ms must be in (0, {MAX_TTL_MS}]")
    claims = CapabilityClaims(
        typ=TOKEN_TYPE,
        packet_id=packet_id,
        expected_state_version=expected_state_version,
        action_hash=action_hash,
        expires_at_ms=now_ms + ttl_ms,
    )
    payload = canonicalize(claims.model_dump(mode="json"))
    mac = hmac.new(secret_key, payload, hashlib.sha256).digest()
    return f"v1.{_b64u_encode(payload)}.{_b64u_encode(mac)}"


def verify_capability_token(
    *,
    secret_key: bytes,
    token: str,
    now_ms: int,
    packet_id: str,
    expected_state_version: int,
    action_hash: str,
) -> CapabilityClaims:
    """Verify signature first, then expiry, then the binding to the packet."""
    if len(secret_key) < MIN_KEY_BYTES:
        raise ValueError(f"secret_key must be at least {MIN_KEY_BYTES} bytes")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise CapabilityRejected(CapabilityRejection.MALFORMED, "bad token layout")
    try:
        payload = _b64u_decode(parts[1])
        mac = _b64u_decode(parts[2])
    except ValueError as exc:
        raise CapabilityRejected(CapabilityRejection.MALFORMED, "bad base64url") from exc
    expected_mac = hmac.new(secret_key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise CapabilityRejected(CapabilityRejection.BAD_SIGNATURE, "MAC mismatch")
    try:
        claims = CapabilityClaims.model_validate(json.loads(payload))
    except ValueError as exc:  # JSONDecodeError and pydantic ValidationError
        raise CapabilityRejected(CapabilityRejection.MALFORMED, "bad claims") from exc
    if now_ms >= claims.expires_at_ms:
        raise CapabilityRejected(CapabilityRejection.EXPIRED, "token expired")
    if claims.packet_id != packet_id:
        raise CapabilityRejected(CapabilityRejection.PACKET_MISMATCH, "different packet")
    if claims.action_hash != action_hash:
        raise CapabilityRejected(CapabilityRejection.ACTION_MISMATCH, "different action")
    if claims.expected_state_version != expected_state_version:
        raise CapabilityRejected(
            CapabilityRejection.STATE_VERSION_MISMATCH, "different state version"
        )
    return claims
