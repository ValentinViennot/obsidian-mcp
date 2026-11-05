"""Retrieval metrics, hand-written and standard-library only (#356, design D8, E1).

All functions take `relevant: set[str]` and `run: list[str]` (ranked, best
first). Gains are binary, and `dedup` keeps the first occurrence of a path, so
a duplicate never earns gain twice:

- `recall_at(k)` = |relevant ∩ dedup(run)[:k]| / |relevant|
- `reciprocal_rank` = 1 / rank of the first relevant item in `dedup(run)`, 0 if none
- `ndcg_at(k)` = DCG / IDCG, DCG = Σ_{i ≤ k} rel_i / log2(i + 1) over
  `dedup(run)[:k]`, IDCG the same for min(|relevant|, k) relevant items
- `bootstrap_ci(deltas)`: a seeded percentile bootstrap of the mean

A query with an empty relevant set never reaches these (the drift drop removes
it); they return 0.0 for it rather than divide by zero.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Sequence


def dedup(run: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in run:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def recall_at(relevant: set[str], run: Sequence[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(relevant.intersection(dedup(run)[:k])) / len(relevant)


def reciprocal_rank(relevant: set[str], run: Sequence[str]) -> float:
    for i, p in enumerate(dedup(run), start=1):
        if p in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(relevant: set[str], run: Sequence[str], k: int) -> float:
    if not relevant:
        return 0.0
    dcg = sum(1.0 / math.log2(i + 1)
              for i, p in enumerate(dedup(run)[:k], start=1) if p in relevant)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / idcg


def novel_at(pool: set[str], run: Sequence[str], k: int) -> float:
    """Share of `dedup(run)[:k]` outside the judged pool; 0.0 for an empty run."""
    top = dedup(run)[:k]
    if not top:
        return 0.0
    return sum(1 for p in top if p not in pool) / len(top)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method)."""
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_ci(deltas: Sequence[float], *, resamples: int = 1000, seed: int = 0,
                 level: float = 0.95) -> tuple[float, float] | None:
    """Percentile bootstrap interval on the mean of `deltas`, or None when empty.

    `random.Random(seed)`, `resamples` resamples of len(deltas) drawn with
    replacement (`randrange`), and the (1 - level)/2 and (1 + level)/2
    percentiles of the resampled means. The order of `deltas` is part of the
    input: callers pass them in qid order so a report is reproducible.
    """
    n = len(deltas)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(resamples))
    alpha = (1.0 - level) / 2.0
    return (_percentile(means, alpha), _percentile(means, 1.0 - alpha))
