# ADR-0015: Two-tier observability: online robust telemetry, offline evaluation

**Status:** Accepted — revision 2; Tier 1 implemented as reference code in v3. Replaces the first text, which swapped the
statistical filter in `observer.py` for Ragas metrics.

## Context

Revision 1 scored every run with Tool-Call Accuracy and Agent-Goal Accuracy inside
`observer.py`. That does not work at runtime: those metrics need a `ground_truth`
or an LLM-as-a-Judge call per step, which costs latency and money on the critical
path. Optimizing the agent against a judge's score also invites **reward hacking**
(Goodhart's law).

## Decision

Split `observer.py` into two tiers.

### Tier 1: online runtime telemetry (no LLM call)

- Build the OpenTelemetry-style **span DAG** (one span per Perceive / Think / Act /
  Check, plus tool calls) from `event_stream.jsonl`. The existing
  [`telemetry/observer.py`](../../telemetry/observer.py) already rebuilds trace
  trees, depth and cycles; its 0/1/2 exit code stays the Kill-Switch hook.
- Detect drift with robust statistics over a fixed window of recent events, on
  step latency, canonical argument size, retry rate per tool and tokens per step:
  - **Modified Z-score** `M = 0.6745 · (x − median) / MAD`; flag `|M| > 3.5`
    (the usual Iglewicz–Hoaglin cut-off). If `MAD = 0`, fall back to
    `1.253314 · MeanAD`.
  - **Huber M-estimator** of location as the robust running baseline
    (`δ = 1.345 · scale`), so a few outliers do not drag the baseline.
- Cost per event is bounded by the window size and independent of log length, and
  there is no model call. It is **not** strictly O(1): an exact rolling median
  costs O(log W) with order-statistics structures. Strict O(1) would need
  approximate streaming quantiles, which this ADR does not claim.

### Tier 2: asynchronous offline audit

- A sampled export of `event_stream.jsonl` runs outside the critical path:
  deterministic invariants first (every Act has a Check; every commit carries a
  verified capability token; no packet without an attestation), then Ragas /
  LLM-as-a-Judge scoring.
- Tier 2 results are **audit-only**: never fed to the agent, never used as a
  reward or as an online gate.

### Reward-hacking protection

The agent has no read path to validator weights or prompts, to Tier 1 or Tier 2
metrics, or to detailed rejection reasons. See the section "Reward-hacking
protection" in [the L-E-J-D-A-S framework](../security/l-e-j-d-a-s-framework.md).

## Alternatives considered

| Option | Why not |
|---|---|
| Revision 1: Ragas metrics online | Needs ground truth or a judge call per step; Goodhart risk |
| Mean/standard-deviation Z-score (original text) | Not robust: the outliers being sought inflate the mean and σ |
| **Tier 1 robust statistics + Tier 2 offline audit (chosen)** | Fast online signal, expensive evaluation off the hot path |

## Consequences

- **Status:** Tier 1 is now reference code (amendment below); the Tier 2
  pipeline are not in the repository; only the span/cycle part exists in
  `telemetry/observer.py`.
- The thresholds (3.5, 1.345, window size, sampling rate) are textbook starting
  values, **UNVERIFIED** for this workload.
- The judge model used in Tier 2 should differ from the agent's model.
- This replaces the open questions "Huber/Z-score thresholds" and "Trace DAG
  schema" with the concrete choices above; tuning remains open.

## Amendment (v3): Tier 1 as code

[`src/observability/observer.py`](../../src/observability/observer.py) (stdlib only):

- `SpanNode` / `TraceDAG`: the span tree of one trace, built from events and optionally
  *written* through `telemetry.transponder` (schema validation, exclusive lock,
  `flush` + `os.fsync`). There is one durable writer, not two.
- `RuntimeAnomalyDetector`: sliding window (64) per metric, `min_samples` = 10 cold
  start, Modified Z-score `0.6745·|x − median|/MAD` flagged above 3.5, Huber loss with
  `delta` = 1.345 on the standardized residual, for step latency and payload size.
- `Tier1Observer`: feeds events to both; keeps at most 1024 traces (LRU).

Differences from the text above, stated plainly:

- **Cost.** Per event the work is bounded by the window `W` and independent of stream
  length: O(W log W) with an exact median (O(1) in stream length, not in `W`).
- **Huber** is computed against the window median and *reported*; the anomaly decision
  is the Modified Z-score. The Huber M-estimator of location named earlier is not
  implemented.
- A constant window (`MAD` = 0 and mean deviation 0) divides by a new parameter
  `scale_floor` (1.0 metric unit), which this ADR did not define; it is a starting
  value.
- The scored value is added to the window afterwards, so a persistent level shift is
  absorbed once it fills the window.

Thresholds are textbook starting values, **unverified** on real traces. Tier 2 is still
specified only. Evidence: `tests/test_observability.py` (formula checks against hand
computations, cold start, boundedness, fallback, robustness against a masked outlier,
`fsync` call count on writes, agreement with the existing `telemetry/observer.py`).
