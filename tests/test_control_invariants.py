"""Invariant tests for the Control / Execution / Reasoning reference core.

Each test names the risk it guards. Stdlib ``unittest`` + the repo's own modules
(pydantic is the only third-party import).
"""

import hashlib
import hmac
import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.control import validator as v  # noqa: E402
from src.control.validator import (  # noqa: E402
    BreakerConfig,
    CapabilityRejected,
    CapabilityRejection,
    KillReason,
    KillSwitchTrigger,
    LoopCircuitBreaker,
    ProposedCall,
    TokenBucket,
    ToolOutcome,
    action_hash,
    canonicalize,
)
from src.data.store import open_writer  # noqa: E402
from src.execution.state_ledger import (  # noqa: E402
    ExecutionOutcome,
    ReplayConflictError,
    StaleStateError,
    execute_with_capability,
    read_head,
    register_scope,
)
from src.reasoning.context_budget import (  # noqa: E402
    AllocatorConfig,
    ContextConfigurationError,
    HistoryPair,
    InelasticBlocks,
    RagChunk,
    WaterfillingContextAllocator,
    waterfill,
)

KEY = bytes(range(32))
PID = "123e4567-e89b-42d3-a456-426614174000"
PID2 = "223e4567-e89b-42d3-a456-426614174001"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# JCS (RFC 8785)
# --------------------------------------------------------------------------- #


class JcsTests(unittest.TestCase):
    def test_rfc8785_section_3_2_2_example(self):
        # Risk: wrong number/string canonical form -> hash differs across languages.
        src = {
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
            "string": "\u20ac$\u000f\nA'B\"\\\\\"/",
            "literals": [None, True, False],
        }
        expected = (
            '{"literals":[null,true,false],'
            '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        )
        self.assertEqual(canonicalize(src).decode("utf-8"), expected)

    def test_keys_sorted_by_utf16_code_units_not_code_points(self):
        # Risk: code-point ordering puts U+FB33 before U+1F600; RFC 8785 orders by UTF-16 units.
        keys = ["\u20ac", "\r", "\ufb33", "1", "\U0001f600", "\u0080", "\u00f6"]
        parsed = json.loads(canonicalize({k: 0 for k in keys}).decode("utf-8"))
        self.assertEqual(list(parsed), ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"])

    def test_number_forms_follow_ecmascript(self):
        cases = {
            1.0: "1",
            100.0: "100",
            1e21: "1e+21",
            1e20: "100000000000000000000",
            1e-7: "1e-7",
            0.000001: "0.000001",
            -0.0: "0",
            5e-324: "5e-324",
            1.7976931348623157e308: "1.7976931348623157e+308",
            -1.5: "-1.5",
        }
        for value, text in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canonicalize(value).decode(), text)

    def test_arg_mutations_that_must_not_change_the_hash(self):
        # Risk (ADR-0011): key order / 1 vs 1.0 used to dodge exact-hash repeat detection.
        a = action_hash("ledger.adjust", {"sku": "A", "qty": 1, "meta": {"x": 1, "y": 2}})
        b = action_hash("ledger.adjust", {"meta": {"y": 2, "x": 1.0}, "qty": 1.0, "sku": "A"})
        self.assertEqual(a, b)

    def test_arg_mutations_that_must_change_the_hash(self):
        base = action_hash("t", {"k": "v"})
        self.assertNotEqual(base, action_hash("t", {"k": "v "}))
        self.assertNotEqual(base, action_hash("u", {"k": "v"}))

    def test_canonical_text_roundtrips_through_loads_i_json(self):
        # Regression (found by the v3 SQL/Python fuzz): the canonical text of a large float
        # looks like an out-of-range int, and plain json.loads broke the round trip.
        for value in (-3.897857504981826e16, 1e20, 9007199254740993.0, 1e21, 5e-324, 123456789012345680000.0):
            with self.subTest(value=value):
                text = canonicalize(value).decode()
                self.assertEqual(canonicalize(v.loads_i_json(text)).decode(), text)
        rnd = random.Random(21)
        for _ in range(3000):
            value = {
                "n": [rnd.uniform(-1, 1) * 10 ** rnd.randint(-30, 30), rnd.randint(-2**53 + 1, 2**53 - 1)],
                "s": "".join(rnd.choice("aé\u2028\"\\\n") for _ in range(rnd.randint(0, 5))),
            }
            text = canonicalize(value).decode()
            self.assertEqual(canonicalize(v.loads_i_json(text)).decode(), text)

    def test_loads_i_json_rejects_non_finite_literals(self):
        for text in ("NaN", "Infinity", "-Infinity", "[NaN]"):
            with self.subTest(text=text), self.assertRaises(v.CanonicalizationError):
                v.loads_i_json(text)

    def test_rejects_non_canonicalizable_values(self):
        bad = [float("nan"), float("inf"), 2**53, {1: "x"}, "\ud800", {"\ud800": 1}, {1, 2}, object()]
        for value in bad:
            with self.subTest(value=repr(value)):
                with self.assertRaises(v.CanonicalizationError):
                    canonicalize(value)

    def test_depth_bomb_rejected(self):
        value: list = []
        for _ in range(200):
            value = [value]
        with self.assertRaises(v.CanonicalizationError):
            canonicalize(value)


class CacheKeyTests(unittest.TestCase):
    BASE = dict(model_id="m-1", system_prompt_hash="a" * 64, state_root="b" * 64, canonical_prompt_payload={"q": [1, 2], "r": {"x": 1}})

    def test_key_changes_with_every_component(self):
        # Risk (ADR-0012): a cached packet computed for another state/model/prompt is replayed.
        base = v.compute_cache_key(**self.BASE)
        for field, other in (
            ("model_id", "m-2"),
            ("system_prompt_hash", "c" * 64),
            ("state_root", "d" * 64),
            ("canonical_prompt_payload", {"q": [1, 3], "r": {"x": 1}}),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(base, v.compute_cache_key(**{**self.BASE, field: other}))

    def test_key_is_stable_under_payload_key_order(self):
        reordered = {**self.BASE, "canonical_prompt_payload": {"r": {"x": 1}, "q": [1, 2]}}
        self.assertEqual(v.compute_cache_key(**self.BASE), v.compute_cache_key(**reordered))

    def test_fields_cannot_be_shifted_across_boundaries(self):
        a = v.compute_cache_key(**{**self.BASE, "model_id": "ab", "system_prompt_hash": "c"})
        b = v.compute_cache_key(**{**self.BASE, "model_id": "a", "system_prompt_hash": "bc"})
        self.assertNotEqual(a, b)


# --------------------------------------------------------------------------- #
# Token bucket + loop breaker
# --------------------------------------------------------------------------- #


def make_breaker(*, max_steps=25, capacity=100_000, rate=0.0, clock=None, **cfg):
    clock = clock or FakeClock()
    bucket = TokenBucket(capacity, rate, clock)
    return LoopCircuitBreaker(BreakerConfig(max_steps=max_steps, **cfg), bucket), bucket, clock


def pc(tool="a", args=None, version=0, inp=10, out=10):
    return ProposedCall(
        tool_name=tool, args=args or {}, state_version=version, input_tokens=inp, max_output_tokens=out
    )


OK = ToolOutcome(status="ok")
ERR = ToolOutcome(status="error", error_code="E_TIMEOUT")


def run_ok(breaker, call):
    res = breaker.admit(call)
    breaker.record_outcome(call, res, OK, actual_tokens=call.input_tokens)
    return res


class TokenBucketTests(unittest.TestCase):
    def test_reserve_denied_when_over_budget_and_refill_restores(self):
        clock = FakeClock()
        bucket = TokenBucket(100, 10.0, clock)
        self.assertTrue(bucket.try_reserve(100))
        self.assertFalse(bucket.try_reserve(1))
        clock.now += 5.0  # +50 tokens
        self.assertTrue(bucket.try_reserve(50))
        self.assertFalse(bucket.try_reserve(1))

    def test_never_exceeds_capacity_after_idle(self):
        clock = FakeClock()
        bucket = TokenBucket(100, 1000.0, clock)
        clock.now += 3600
        self.assertEqual(bucket.available(), 100.0)

    def test_settle_refunds_unused_and_charges_overrun(self):
        bucket = TokenBucket(100, 0.0, FakeClock())
        self.assertTrue(bucket.try_reserve(80))
        bucket.settle(80, 30)  # used only 30 -> +50
        self.assertEqual(bucket.available(), 70.0)
        bucket.settle(0, 20)  # overrun of 20
        self.assertEqual(bucket.available(), 50.0)

    def test_parallel_burst_cannot_overspend(self):
        # Risk: check-then-spend races let a parallel burst exceed the budget.
        bucket = TokenBucket(100, 0.0, FakeClock())
        results: list[bool] = []
        barrier = threading.Barrier(50)

        def worker():
            barrier.wait()
            results.append(bucket.try_reserve(10))

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 10)
        self.assertEqual(bucket.available(), 0.0)


class LoopBreakerTests(unittest.TestCase):
    def assertKill(self, reason, fn):
        with self.assertRaises(KillSwitchTrigger) as ctx:
            fn()
        self.assertIs(ctx.exception.reason, reason)

    def test_exact_repeat_at_same_state_is_killed_even_with_reordered_args(self):
        breaker, _, _ = make_breaker()
        run_ok(breaker, pc("read", {"a": 1, "b": {"x": 1, "y": 2}}))
        self.assertKill(
            KillReason.EXACT_REPEAT,
            lambda: breaker.admit(pc("read", {"b": {"y": 2, "x": 1.0}, "a": 1})),
        )

    def test_same_call_allowed_after_state_version_moves(self):
        breaker, _, _ = make_breaker()
        run_ok(breaker, pc("read", {"a": 1}, version=0))
        run_ok(breaker, pc("read", {"a": 1}, version=1))

    def test_parallel_identical_in_flight_call_is_killed(self):
        breaker, _, _ = make_breaker()
        breaker.admit(pc("read", {"a": 1}))
        self.assertKill(KillReason.EXACT_REPEAT, lambda: breaker.admit(pc("read", {"a": 1})))

    def test_retry_after_error_is_allowed_until_third_consecutive_error(self):
        breaker, _, _ = make_breaker()
        call = pc("fetch", {"u": "x"})
        for _ in range(2):
            res = breaker.admit(call)
            breaker.record_outcome(call, res, ERR)
        res = breaker.admit(call)
        self.assertKill(KillReason.CONSECUTIVE_ERRORS, lambda: breaker.record_outcome(call, res, ERR))

    def test_error_code_on_ok_status_counts_as_failure(self):
        breaker, _, _ = make_breaker(max_consecutive_errors=2)
        sneaky = ToolOutcome(status="ok", error_code="E_PARTIAL")
        for i in range(2):
            call = pc("t", {"i": i}, version=i)
            res = breaker.admit(call)
            if i == 1:
                self.assertKill(
                    KillReason.CONSECUTIVE_ERRORS, lambda: breaker.record_outcome(call, res, sneaky)
                )
            else:
                breaker.record_outcome(call, res, sneaky)

    def test_success_resets_error_streak_and_streak_is_per_tool(self):
        breaker, _, _ = make_breaker()
        for i in range(2):
            c = pc("a", {"i": i}, version=i)
            breaker.record_outcome(c, breaker.admit(c), ERR)
        c = pc("b", {}, version=2)
        breaker.record_outcome(c, breaker.admit(c), ERR)  # other tool: separate streak
        c = pc("a", {"i": 9}, version=3)
        breaker.record_outcome(c, breaker.admit(c), OK)  # resets tool a
        for i in range(2):
            c = pc("a", {"i": 20 + i}, version=10 + i)
            breaker.record_outcome(c, breaker.admit(c), ERR)  # 2 < 3, still alive

    def test_cycle_abab_killed_even_though_args_keep_changing(self):
        # Risk: the LLM mutates args slightly each step so hashes never repeat.
        breaker, _, _ = make_breaker()
        run_ok(breaker, pc("a", {"n": 1}))
        run_ok(breaker, pc("b", {"n": 2}))
        run_ok(breaker, pc("a", {"n": 3}))
        self.assertKill(KillReason.STRUCTURAL_CYCLE, lambda: breaker.admit(pc("b", {"n": 4})))

    def test_cycles_of_period_three_and_four_are_detected(self):
        for names in (["a", "b", "c"], ["a", "b", "c", "d"]):
            with self.subTest(period=len(names)):
                breaker, _, _ = make_breaker()
                seq = names * 2
                for i, name in enumerate(seq[:-1]):
                    run_ok(breaker, pc(name, {"n": i}))
                self.assertKill(
                    KillReason.STRUCTURAL_CYCLE, lambda: breaker.admit(pc(seq[-1], {"n": 99}))
                )

    def test_no_cycle_when_state_advances_between_calls(self):
        breaker, _, _ = make_breaker()
        for i, name in enumerate(["a", "b", "a", "b", "a", "b"]):
            run_ok(breaker, pc(name, {"n": i}, version=i))  # progress every step

    def test_no_cycle_for_distinct_sequence(self):
        breaker, _, _ = make_breaker()
        for i, name in enumerate(["a", "b", "c", "d", "e", "f"]):
            run_ok(breaker, pc(name, {"n": i}))

    # --- read-only exploration (v3) -------------------------------------------------

    def test_legitimate_pagination_over_read_only_tools_is_not_a_cycle(self):
        # Risk: A(page=1) -> B(id=1) -> A(page=2) -> B(id=2) is exploration, not a loop.
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a", "b"}))
        run_ok(breaker, pc("a", {"page": 1}))
        run_ok(breaker, pc("b", {"id": 1}))
        run_ok(breaker, pc("a", {"page": 2}))
        run_ok(breaker, pc("b", {"id": 2}))
        run_ok(breaker, pc("a", {"page": 3}))
        run_ok(breaker, pc("b", {"id": 3}))

    def test_same_pagination_over_mutating_tools_halts(self):
        # Risk: the exemption must not apply to tools nobody declared read-only.
        breaker, _, _ = make_breaker()
        run_ok(breaker, pc("a", {"page": 1}))
        run_ok(breaker, pc("b", {"id": 1}))
        run_ok(breaker, pc("a", {"page": 2}))
        self.assertKill(KillReason.STRUCTURAL_CYCLE, lambda: breaker.admit(pc("b", {"id": 2})))

    def test_window_mixing_read_only_and_mutating_tools_halts(self):
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a"}))
        run_ok(breaker, pc("a", {"page": 1}))
        run_ok(breaker, pc("b", {"id": 1}))  # b is not declared read-only
        run_ok(breaker, pc("a", {"page": 2}))
        self.assertKill(KillReason.STRUCTURAL_CYCLE, lambda: breaker.admit(pc("b", {"id": 2})))

    def test_read_only_cycle_with_repeated_args_inside_the_window_halts(self):
        # B(id=1) failed the first time, so exact-repeat allows the retry; the window
        # [A1, B1, A2, B1] repeats a (tool, args) hash and is therefore a loop.
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a", "b"}))
        run_ok(breaker, pc("a", {"page": 1}))
        failed = pc("b", {"id": 1})
        breaker.record_outcome(failed, breaker.admit(failed), ERR)
        run_ok(breaker, pc("a", {"page": 2}))
        self.assertKill(KillReason.STRUCTURAL_CYCLE, lambda: breaker.admit(pc("b", {"id": 1})))

    def test_single_read_only_tool_with_distinct_args_is_allowed(self):
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a"}))
        for i in range(8):
            run_ok(breaker, pc("a", {"page": i}))

    def test_read_only_exploration_is_bounded_per_state_version(self):
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a", "b"}), max_steps_per_state_version=6)
        for i in range(3):
            run_ok(breaker, pc("a", {"page": i}))
            run_ok(breaker, pc("b", {"id": i}))
        self.assertKill(KillReason.VERSION_STEP_LIMIT, lambda: breaker.admit(pc("a", {"page": 99})))

    def test_version_cap_resets_when_the_state_moves(self):
        breaker, _, _ = make_breaker(read_only_tools=frozenset({"a"}), max_steps_per_state_version=3)
        for i in range(3):
            run_ok(breaker, pc("a", {"page": i}, version=0))
        run_ok(breaker, pc("a", {"page": 0}, version=1))

    def test_version_cap_applies_to_mutating_tools_too(self):
        breaker, _, _ = make_breaker(max_steps_per_state_version=2)
        run_ok(breaker, pc("a", {"n": 1}))
        run_ok(breaker, pc("b", {"n": 2}))
        self.assertKill(KillReason.VERSION_STEP_LIMIT, lambda: breaker.admit(pc("c", {"n": 3})))

    def test_agent_cannot_mark_a_call_as_read_only(self):
        # Risk: a compromised agent opting out of cycle detection via its own flag.
        with self.assertRaises(ValueError):
            ProposedCall(
                tool_name="a", args={}, state_version=0, input_tokens=1, max_output_tokens=1, is_read_only=True
            )
        self.assertEqual(BreakerConfig().read_only_tools, frozenset())

    def test_step_limit(self):
        breaker, _, _ = make_breaker(max_steps=3)
        for i, name in enumerate(["a", "b", "c"]):
            run_ok(breaker, pc(name, {}, version=i))
        self.assertKill(KillReason.STEP_LIMIT, lambda: breaker.admit(pc("d", {}, version=3)))

    def test_preflight_reservation_blocks_before_dispatch_and_leaves_no_trace(self):
        breaker, bucket, clock = make_breaker(capacity=100, rate=10.0)
        big = pc("a", {"x": 1}, inp=60, out=60)  # needs 120 > capacity
        self.assertKill(KillReason.TOKEN_BUDGET, lambda: breaker.admit(big))
        self.assertEqual(bucket.available(), 100.0)  # nothing was spent
        run_ok(breaker, pc("a", {"x": 1}, inp=40, out=40))  # same tool+args now fine

    def test_reservation_is_input_plus_max_output(self):
        breaker, bucket, _ = make_breaker(capacity=1000)
        res = breaker.admit(pc("a", {}, inp=30, out=70))
        self.assertEqual(res.tokens, 100)
        self.assertEqual(bucket.available(), 900.0)


# --------------------------------------------------------------------------- #
# Capability tokens
# --------------------------------------------------------------------------- #

AH = action_hash("ledger.adjust", {"sku": "META-001", "delta": -10})
NOW = 1_700_000_000_000


def issue(**over):
    kw = dict(
        secret_key=KEY, packet_id=PID, expected_state_version=7, action_hash=AH, now_ms=NOW, ttl_ms=5000
    )
    kw.update(over)
    return v.issue_capability_token(**kw)


def verify(token, **over):
    kw = dict(
        secret_key=KEY, token=token, now_ms=NOW + 1, packet_id=PID, expected_state_version=7, action_hash=AH
    )
    kw.update(over)
    return v.verify_capability_token(**kw)


class CapabilityTokenTests(unittest.TestCase):
    def assertRejected(self, reason, fn):
        with self.assertRaises(CapabilityRejected) as ctx:
            fn()
        self.assertIs(ctx.exception.reason, reason)

    def test_roundtrip(self):
        claims = verify(issue())
        self.assertEqual(claims.packet_id, PID)
        self.assertEqual(claims.expected_state_version, 7)
        self.assertEqual(claims.expires_at_ms, NOW + 5000)

    def test_wire_format_matches_independent_stdlib_derivation(self):
        # Re-derive with json+hmac only: this is the contract a Node.js verifier follows.
        payload = json.dumps(
            {
                "action_hash": AH,
                "expected_state_version": 7,
                "expires_at_ms": NOW + 5000,
                "packet_id": PID,
                "typ": "meta-core/capability/v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        import base64

        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        mac = hmac.new(KEY, payload, hashlib.sha256).digest()
        self.assertEqual(issue(), f"v1.{b64(payload)}.{b64(mac)}")

    def test_fixed_vector(self):
        # Regression vector for other-language implementations (key = bytes 0..31).
        self.assertEqual(
            issue(),
            "v1.eyJhY3Rpb25faGFzaCI6IjIwY2U1MDZjODA4ZGJiMjg1Y2NlNTU2MGY4ZWIwMzg3YzA2OTFiNTQyMjZlYmMxMzcxNmEzZmRmZWFjYWNiNTkiLCJleHBlY3RlZF9zdGF0ZV92ZXJzaW9uIjo3LCJleHBpcmVzX2F0X21zIjoxNzAwMDAwMDA1MDAwLCJwYWNrZXRfaWQiOiIxMjNlNDU2Ny1lODliLTQyZDMtYTQ1Ni00MjY2MTQxNzQwMDAiLCJ0eXAiOiJtZXRhLWNvcmUvY2FwYWJpbGl0eS92MSJ9.RRgffFEzXMHncmQge3_I6hA2nkfG6Rw6dMuDBDIOx_o",
        )

    def test_tampered_payload_or_mac_is_rejected(self):
        head, payload, mac = issue().split(".")
        flipped = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
        self.assertRejected(CapabilityRejection.BAD_SIGNATURE, lambda: verify(f"{head}.{flipped}.{mac}"))
        bad_mac = ("A" if mac[0] != "A" else "B") + mac[1:]
        self.assertRejected(CapabilityRejection.BAD_SIGNATURE, lambda: verify(f"{head}.{payload}.{bad_mac}"))

    def test_malformed_tokens(self):
        for token in ["", "garbage", "v1.a.b.c", "v2.AAAA.AAAA", "v1.@@@.AAAA", "v1..", ".."]:
            with self.subTest(token=token):
                self.assertRejected(CapabilityRejection.MALFORMED, lambda t=token: verify(t))

    def test_wrong_key(self):
        self.assertRejected(CapabilityRejection.BAD_SIGNATURE, lambda: verify(issue(), secret_key=bytes(32)))

    def test_expiry_boundary(self):
        token = issue()
        verify(token, now_ms=NOW + 4999)
        self.assertRejected(CapabilityRejection.EXPIRED, lambda: verify(token, now_ms=NOW + 5000))

    def test_signature_is_checked_before_expiry(self):
        token = issue(secret_key=bytes(range(1, 33)))
        self.assertRejected(CapabilityRejection.BAD_SIGNATURE, lambda: verify(token, now_ms=NOW + 10**9))

    def test_binding_to_packet_action_and_state_version(self):
        token = issue()
        self.assertRejected(CapabilityRejection.PACKET_MISMATCH, lambda: verify(token, packet_id=PID2))
        self.assertRejected(
            CapabilityRejection.ACTION_MISMATCH,
            lambda: verify(token, action_hash=action_hash("ledger.adjust", {"sku": "META-001", "delta": -11})),
        )
        self.assertRejected(CapabilityRejection.STATE_VERSION_MISMATCH, lambda: verify(token, expected_state_version=8))

    def test_signature_compare_is_constant_time_primitive(self):
        # Risk: '==' on MACs leaks timing. We assert hmac.compare_digest is the comparison used.
        with mock.patch.object(v.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            verify(issue())
        self.assertEqual(spy.call_count, 1)

    def test_key_and_ttl_validation(self):
        with self.assertRaises(ValueError):
            issue(secret_key=b"short")
        with self.assertRaises(ValueError):
            verify(issue(), secret_key=b"short")
        for ttl in (0, -1, v.MAX_TTL_MS + 1):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                issue(ttl_ms=ttl)

    def test_agent_channel_cannot_claim_organic_provenance(self):
        base = dict(
            packet_id=PID,
            parent_state_version=0,
            tool_call={"tool_name": "x", "args": {}},
            lejdas_attestation=None,
            capability_token=None,
        )
        v.require_agent_provenance(v.DecisionPacket.model_validate({**base, "provenance": "SYNTHETIC_AGENT"}))
        with self.assertRaises(v.ProvenanceViolation):
            v.require_agent_provenance(v.DecisionPacket.model_validate({**base, "provenance": "ORGANIC_USER"}))


# --------------------------------------------------------------------------- #
# TOCTOU: capability + compare-and-swap
# --------------------------------------------------------------------------- #


class ToctouTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "core.db")
        self.conn = open_writer(self.path)
        self.conn.execute("CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INTEGER NOT NULL)")
        self.conn.execute("INSERT INTO stock VALUES ('META-001', 50)")
        register_scope(self.conn, "inventory", NOW)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def qty(self):
        return self.conn.execute("SELECT qty FROM stock").fetchone()[0]

    @staticmethod
    def adjust(delta):
        def mutation(conn):
            conn.execute("UPDATE stock SET qty = qty + ? WHERE sku = 'META-001'", (delta,))

        return mutation

    def run_packet(self, packet_id, delta, expected=0, conn=None, ttl=5000, now=NOW + 1):
        ah = action_hash("ledger.adjust", {"sku": "META-001", "delta": delta})
        token = v.issue_capability_token(
            secret_key=KEY, packet_id=packet_id, expected_state_version=expected, action_hash=ah, now_ms=NOW, ttl_ms=ttl
        )
        return execute_with_capability(
            conn or self.conn,
            secret_key=KEY,
            token=token,
            packet_id=packet_id,
            scope="inventory",
            expected_state_version=expected,
            tool_name="ledger.adjust",
            args={"sku": "META-001", "delta": delta},
            now_ms=now,
            mutation=self.adjust(delta),
        )

    def test_stale_state_version_aborts_without_mutation(self):
        # Two packets validated against version 0 (the check->use window). P1 commits first.
        self.assertIs(self.run_packet(PID, -10), ExecutionOutcome.APPLIED)
        with self.assertRaises(StaleStateError) as ctx:
            self.run_packet(PID2, -30)
        self.assertEqual((ctx.exception.expected, ctx.exception.actual), (0, 1))
        self.assertEqual(self.qty(), 40)  # only P1 applied
        self.assertEqual(read_head(self.conn, "inventory")[0], 1)

    def test_redelivered_packet_is_applied_once(self):
        self.assertIs(self.run_packet(PID, -10), ExecutionOutcome.APPLIED)
        self.assertIs(self.run_packet(PID, -10), ExecutionOutcome.ALREADY_APPLIED)
        self.assertEqual(self.qty(), 40)
        self.assertEqual(read_head(self.conn, "inventory")[0], 1)

    def test_same_packet_id_with_different_action_is_a_conflict(self):
        self.run_packet(PID, -10)
        with self.assertRaises(ReplayConflictError):
            self.run_packet(PID, -20)
        self.assertEqual(self.qty(), 40)

    def test_failed_mutation_rolls_back_the_version_bump(self):
        ah = action_hash("ledger.adjust", {"sku": "META-001", "delta": 1})
        token = v.issue_capability_token(
            secret_key=KEY, packet_id=PID, expected_state_version=0, action_hash=ah, now_ms=NOW, ttl_ms=5000
        )

        def boom(conn):
            conn.execute("UPDATE stock SET qty = 0")
            raise RuntimeError("mutation failed")

        with self.assertRaises(RuntimeError):
            execute_with_capability(
                self.conn, secret_key=KEY, token=token, packet_id=PID, scope="inventory",
                expected_state_version=0, tool_name="ledger.adjust",
                args={"sku": "META-001", "delta": 1}, now_ms=NOW + 1, mutation=boom,
            )
        self.assertEqual(self.qty(), 50)
        self.assertEqual(read_head(self.conn, "inventory")[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM applied_packets").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM execution_outbox").fetchone()[0], 0)

    def test_expired_or_mismatched_token_touches_nothing(self):
        with self.assertRaises(CapabilityRejected) as ctx:
            self.run_packet(PID, -10, ttl=1000, now=NOW + 1000)
        self.assertIs(ctx.exception.reason, CapabilityRejection.EXPIRED)
        # token minted for delta=-10 but a different mutation is attempted
        ah_ok = action_hash("ledger.adjust", {"sku": "META-001", "delta": -10})
        token = v.issue_capability_token(
            secret_key=KEY, packet_id=PID, expected_state_version=0, action_hash=ah_ok, now_ms=NOW, ttl_ms=5000
        )
        # the executed action is recomputed from tool_name/args, so a different action
        # cannot ride on a token issued for another one
        with self.assertRaises(CapabilityRejected) as ctx2:
            execute_with_capability(
                self.conn, secret_key=KEY, token=token, packet_id=PID, scope="inventory",
                expected_state_version=0, tool_name="ledger.adjust",
                args={"sku": "META-001", "delta": -50}, now_ms=NOW + 1, mutation=self.adjust(-50),
            )
        self.assertIs(ctx2.exception.reason, CapabilityRejection.ACTION_MISMATCH)
        self.assertEqual(self.qty(), 50)
        self.assertEqual(read_head(self.conn, "inventory")[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM execution_outbox").fetchone()[0], 0)

    def test_race_between_connections_has_exactly_one_winner(self):
        n = 8
        barrier = threading.Barrier(n)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(i):
            conn = open_writer(self.path)
            pid = f"{i:08x}-e89b-42d3-a456-426614174000"
            try:
                barrier.wait()
                self.run_packet(pid, -1, conn=conn)
                result = "applied"
            except StaleStateError:
                result = "stale"
            finally:
                conn.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("applied"), 1)
        self.assertEqual(outcomes.count("stale"), n - 1)
        self.assertEqual(self.qty(), 49)
        self.assertEqual(read_head(self.conn, "inventory")[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM execution_outbox").fetchone()[0], 1)

    def test_ledger_cannot_be_rewound_skipped_or_deleted_directly(self):
        for sql in (
            "UPDATE state_ledger SET state_version = 5",
            "UPDATE state_ledger SET state_version = state_version - 1",
            "DELETE FROM state_ledger",
            "INSERT OR REPLACE INTO state_ledger VALUES ('inventory', 0, '" + "0" * 64 + "', 0)",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        self.assertEqual(read_head(self.conn, "inventory")[0], 0)


# --------------------------------------------------------------------------- #
# Waterfilling context allocator
# --------------------------------------------------------------------------- #


def blocks(system=100, tools=200, intent=50, out=150):
    return InelasticBlocks(
        system_prompt_tokens=system, tool_schema_tokens=tools, last_user_intent_tokens=intent, output_buffer_tokens=out
    )


def hist(n, tokens=100, anchors=()):
    return [HistoryPair(pair_id=f"h{i}", seq=i, tokens=tokens, anchor=i in anchors) for i in range(n)]


def chunks(scores, tokens=100):
    return [RagChunk(chunk_id=f"c{i}", tokens=tokens, relevance_score=s) for i, s in enumerate(scores)]


class WaterfillTests(unittest.TestCase):
    def test_everything_fits(self):
        self.assertEqual(waterfill({"a": 10, "b": 20}, 100), {"a": 10, "b": 20})

    def test_small_demand_is_fully_served_and_rest_goes_to_the_big_one(self):
        self.assertEqual(waterfill({"a": 10, "b": 500}, 100), {"a": 10, "b": 90})

    def test_equal_oversubscribed_demands_split_evenly_with_deterministic_remainder(self):
        self.assertEqual(waterfill({"a": 500, "b": 500}, 101), {"a": 51, "b": 50})

    def test_allocation_never_exceeds_demand_or_pool(self):
        rnd = random.Random(7)
        for _ in range(500):
            demands = {n: rnd.randint(0, 300) for n in "abc"}
            pool = rnd.randint(0, 600)
            alloc = waterfill(demands, pool)
            self.assertTrue(all(alloc[n] <= demands[n] for n in demands))
            self.assertEqual(sum(alloc.values()), min(pool, sum(demands.values())))


class ContextAllocatorTests(unittest.TestCase):
    def alloc(self, window=2000, cap=1000):
        return WaterfillingContextAllocator(AllocatorConfig(context_window=window, hard_inelastic_cap=cap))

    def test_inelastic_over_hard_cap_raises_before_any_truncation(self):
        with self.assertRaises(ContextConfigurationError):
            self.alloc(cap=400).allocate(blocks(), [], [])  # 500 > 400

    def test_no_room_left_for_elastic_pool_raises(self):
        with self.assertRaises(ContextConfigurationError):
            self.alloc(window=500, cap=1000).allocate(blocks(), [], [])  # 500 >= 500

    def test_tool_schemas_are_never_cut_when_the_window_is_tight(self):
        plan = self.alloc(window=600, cap=600).allocate(blocks(tools=200), hist(5), chunks([0.9, 0.5]))
        self.assertEqual(plan.inelastic_tokens, 500)
        self.assertEqual(plan.elastic_pool, 100)
        self.assertLessEqual(plan.total_tokens, 600)

    def test_large_window_keeps_everything(self):
        plan = self.alloc(window=128_000, cap=4000).allocate(blocks(), hist(40), chunks([0.1 * i for i in range(30)]))
        self.assertEqual(plan.evictions, ())
        self.assertEqual(len(plan.history), 40)
        self.assertEqual(len(plan.rag), 30)

    def test_history_evicts_oldest_first_and_rag_evicts_lowest_relevance_first(self):
        # inelastic 500, window 1500 -> pool 1000; demands 800 + 800 -> 500 each
        plan = self.alloc(window=1500, cap=1000).allocate(
            blocks(), hist(8), chunks([0.9, 0.1, 0.5, 0.7, 0.2, 0.3, 0.8, 0.4])
        )
        kept_history = [p.pair_id for p in plan.history]
        self.assertEqual(kept_history, ["h3", "h4", "h5", "h6", "h7"])  # newest 5 kept
        self.assertEqual({e.item_id for e in plan.evictions if e.kind == "history"}, {"h0", "h1", "h2"})
        self.assertEqual([c.chunk_id for c in plan.rag], ["c0", "c6", "c3", "c2", "c7"])  # top-5 by score
        evicted_rag = [e.item_id for e in plan.evictions if e.kind == "rag"]
        self.assertEqual(evicted_rag[0], "c1")  # lowest relevance goes first
        self.assertLessEqual(plan.total_tokens, 1500)

    def test_anchors_survive_and_are_charged_to_the_inelastic_reserve(self):
        plan = self.alloc(window=1200, cap=1000).allocate(blocks(), hist(6, anchors={0}), chunks([0.5, 0.4]))
        self.assertIn("h0", [p.pair_id for p in plan.history])
        self.assertEqual(plan.inelastic_tokens, 600)  # 500 + anchor 100
        self.assertNotIn("h0", [e.item_id for e in plan.evictions])

    def test_anchors_alone_can_trip_the_hard_cap(self):
        with self.assertRaises(ContextConfigurationError):
            self.alloc(window=5000, cap=550).allocate(blocks(), hist(3, anchors={0, 1}), [])

    def test_plan_is_independent_of_input_order(self):
        h, r = hist(9, tokens=90), chunks([0.3, 0.9, 0.5, 0.1, 0.7, 0.2, 0.6], tokens=130)
        reference = self.alloc(window=1700, cap=1000).allocate(blocks(), h, r)
        rnd = random.Random(3)
        for _ in range(25):
            h2, r2 = h[:], r[:]
            rnd.shuffle(h2)
            rnd.shuffle(r2)
            self.assertEqual(self.alloc(window=1700, cap=1000).allocate(blocks(), h2, r2), reference)

    def test_random_scenarios_keep_core_invariants(self):
        rnd = random.Random(11)
        for _ in range(400):
            window = rnd.randint(800, 6000)
            allocator = self.alloc(window=window, cap=window - 1)
            n_hist, n_rag = rnd.randint(0, 12), rnd.randint(0, 12)
            h = [
                HistoryPair(pair_id=f"h{i}", seq=i, tokens=rnd.randint(0, 400), anchor=rnd.random() < 0.15)
                for i in range(n_hist)
            ]
            r = [RagChunk(chunk_id=f"c{i}", tokens=rnd.randint(0, 400), relevance_score=rnd.random()) for i in range(n_rag)]
            try:
                plan = allocator.allocate(blocks(), h, r)
            except ContextConfigurationError:
                continue
            self.assertLessEqual(plan.total_tokens, window)
            kept = {p.pair_id for p in plan.history} | {c.chunk_id for c in plan.rag}
            gone = {e.item_id for e in plan.evictions}
            self.assertEqual(kept & gone, set())
            self.assertEqual(kept | gone, {p.pair_id for p in h} | {c.chunk_id for c in r})
            for p in h:
                if p.anchor:
                    self.assertIn(p.pair_id, kept)


if __name__ == "__main__":
    unittest.main()
