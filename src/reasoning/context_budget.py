"""Dynamic waterfilling context allocator for the Perceive phase (ADR-0014).

The context window is split into

* an INELASTIC reserve, counted in absolute tokens and never truncated:
  system prompt, tool JSON schemas, last user intent, output buffer
  (``max_tokens``) and protected history anchors; and
* an ELASTIC pool (``context_window - inelastic_used``) shared between
  conversation history and RAG chunks by max-min fair ("waterfilling")
  allocation, then trimmed with a deterministic eviction policy.

If the inelastic reserve exceeds ``hard_inelastic_cap`` (or leaves no room), the
allocator raises ``ContextConfigurationError`` BEFORE any LLM call: a truncated
tool schema would crash the parser, so it is never cut.

Token counts are inputs (the caller owns the tokenizer); this module does no I/O.
The same input always yields the same plan, regardless of input ordering.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContextConfigurationError(ValueError):
    """The inelastic reserve does not fit; fix the configuration, do not truncate."""


_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class HistoryPair(BaseModel):
    """One request/response pair; the unit of eviction (never split)."""

    model_config = _STRICT
    pair_id: str = Field(min_length=1)
    seq: int = Field(ge=0)  # larger = newer
    tokens: int = Field(ge=0)
    anchor: bool = False  # protected: never evicted, charged to the inelastic reserve


class RagChunk(BaseModel):
    model_config = _STRICT
    chunk_id: str = Field(min_length=1)
    tokens: int = Field(ge=0)
    relevance_score: float = Field(allow_inf_nan=False)


class InelasticBlocks(BaseModel):
    model_config = _STRICT
    system_prompt_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    last_user_intent_tokens: int = Field(ge=0)
    output_buffer_tokens: int = Field(gt=0)  # = max_tokens of the call


class AllocatorConfig(BaseModel):
    model_config = _STRICT
    context_window: int = Field(gt=0)
    hard_inelastic_cap: int = Field(gt=0)


class Eviction(BaseModel):
    model_config = _STRICT
    kind: Literal["history", "rag"]
    item_id: str
    tokens: int


class ContextPlan(BaseModel):
    model_config = _STRICT
    inelastic_tokens: int
    elastic_pool: int
    budget_history: int
    budget_rag: int
    history: tuple[HistoryPair, ...]  # chronological, anchors included
    rag: tuple[RagChunk, ...]  # by relevance, best first
    evictions: tuple[Eviction, ...]  # caller logs these to event_stream.jsonl
    total_tokens: int


def waterfill(demands: Mapping[str, int], pool: int) -> dict[str, int]:
    """Max-min fair integer split of ``pool`` across ``demands``.

    No class receives more than it asked for; if everything fits, everyone gets
    their full demand. Remainders go to the first names in sorted order.
    """
    if pool < 0:
        raise ValueError("pool must be >= 0")
    if any(d < 0 for d in demands.values()):
        raise ValueError("demands must be >= 0")
    order = sorted(demands, key=lambda name: (demands[name], name))
    result: dict[str, int] = {}
    remaining = pool
    for idx, name in enumerate(order):
        left = len(order) - idx
        share = remaining // left
        if demands[name] <= share:
            result[name] = demands[name]
            remaining -= demands[name]
            continue
        rest = sorted(order[idx:])
        base, extra = divmod(remaining, len(rest))
        for j, other in enumerate(rest):
            result[other] = base + (1 if j < extra else 0)
        break
    return result


class WaterfillingContextAllocator:
    def __init__(self, config: AllocatorConfig) -> None:
        self._cfg = config

    def allocate(
        self,
        blocks: InelasticBlocks,
        history: list[HistoryPair],
        rag: list[RagChunk],
    ) -> ContextPlan:
        if len({p.pair_id for p in history}) != len(history):
            raise ValueError("duplicate pair_id in history")
        if len({c.chunk_id for c in rag}) != len(rag):
            raise ValueError("duplicate chunk_id in rag")

        anchors = sorted((p for p in history if p.anchor), key=lambda p: (p.seq, p.pair_id))
        movable = [p for p in history if not p.anchor]
        inelastic = (
            blocks.system_prompt_tokens
            + blocks.tool_schema_tokens
            + blocks.last_user_intent_tokens
            + blocks.output_buffer_tokens
            + sum(p.tokens for p in anchors)
        )
        if inelastic > self._cfg.hard_inelastic_cap:
            raise ContextConfigurationError(
                f"inelastic reserve {inelastic} exceeds HARD_INELASTIC_CAP "
                f"{self._cfg.hard_inelastic_cap}"
            )
        if inelastic >= self._cfg.context_window:
            raise ContextConfigurationError(
                f"inelastic reserve {inelastic} leaves no room in a "
                f"{self._cfg.context_window}-token window"
            )
        pool = self._cfg.context_window - inelastic

        budgets = waterfill(
            {"history": sum(p.tokens for p in movable), "rag": sum(c.tokens for c in rag)},
            pool,
        )

        # History: newest-first contiguous suffix (oldest pairs are evicted first).
        newest_first = sorted(movable, key=lambda p: (-p.seq, p.pair_id))
        kept_history: list[HistoryPair] = []
        used_h = 0
        for pair in newest_first:
            if used_h + pair.tokens > budgets["history"]:
                break
            kept_history.append(pair)
            used_h += pair.tokens

        # RAG: highest relevance first; lowest-relevance chunks are evicted first.
        by_relevance = sorted(rag, key=lambda c: (-c.relevance_score, c.chunk_id))
        kept_rag: list[RagChunk] = []
        used_r = 0
        for chunk in by_relevance:
            if used_r + chunk.tokens <= budgets["rag"]:
                kept_rag.append(chunk)
                used_r += chunk.tokens

        # Slack pass: hand unused tokens back, RAG first, then the next-older pairs.
        slack = pool - used_h - used_r
        kept_ids = {c.chunk_id for c in kept_rag}
        for chunk in by_relevance:
            if chunk.chunk_id not in kept_ids and chunk.tokens <= slack:
                kept_rag.append(chunk)
                kept_ids.add(chunk.chunk_id)
                slack -= chunk.tokens
                used_r += chunk.tokens
        for pair in newest_first[len(kept_history):]:
            if pair.tokens > slack:
                break
            kept_history.append(pair)
            slack -= pair.tokens
            used_h += pair.tokens

        kept_rag.sort(key=lambda c: (-c.relevance_score, c.chunk_id))
        final_history = tuple(sorted([*anchors, *kept_history], key=lambda p: (p.seq, p.pair_id)))
        kept_pair_ids = {p.pair_id for p in kept_history}
        evictions = tuple(
            [
                Eviction(kind="history", item_id=p.pair_id, tokens=p.tokens)
                for p in sorted(movable, key=lambda p: (p.seq, p.pair_id))
                if p.pair_id not in kept_pair_ids
            ]
            + [
                Eviction(kind="rag", item_id=c.chunk_id, tokens=c.tokens)
                for c in reversed(by_relevance)
                if c.chunk_id not in kept_ids
            ]
        )
        total = inelastic + used_h + used_r
        if total > self._cfg.context_window:  # internal invariant, never expected
            raise AssertionError("allocator produced a plan larger than the window")
        return ContextPlan(
            inelastic_tokens=inelastic,
            elastic_pool=pool,
            budget_history=budgets["history"],
            budget_rag=budgets["rag"],
            history=final_history,
            rag=tuple(kept_rag),
            evictions=evictions,
            total_tokens=total,
        )
