"""Configuration tests: .env.example, the restricted YAML parser, the gateway manifest."""

import contextlib
import copy
import importlib.util
import os
import io
import secrets
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.config import (  # noqa: E402
    ConfigError,
    Settings,
    check_budget_against_settings,
    load_gateway_manifest,
    parse_env_text,
    parse_simple_yaml,
    validate_gateway_manifest,
)

HAS_YAML = importlib.util.find_spec("yaml") is not None
SECRET = secrets.token_hex(32)


def manifest():
    return copy.deepcopy(parse_simple_yaml((ROOT / "config" / "ai_gateway.yaml").read_text(encoding="utf-8")))


class EnvTests(unittest.TestCase):
    def setUp(self):
        self.example = parse_env_text((ROOT / ".env.example").read_text(encoding="utf-8"))

    def test_example_declares_the_documented_keys(self):
        for key in ("META_CORE_HMAC_SECRET", "SQLITE_DB_PATH", "MAX_TOKEN_BUDGET", "CAPABILITY_TTL_MS",
                    "CORE_ID", "EVENT_LOG_PATH", "MAX_HANDOFF_DEPTH", "HEARTBEAT_TIMEOUT_S"):
            self.assertIn(key, self.example)

    def test_example_ships_no_secret_and_refuses_to_start_unchanged(self):
        # Risk: a committed default key is a known key.
        self.assertEqual(self.example["META_CORE_HMAC_SECRET"], "")
        with self.assertRaises(ConfigError):
            Settings.from_env(self.example)

    def test_example_with_a_generated_secret_loads(self):
        settings = Settings.from_env({**self.example, "META_CORE_HMAC_SECRET": SECRET})
        self.assertEqual(len(settings.hmac_secret), 32)
        self.assertEqual(settings.max_token_budget, 20000)
        self.assertEqual(settings.capability_ttl_ms, 5000)
        self.assertEqual(str(settings.sqlite_db_path), "data/meta_core.db")

    def test_weak_or_malformed_secrets_are_rejected(self):
        for bad in ("", "abc", "g" * 64, "0" * 64, "ab" * 32, SECRET[:-1], SECRET + "0"):
            with self.subTest(secret=bad[:12]), self.assertRaises(ConfigError):
                Settings.from_env({"META_CORE_HMAC_SECRET": bad})

    def test_numeric_bounds(self):
        base = {"META_CORE_HMAC_SECRET": SECRET}
        for key, value in (("MAX_TOKEN_BUDGET", "0"), ("MAX_TOKEN_BUDGET", "x"), ("CAPABILITY_TTL_MS", "0"),
                           ("CAPABILITY_TTL_MS", "60001")):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                Settings.from_env({**base, key: value})

    def test_the_example_secret_is_accepted_by_the_token_code(self):
        from src.control import validator as v

        settings = Settings.from_env({"META_CORE_HMAC_SECRET": SECRET})
        token = v.issue_capability_token(
            secret_key=settings.hmac_secret, packet_id="123e4567-e89b-42d3-a456-426614174000",
            expected_state_version=1, action_hash="a" * 64, now_ms=1, ttl_ms=settings.capability_ttl_ms,
        )
        self.assertTrue(token.startswith("v1."))

    def test_env_parser_rejects_garbage_and_duplicates(self):
        for text in ("not a pair", "lower=case", "A=1\nA=2"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_env_text(text)
        self.assertEqual(parse_env_text('# c\n\nA="x y"\nB=\'z\''), {"A": "x y", "B": "z"})


class MiniYamlTests(unittest.TestCase):
    def test_supported_subset(self):
        doc = parse_simple_yaml(
            "a: 1  # comment\nb:\n  c: true\n  d: [x, 'y, z', 2.5]\n  e: ~\n  f: \"q # not a comment\"\n"
            "g:\n  - one\n  - 2\nh: []\n"
        )
        self.assertEqual(doc, {"a": 1, "b": {"c": True, "d": ["x", "y, z", 2.5], "e": None,
                                             "f": "q # not a comment"}, "g": ["one", 2], "h": []})

    def test_unsupported_constructs_fail_closed(self):
        for text in ("a: &x 1", "a: *x", "a: |\n  text", "a: {b: 1}", "a: [[1]]", "a:\n\tb: 1", "a:\n b: 1",
                     "a: 1\na: 2", "- x", "a:", "a: [1, 2", "a:\n  - b: 1", "a: 'open"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_simple_yaml(text)

    @unittest.skipUnless(HAS_YAML, "PyYAML not installed (optional cross-check only)")
    def test_agrees_with_pyyaml_on_the_repository_manifest(self):
        import yaml

        text = (ROOT / "config" / "ai_gateway.yaml").read_text(encoding="utf-8")
        self.assertEqual(parse_simple_yaml(text), yaml.safe_load(text))


class GatewayManifestTests(unittest.TestCase):
    def assertRejected(self, mutate):
        data = manifest()
        mutate(data)
        with self.assertRaises(ConfigError):
            validate_gateway_manifest(data)

    def test_repository_manifest_is_valid_and_matches_adr_0012(self):
        m = load_gateway_manifest(ROOT / "config" / "ai_gateway.yaml")
        route = m.routes["reasoning-decision"]
        self.assertFalse(route.semantic_cache.enabled)
        self.assertFalse(route.exact_cache.enabled)  # off by default
        self.assertEqual(route.max_retries, 0)
        self.assertTrue(m.routes["reference-docs"].semantic_cache.enabled)  # static docs only
        self.assertFalse(m.routes["reference-docs"].tools_in_scope)
        self.assertEqual(m.egress.default_action, "deny")

    def test_semantic_cache_on_the_decision_route_is_rejected(self):
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"]["semantic_cache"].update(enabled=True))

    def test_semantic_cache_on_any_route_with_tools_or_packets_is_rejected(self):
        def tools(d):
            d["routes"]["reference-docs"]["tools_in_scope"] = True

        def packets(d):
            d["routes"]["reference-docs"]["produces_decision_packet"] = True

        self.assertRejected(tools)
        self.assertRejected(packets)

    def test_decision_route_is_mandatory_and_must_declare_what_it_is(self):
        self.assertRejected(lambda d: d["routes"].pop("reasoning-decision"))
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"].update(tools_in_scope=False))
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"].update(produces_decision_packet=False))

    def test_exact_cache_must_be_state_bound(self):
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"]["exact_cache"].update(key="prompt_only"))
        self.assertRejected(
            lambda d: d["routes"]["reasoning-decision"]["exact_cache"].update(
                key_fields=["model_id", "canonical_prompt_payload"]
            )
        )

    def test_egress_must_fail_closed_with_plain_hosts_and_tls(self):
        self.assertRejected(lambda d: d["egress"].update(default_action="allow"))
        self.assertRejected(lambda d: d["egress"].update(allowed_hosts=["*"]))
        self.assertRejected(lambda d: d["egress"].update(allowed_hosts=["*.example.com"]))
        self.assertRejected(lambda d: d["egress"].update(allowed_hosts=["https://x.example"]))
        self.assertRejected(lambda d: d["egress"].update(allowed_hosts=[]))
        self.assertRejected(lambda d: d["egress"].update(tls_required=False))

    def test_timeouts_must_be_finite_and_ordered(self):
        for timeouts in ({"connect": 0, "read": 10, "total": 20}, {"connect": 5, "read": 5, "total": 20},
                         {"connect": 3, "read": 30, "total": 20}, {"connect": 3, "read": 30, "total": 100000}):
            with self.subTest(timeouts=timeouts):
                self.assertRejected(lambda d, t=timeouts: d["routes"]["reasoning-decision"].update(timeouts_s=t))

    def test_limits_and_retries_are_bounded(self):
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"]["limits"].update(max_output_tokens=0))
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"]["limits"].update(max_request_budget_tokens=10**9))
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"].update(max_retries=5))

    def test_unknown_keys_are_rejected(self):
        self.assertRejected(lambda d: d.update(surprise=1))
        self.assertRejected(lambda d: d["routes"]["reasoning-decision"].update(surprise=1))

    def test_route_budgets_must_fit_the_run_budget(self):
        m = load_gateway_manifest(ROOT / "config" / "ai_gateway.yaml")
        ok = Settings.from_env({"META_CORE_HMAC_SECRET": SECRET, "MAX_TOKEN_BUDGET": "20000"})
        check_budget_against_settings(m, ok)
        tight = Settings.from_env({"META_CORE_HMAC_SECRET": SECRET, "MAX_TOKEN_BUDGET": "10000"})
        with self.assertRaises(ConfigError):
            check_budget_against_settings(m, tight)  # reasoning-decision needs 18048

    def test_command_line_check(self):
        out, err = io.StringIO(), io.StringIO()
        bad = ROOT / "tests" / "_bad_manifest.tmp"
        try:
            bad.write_text("version: 1\n", encoding="utf-8")
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(config.main(["config"]), 0)
                self.assertEqual(config.main(["config", str(bad)]), 1)
                self.assertEqual(config.main(["config", str(ROOT / "nope.yaml")]), 1)
        finally:
            bad.unlink(missing_ok=True)
        self.assertIn("semantic cache off", out.getvalue())
        self.assertEqual(err.getvalue().count("FAIL"), 2)


if __name__ == "__main__":
    unittest.main()
