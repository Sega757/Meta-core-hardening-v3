import json, os, subprocess, sys, tempfile, unittest
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from telemetry.transponder import ValidationError, emit, validate_and_enrich

CLI = os.path.join(ROOT, "telemetry", "transponder.py")


def _write(args):
    path, i = args
    emit({"event": "HEARTBEAT", "core_id": f"c{i % 4}", "data": {"pad": "x" * 2000, "i": i}}, path)


class TransponderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ev.jsonl")

    def run_cli(self, arg, **env):
        e = {**os.environ, "EVENT_LOG_PATH": self.path, **env}
        e.pop("CORE_ID", None) if "CORE_ID" not in env else None
        return subprocess.run([sys.executable, CLI, arg], env=e, capture_output=True, text=True)

    def test_enrich(self):
        ev = validate_and_enrich({"event": "HEARTBEAT", "core_id": "c1"})
        self.assertEqual(ev["schema"], "1.0")
        self.assertIn("event_id", ev)
        self.assertTrue(ev["timestamp"].endswith("Z"))
        self.assertNotIn("trace_id", ev)  # heartbeats stay light

    def test_execution_events_get_trace(self):
        ev = validate_and_enrich({"event": "STEP_STARTED", "core_id": "c1"})
        self.assertEqual(len(ev["trace_id"]), 32)
        self.assertEqual(len(ev["span_id"]), 16)

    def test_core_id_from_env(self):
        ev = validate_and_enrich({"event": "HEARTBEAT"}, core_id_env="env-core")
        self.assertEqual(ev["core_id"], "env-core")

    def test_rejections(self):
        for bad in ({"core_id": "c"}, {"event": "step_ok", "core_id": "c"},
                    {"event": "HEARTBEAT"}, {"event": "HEARTBEAT", "core_id": "c", "trace_id": "xyz"},
                    {"event": "HEARTBEAT", "core_id": "c", "data": "notobj"},
                    {"event": "HEARTBEAT", "core_id": "c", "schema": "2.0"}):
            with self.assertRaises(ValidationError, msg=bad):
                validate_and_enrich(dict(bad))

    def test_cli_exit_codes(self):
        self.assertEqual(self.run_cli('{"event":"HEARTBEAT","core_id":"c"}').returncode, 0)
        self.assertEqual(self.run_cli("{bad json").returncode, 3)
        self.assertEqual(self.run_cli('{"event":"done","core_id":"c"}').returncode, 4)
        with open(self.path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_concurrent_writes_no_corruption(self):
        n = 400
        with Pool(8) as p:
            p.map(_write, [(self.path, i) for i in range(n)])
        with open(self.path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), n)
        ids = {json.loads(l)["event_id"] for l in lines}
        self.assertEqual(len(ids), n)


if __name__ == "__main__":
    unittest.main()
