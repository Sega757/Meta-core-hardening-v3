"""Tier 1 observability tests (stdlib only)."""

import json
import math
import os
import statistics
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.observability.observer import (  # noqa: E402
    RuntimeAnomalyDetector,
    Tier1Observer,
    TraceDAG,
    payload_size_bytes,
)
from telemetry.observer import analyze, read_events  # noqa: E402

T = "a" * 32
BASE = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def sid(i):
    return f"{i:016x}"


def ev(kind, span, parent=None, at=0.0, trace=T, **kw):
    ts = (BASE + timedelta(milliseconds=at)).isoformat().replace("+00:00", "Z")
    return {"schema": "1.0", "event": kind, "core_id": "c1", "event_id": f"{kind}{span}{at}",
            "timestamp": ts, "trace_id": trace, "span_id": span, "parent_span_id": parent,
            "execution_step": f"step-{span[-2:]}", **kw}


class SpanTreeTests(unittest.TestCase):
    def test_tree_depth_children_and_latency(self):
        dag = TraceDAG(T)
        for e in (
            ev("STEP_STARTED", sid(1), None, at=0),
            ev("STEP_STARTED", sid(2), sid(1), at=10),
            ev("STEP_STARTED", sid(3), sid(2), at=20),
            ev("STEP_COMPLETED", sid(3), sid(2), at=45),
            ev("STEP_COMPLETED", sid(2), sid(1), at=60),
            ev("STEP_FAILED", sid(1), None, at=100),
        ):
            dag.add_event(e)
        self.assertEqual(dag.depths(), {sid(1): 0, sid(2): 1, sid(3): 2})
        self.assertEqual(dag.max_depth(), 2)
        self.assertEqual(dag.spans[sid(1)].children, [sid(2)])
        self.assertEqual(dag.spans[sid(2)].children, [sid(3)])
        self.assertEqual([n.span_id for n in dag.roots()], [sid(1)])
        self.assertAlmostEqual(dag.latency_ms(sid(3)), 25.0)
        self.assertAlmostEqual(dag.latency_ms(sid(1)), 100.0)
        self.assertEqual(dag.spans[sid(3)].status, "ok")
        self.assertEqual(dag.spans[sid(1)].status, "failed")

    def test_child_seen_before_parent_is_attached_when_the_parent_arrives(self):
        dag = TraceDAG(T)
        dag.add_event(ev("STEP_STARTED", sid(2), sid(1)))
        self.assertEqual([n.span_id for n in dag.roots()], [sid(2)])  # parent not seen yet
        dag.add_event(ev("STEP_STARTED", sid(1), None))
        self.assertEqual(dag.spans[sid(1)].children, [sid(2)])
        self.assertEqual(dag.depths()[sid(2)], 1)

    def test_parent_cycle_is_reported_not_followed_forever(self):
        # Risk: a malformed trace with A -> B -> A must not hang the observer.
        dag = TraceDAG(T)
        dag.add_event(ev("STEP_STARTED", sid(1), sid(2)))
        dag.add_event(ev("STEP_STARTED", sid(2), sid(1)))
        self.assertEqual(sorted(dag.cycles()), [sid(1), sid(2)])
        self.assertEqual(set(dag.depths().values()), {-1})

    def test_other_traces_and_irrelevant_events_are_ignored(self):
        dag = TraceDAG(T)
        self.assertIsNone(dag.add_event(ev("STEP_STARTED", sid(1), trace="b" * 32)))
        self.assertIsNone(dag.add_event({**ev("HEARTBEAT", sid(1))}))
        self.assertIsNone(dag.add_event({"event": "STEP_STARTED", "trace_id": T}))  # no span_id
        self.assertEqual(dag.spans, {})

    def test_missing_or_bad_timestamps_give_no_latency(self):
        dag = TraceDAG(T)
        dag.add_event({**ev("STEP_STARTED", sid(1)), "timestamp": "garbage"})
        dag.add_event(ev("STEP_COMPLETED", sid(1), at=5))
        self.assertIsNone(dag.latency_ms(sid(1)))
        self.assertIsNone(dag.latency_ms(sid(99)))


class SpanWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "event_stream.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_written_event_is_fsynced(self):
        # Risk: a span event lost on crash because the append was only flushed.
        dag = TraceDAG(T, log_path=self.path)
        with mock.patch("os.fsync", wraps=os.fsync) as fsync:
            root = dag.start_span("perceive", core_id="core-001")
            child = dag.start_span("think", core_id="core-001", parent_span_id=root.span_id)
            dag.end_span(child.span_id)
            dag.end_span(root.span_id, ok=False, data={"error": "boom"})
        self.assertEqual(fsync.call_count, 4)
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 4)
        kinds = [json.loads(line)["event"] for line in lines]
        self.assertEqual(kinds, ["STEP_STARTED", "STEP_STARTED", "STEP_COMPLETED", "STEP_FAILED"])

    def test_written_spans_form_the_expected_tree_and_pass_the_existing_observer(self):
        dag = TraceDAG(T, log_path=self.path)
        root = dag.start_span("perceive", core_id="core-001")
        child = dag.start_span("think", core_id="core-001", parent_span_id=root.span_id)
        dag.end_span(child.span_id)
        dag.end_span(root.span_id)
        self.assertEqual(dag.depths(), {root.span_id: 0, child.span_id: 1})
        self.assertEqual(dag.spans[root.span_id].children, [child.span_id])
        self.assertGreaterEqual(dag.latency_ms(root.span_id), 0.0)
        events, warnings = read_events(self.path)
        self.assertEqual(warnings, [])
        report = analyze(events, now=datetime.now(timezone.utc))
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["traces"][T]["spans"], 2)
        # a second DAG rebuilt purely from the file agrees with the one built while writing
        rebuilt = TraceDAG(T)
        for e in events:
            rebuilt.add_event(e)
        self.assertEqual(rebuilt.depths(), dag.depths())

    def test_invalid_events_are_rejected_by_the_shared_writer(self):
        dag = TraceDAG("not-a-trace-id", log_path=self.path)
        with self.assertRaises(ValueError):
            dag.start_span("x", core_id="c")
        self.assertFalse(os.path.exists(self.path))


class DetectorTests(unittest.TestCase):
    BASELINE = [10, 11, 9, 10, 12, 10, 11, 9, 10, 10]  # median 10, MAD 0.5

    def warmed(self, **kw):
        det = RuntimeAnomalyDetector(**kw)
        for x in self.BASELINE:
            det.observe("latency_ms", x)
        return det

    def test_modified_z_score_matches_the_formula(self):
        v = self.warmed().observe("latency_ms", 20.0)
        median = statistics.median(self.BASELINE)
        mad = statistics.median([abs(x - median) for x in self.BASELINE])
        self.assertEqual((median, mad), (10, 0.5))
        self.assertAlmostEqual(v.modified_z, 0.6745 * abs(20 - median) / mad)
        self.assertTrue(v.is_anomaly)
        self.assertTrue(v.warmed_up)

    def test_threshold_is_exclusive_at_3_5(self):
        # z = 0.6745 * d / 0.5 = 1.349 * d  -> d = 2.5 gives 3.3725 (ok), d = 2.7 gives 3.6423
        det = self.warmed()
        self.assertFalse(det.observe("latency_ms", 12.5).is_anomaly)
        det = self.warmed()
        self.assertTrue(det.observe("latency_ms", 12.7).is_anomaly)

    def test_huber_loss_is_quadratic_inside_delta_and_linear_outside(self):
        delta = 1.345
        inside = self.warmed().observe("latency_ms", 10.5)  # r = 0.6745 -> r^2/2
        self.assertAlmostEqual(inside.huber_loss, 0.5 * inside.modified_z**2)
        outside = self.warmed().observe("latency_ms", 20.0)  # r = 13.49 -> linear branch
        self.assertAlmostEqual(outside.huber_loss, delta * (outside.modified_z - 0.5 * delta))
        self.assertLess(outside.huber_loss, 0.5 * outside.modified_z**2)  # robust: grows linearly

    def test_cold_start_never_flags_and_records_values(self):
        det = RuntimeAnomalyDetector(min_samples=10)
        verdicts = [det.observe("latency_ms", x) for x in [10] * 5 + [10_000]]
        self.assertFalse(any(v.warmed_up or v.is_anomaly for v in verdicts))
        self.assertEqual(det.window_size("latency_ms"), 6)
        for x in [10] * 4:
            det.observe("latency_ms", x)
        self.assertTrue(det.observe("latency_ms", 10.0).warmed_up)  # 10 prior values

    def test_window_is_bounded_regardless_of_stream_length(self):
        det = RuntimeAnomalyDetector(window=32, min_samples=5)
        for i in range(5000):
            det.observe("latency_ms", 10 + (i % 7))
        self.assertEqual(det.window_size("latency_ms"), 32)

    def test_metrics_have_independent_windows(self):
        det = RuntimeAnomalyDetector(min_samples=5, window=20)
        for _ in range(6):
            det.observe("latency_ms", 10.0 + _)
        self.assertFalse(det.observe("payload_bytes", 5_000.0).warmed_up)  # separate, still cold

    def test_zero_mad_falls_back_to_mean_absolute_deviation(self):
        # 10 of 12 values equal -> MAD = 0, but the mean absolute deviation is not.
        det = RuntimeAnomalyDetector(min_samples=12, window=64)
        for x in [10] * 10 + [14, 14]:
            det.observe("latency_ms", x)
        v = det.observe("latency_ms", 30.0)
        self.assertEqual(v.mad, 0)
        sigma = 1.253314 * (4 * 2 / 12)  # mean AD around the median (10): (0*10 + 4*2) / 12
        self.assertAlmostEqual(v.modified_z, abs(30 - 10) / sigma)
        self.assertTrue(v.is_anomaly)

    def test_constant_window_uses_the_scale_floor(self):
        det = RuntimeAnomalyDetector(min_samples=5, window=20, scale_floor=1.0)
        for _ in range(8):
            det.observe("payload_bytes", 100.0)
        self.assertFalse(det.observe("payload_bytes", 100.0).is_anomaly)
        self.assertFalse(det.observe("payload_bytes", 101.0).is_anomaly)  # z = 1.0 < 3.5
        self.assertTrue(det.observe("payload_bytes", 500.0).is_anomaly)

    def test_robust_to_outliers_already_in_the_window(self):
        # Risk: mean/std Z-score is masked by earlier outliers; MAD is not.
        det = RuntimeAnomalyDetector(min_samples=10, window=64)
        series = [10, 11, 9, 10, 12, 10, 300, 11, 9, 10, 400, 10]
        for x in series:
            det.observe("latency_ms", x)
        self.assertTrue(det.observe("latency_ms", 60.0).is_anomaly)
        mean, std = statistics.fmean(series), statistics.pstdev(series)
        self.assertLess(abs(60 - mean) / std, 3.5)  # the classical Z-score would have missed it

    def test_rejects_non_finite_values_and_bad_config(self):
        det = RuntimeAnomalyDetector()
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                det.observe("latency_ms", bad)
        for kw in ({"min_samples": 2}, {"window": 5, "min_samples": 10}, {"z_threshold": 0},
                   {"huber_delta": -1}, {"scale_floor": 0}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                RuntimeAnomalyDetector(**kw)

    def test_is_deterministic(self):
        series = [10 + (i * 7) % 5 for i in range(200)] + [95]
        a, b = RuntimeAnomalyDetector(), RuntimeAnomalyDetector()
        self.assertEqual([a.observe("m", x) for x in series], [b.observe("m", x) for x in series])


class Tier1ObserverTests(unittest.TestCase):
    def feed(self, obs, n, slow_at=None):
        out = []
        for i in range(n):
            span = sid(i + 1)
            duration = 500.0 if slow_at == i else 20.0 + (i % 5)
            out += obs.ingest(ev("STEP_STARTED", span, None, at=i * 1000, trace=f"{i:032x}"))
            out += obs.ingest(ev("STEP_COMPLETED", span, None, at=i * 1000 + duration, trace=f"{i:032x}",
                                 data={"k": "v" * (i % 3)}))
        return out

    def test_slow_span_is_flagged_only_after_warm_up(self):
        obs = Tier1Observer(RuntimeAnomalyDetector(min_samples=10, window=64))
        verdicts = self.feed(obs, 30, slow_at=20)
        latency = [v for v in verdicts if v.metric == "latency_ms"]
        self.assertEqual(len(latency), 30)
        flagged = [v for v in latency if v.is_anomaly]
        self.assertEqual([v.value for v in flagged], [500.0])
        self.assertTrue(all(not v.warmed_up for v in latency[:10]))

    def test_slow_span_during_cold_start_is_not_flagged(self):
        obs = Tier1Observer(RuntimeAnomalyDetector(min_samples=10))
        verdicts = self.feed(obs, 5, slow_at=2)
        self.assertFalse(any(v.is_anomaly for v in verdicts))

    def test_payload_size_is_measured_and_oversized_payloads_flagged(self):
        obs = Tier1Observer(RuntimeAnomalyDetector(min_samples=10))
        for i in range(15):
            obs.ingest(ev("STEP_STARTED", sid(i + 1), trace=f"{i:032x}", data={"q": "x" * 40}))
        big = obs.ingest(ev("STEP_STARTED", sid(99), trace="c" * 32, data={"q": "x" * 5000}))
        self.assertEqual([v.metric for v in big], ["payload_bytes"])
        self.assertTrue(big[0].is_anomaly)
        self.assertEqual(payload_size_bytes({"data": {"q": "é"}}), len('{"q":"é"}'.encode()))
        self.assertEqual(payload_size_bytes({"data": {}}), 0)

    def test_memory_is_bounded_by_max_traces(self):
        obs = Tier1Observer(max_traces=50)
        for i in range(500):
            obs.ingest(ev("STEP_STARTED", sid(1), trace=f"{i:032x}"))
        self.assertEqual(len(obs.traces), 50)
        self.assertIn(f"{499:032x}", obs.traces)
        self.assertNotIn(f"{0:032x}", obs.traces)


if __name__ == "__main__":
    unittest.main()
