import os, sys, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telemetry.observer import analyze, span_depths

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
T = "a" * 32


def ev(kind, core="c1", span=None, parent=None, ago=0, trace=T, **kw):
    e = {"schema": "1.0", "event": kind, "core_id": core, "event_id": f"{kind}{span}{ago}",
         "timestamp": (NOW - timedelta(seconds=ago)).isoformat().replace("+00:00", "Z"), **kw}
    if span:
        e.update(trace_id=trace, span_id=span, parent_span_id=parent)
    return e


def s(i):
    return f"{i:016x}"


class ObserverTests(unittest.TestCase):
    def test_depths(self):
        d, cyc = span_depths({s(1): None, s(2): s(1), s(3): s(2)})
        self.assertEqual(d, {s(1): 0, s(2): 1, s(3): 2})
        self.assertEqual(cyc, [])

    def test_cycle(self):
        _, cyc = span_depths({s(1): s(2), s(2): s(1)})
        self.assertEqual(cyc, [s(1), s(2)])

    def test_healthy(self):
        r = analyze([ev("CORE_STARTED"), ev("STEP_STARTED", span=s(1)),
                     ev("STEP_COMPLETED", span=s(2), parent=s(1))], now=NOW)
        self.assertEqual(r["status"], "OK")

    def test_handoff_depth_critical(self):
        chain = [ev("STEP_STARTED", span=s(1))]
        chain += [ev("STEP_STARTED", span=s(i), parent=s(i - 1)) for i in range(2, 9)]
        r = analyze(chain, max_depth=5, now=NOW)
        self.assertEqual(r["status"], "CRITICAL")
        self.assertEqual(r["traces"][T]["max_depth"], 7)

    def test_orphan_spawn_and_stale(self):
        r = analyze([ev("SPAWN_REQUESTED", span=s(1), ago=1000)], hb_timeout=300, now=NOW)
        self.assertEqual(r["status"], "WARN")
        self.assertEqual(r["orphan_spawns"], [s(1)])
        self.assertEqual(r["stale_cores"], ["c1"])

    def test_spawn_completed_clears_orphan(self):
        r = analyze([ev("SPAWN_REQUESTED", span=s(1)),
                     ev("SPAWN_COMPLETED", core="child", span=s(2), parent=s(1))], now=NOW)
        self.assertEqual(r["orphan_spawns"], [])

    def test_stopped_core_not_stale(self):
        r = analyze([ev("CORE_STOPPED", ago=5000)], now=NOW)
        self.assertEqual(r["stale_cores"], [])

    def test_fatal_is_critical(self):
        self.assertEqual(analyze([ev("FATAL_ERROR")], now=NOW)["status"], "CRITICAL")


if __name__ == "__main__":
    unittest.main()
