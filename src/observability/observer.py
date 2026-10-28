"""Tier 1 observability (ADR-0015): span trees and a robust runtime anomaly detector.

* ``SpanNode`` / ``TraceDAG`` - one trace as a tree of spans, built from the
  ``event_stream.jsonl`` events (``trace_id`` / ``span_id`` / ``parent_span_id``) and
  optionally *written* through ``telemetry.transponder``, which validates the event
  against the v1.0 schema and appends it under an exclusive lock with ``flush`` +
  ``os.fsync``. There is deliberately one durable writer, not two.
* ``RuntimeAnomalyDetector`` - Modified Z-score (MAD) plus Huber loss over a fixed
  sliding window, with a cold-start guard. No model call anywhere.
* ``Tier1Observer`` - glue: feed it events, get verdicts for step latency and
  payload size.

Cost model. Per event the work is bounded by the window size ``W`` and does not grow
with the length of the log: O(W log W) with an exact median over a constant ``W``
(O(1) in stream length). It is not O(1) in ``W``; strict O(1) would need approximate
streaming quantiles, which this module does not use. Not thread-safe: one consumer.

Standard library only (plus the repo's own ``telemetry`` package).
"""

from __future__ import annotations

import json
import math
import secrets
import statistics
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Mapping

from telemetry.observer import parse_ts, span_depths
from telemetry.transponder import OBJECT_FIELDS, emit

START_EVENTS: Final[frozenset[str]] = frozenset({"STEP_STARTED", "SPAWN_REQUESTED"})
OK_EVENTS: Final[frozenset[str]] = frozenset({"STEP_COMPLETED", "SPAWN_COMPLETED"})
FAIL_EVENTS: Final[frozenset[str]] = frozenset({"STEP_FAILED"})

SpanStatus = Literal["open", "ok", "failed"]


# --------------------------------------------------------------------------- #
# Span tree
# --------------------------------------------------------------------------- #


@dataclass
class SpanNode:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    core_id: str
    name: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: SpanStatus = "open"
    children: list[str] = field(default_factory=list)  # span_ids


class TraceDAG:
    """All spans of one trace. Parent cycles are representable (and reported)."""

    def __init__(self, trace_id: str, log_path: str | None = None) -> None:
        self.trace_id = trace_id
        self._log_path = log_path
        self.spans: dict[str, SpanNode] = {}
        self._waiting: dict[str, list[str]] = {}  # parent id -> children seen first

    # ---- building from events -------------------------------------------------

    def add_event(self, event: Mapping[str, Any]) -> SpanNode | None:
        """Fold one event into the tree; returns the touched span, if any."""
        kind = event.get("event")
        span_id = event.get("span_id")
        if event.get("trace_id") != self.trace_id or not span_id:
            return None
        if kind not in START_EVENTS | OK_EVENTS | FAIL_EVENTS:
            return None
        node = self.spans.get(span_id)
        if node is None:
            node = SpanNode(
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=event.get("parent_span_id"),
                core_id=str(event.get("core_id", "")),
                name=str(event.get("execution_step") or kind),
            )
            self._attach(node)
        ts = parse_ts(event.get("timestamp"))
        if kind in START_EVENTS:
            node.started_at = node.started_at or ts
        else:
            node.ended_at = ts
            node.status = "failed" if kind in FAIL_EVENTS else "ok"
        return node

    def _attach(self, node: SpanNode) -> None:
        self.spans[node.span_id] = node
        parent = node.parent_span_id
        if parent is not None:
            if parent in self.spans:
                self.spans[parent].children.append(node.span_id)
            else:
                self._waiting.setdefault(parent, []).append(node.span_id)
        node.children.extend(self._waiting.pop(node.span_id, []))

    # ---- writing (single durable writer: telemetry.transponder) -----------------

    def start_span(
        self,
        name: str,
        *,
        core_id: str,
        parent_span_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> SpanNode:
        event = emit(
            {
                "event": "STEP_STARTED",
                "core_id": core_id,
                "trace_id": self.trace_id,
                "span_id": secrets.token_hex(8),
                "parent_span_id": parent_span_id,
                "execution_step": name,
                "data": data or {},
            },
            self._log_path,
        )
        node = self.add_event(event)
        assert node is not None
        return node

    def end_span(
        self, span_id: str, *, ok: bool = True, data: dict[str, Any] | None = None
    ) -> SpanNode:
        node = self.spans[span_id]
        event = emit(
            {
                "event": "STEP_COMPLETED" if ok else "STEP_FAILED",
                "core_id": node.core_id,
                "trace_id": self.trace_id,
                "span_id": span_id,
                "parent_span_id": node.parent_span_id,
                "execution_step": node.name,
                "data": data or {},
            },
            self._log_path,
        )
        touched = self.add_event(event)
        assert touched is not None
        return touched

    # ---- queries --------------------------------------------------------------

    def roots(self) -> list[SpanNode]:
        """Spans whose parent is absent from the trace (external or no parent)."""
        return [n for n in self.spans.values() if n.parent_span_id not in self.spans]

    def depths(self) -> dict[str, int]:
        """Depth per span (root = 0); spans on a parent cycle get -1."""
        depths, _ = span_depths({sid: n.parent_span_id for sid, n in self.spans.items()})
        return depths

    def cycles(self) -> list[str]:
        _, cyclic = span_depths({sid: n.parent_span_id for sid, n in self.spans.items()})
        return cyclic

    def max_depth(self) -> int:
        return max(self.depths().values(), default=0)

    def latency_ms(self, span_id: str) -> float | None:
        node = self.spans.get(span_id)
        if node is None or node.started_at is None or node.ended_at is None:
            return None
        return (node.ended_at - node.started_at).total_seconds() * 1000.0


# --------------------------------------------------------------------------- #
# Runtime anomaly detector
# --------------------------------------------------------------------------- #

_MAD_CONSISTENCY: Final[float] = 0.6745  # MAD / 0.6745 estimates sigma for normal data
_MEAN_AD_FACTOR: Final[float] = 1.253314  # fallback scale when MAD == 0 (Iglewicz-Hoaglin)


@dataclass(frozen=True)
class Verdict:
    metric: str
    value: float
    warmed_up: bool
    median: float | None
    mad: float | None
    modified_z: float | None  # |0.6745 * (x - median) / MAD|
    huber_loss: float | None  # Huber loss of the standardized residual
    is_anomaly: bool


class RuntimeAnomalyDetector:
    """Sliding-window robust detector, one window per metric.

    For a new value ``x`` and the previous ``window`` values: ``median``, ``MAD`` =
    median ``|xi - median|``, ``modified_z = 0.6745 * |x - median| / MAD``; flagged when
    it exceeds ``z_threshold`` (3.5). If ``MAD == 0`` the scale falls back to
    ``1.253314 * mean absolute deviation``; if that is 0 too (a constant window) to
    ``scale_floor``, so a tiny change of a constant metric is not an alarm.
    The Huber loss uses the signed standardized residual ``r`` and ``delta`` (1.345):
    ``r^2 / 2`` for ``|r| <= delta`` else ``delta * (|r| - delta / 2)``; it is reported
    for drift tracking and is not itself a gate.

    Cold start: until ``min_samples`` values have been seen for a metric, values are
    only recorded and never flagged. The new value is added to the window after it
    is scored (a persistent level shift is absorbed once it fills the window).
    """

    def __init__(
        self,
        *,
        window: int = 64,
        min_samples: int = 10,
        z_threshold: float = 3.5,
        huber_delta: float = 1.345,
        scale_floor: float = 1.0,
    ) -> None:
        if min_samples < 3:
            raise ValueError("min_samples must be >= 3")
        if window < min_samples:
            raise ValueError("window must be >= min_samples")
        if z_threshold <= 0 or huber_delta <= 0 or scale_floor <= 0:
            raise ValueError("z_threshold, huber_delta and scale_floor must be > 0")
        self._window = window
        self._min = min_samples
        self._z = z_threshold
        self._delta = huber_delta
        self._floor = scale_floor
        self._windows: dict[str, deque[float]] = {}

    def window_size(self, metric: str) -> int:
        return len(self._windows.get(metric, ()))

    def observe(self, metric: str, value: float) -> Verdict:
        if not math.isfinite(value):
            raise ValueError(f"{metric}: value must be finite, got {value!r}")
        buf = self._windows.setdefault(metric, deque(maxlen=self._window))
        if len(buf) < self._min:
            buf.append(value)
            return Verdict(metric, value, False, None, None, None, None, False)

        samples = list(buf)
        median = statistics.median(samples)
        deviations = [abs(x - median) for x in samples]
        mad = statistics.median(deviations)
        if mad > 0:
            sigma = mad / _MAD_CONSISTENCY
        else:
            mean_ad = sum(deviations) / len(deviations)
            sigma = _MEAN_AD_FACTOR * mean_ad if mean_ad > 0 else self._floor
        residual = (value - median) / sigma
        modified_z = abs(residual)
        huber = (
            0.5 * residual * residual
            if modified_z <= self._delta
            else self._delta * (modified_z - 0.5 * self._delta)
        )
        buf.append(value)
        return Verdict(metric, value, True, median, mad, modified_z, huber, modified_z > self._z)


# --------------------------------------------------------------------------- #
# Glue
# --------------------------------------------------------------------------- #


def payload_size_bytes(event: Mapping[str, Any]) -> int:
    """UTF-8 size of the event's payload objects (data, context_diff, ...)."""
    total = 0
    for name in OBJECT_FIELDS:
        value = event.get(name)
        if value:
            total += len(
                json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
            )
    return total


class Tier1Observer:
    """Feed events in; get per-event verdicts for latency and payload size.

    Keeps at most ``max_traces`` trace trees (least recently used are dropped), so
    memory does not grow with the length of the stream.
    """

    def __init__(self, detector: RuntimeAnomalyDetector | None = None, max_traces: int = 1024) -> None:
        if max_traces < 1:
            raise ValueError("max_traces must be >= 1")
        self.detector = detector or RuntimeAnomalyDetector()
        self._max_traces = max_traces
        self.traces: OrderedDict[str, TraceDAG] = OrderedDict()

    def ingest(self, event: Mapping[str, Any]) -> list[Verdict]:
        verdicts: list[Verdict] = []
        trace_id = event.get("trace_id")
        if trace_id:
            dag = self.traces.get(trace_id)
            if dag is None:
                dag = self.traces[trace_id] = TraceDAG(trace_id)
                while len(self.traces) > self._max_traces:
                    self.traces.popitem(last=False)
            self.traces.move_to_end(trace_id)
            node = dag.add_event(event)
            if node is not None and event.get("event") in OK_EVENTS | FAIL_EVENTS:
                latency = dag.latency_ms(node.span_id)
                if latency is not None:
                    verdicts.append(self.detector.observe("latency_ms", latency))
        size = payload_size_bytes(event)
        if size > 0:
            verdicts.append(self.detector.observe("payload_bytes", float(size)))
        return verdicts
