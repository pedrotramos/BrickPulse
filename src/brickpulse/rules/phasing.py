"""Phased downsizing: don't jump a capacity setting straight to what the data supports.

A rule that lowers capacity users can feel (a warehouse's size or cluster floor, a
cluster's workers, a pool's warm instances) may find that the data supports a big
cut: min_clusters 30 -> 1, 64 workers -> 4. The saving at that target is real, but
taking it in one jump removes most of the capacity at once, on evidence from one
window that may have missed a periodic peak. So capacity rules follow one policy
(docs/ADDING_A_RULE.md, "Rules that reduce capacity"):

1. Say what the data supports (``data_target``).
2. Recommend one bounded step now: each step cuts by at most ``max_step_ratio``
   (default 3x: 30 -> 10 -> 4 -> 2 -> 1), then observe for ``observe_days``
   (default 7) and repeat.
3. Lead with the full saving: ``monthly_savings_usd`` = ``monthly_savings_high_usd``
   = the saving at the data target; ``monthly_savings_low_usd`` = the first step's.
4. Emit ``evidence.phasing`` (:func:`phasing_evidence`) so the UI renders the step
   plan with no per-rule frontend code.
5. Say in ``how`` what to watch between steps (:func:`phase_sentence`).

Timeouts (auto-stop, auto-termination) are deliberately NOT phased: the admin
judges the cold-start trade-off, and the serverless auto-stop rule already bounds
its proposal by restarts per day.

The engine-wide defaults live in the top-level ``phasing:`` block of
``config/thresholds.yaml`` and reach a rule through ``RuleContext.phasing`` (the
same route ``min_observation_days`` takes). A rule may override the ratio in its
own block with ``phasing_max_step_ratio`` (``rightsize_underutilized`` halves).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

#: At most this many times smaller per step (30 -> 10 -> 4 -> 2 -> 1).
DEFAULT_MAX_STEP_RATIO = 3.0
#: Days to watch the KPIs between two steps.
DEFAULT_OBSERVE_DAYS = 7


def max_step_ratio(ctx) -> float:
    """The step ratio for this rule: its own ``phasing_max_step_ratio``, else the
    engine-wide ``phasing.max_step_ratio``, else 3. Never below 1 (a ratio of 1
    means "no phasing": one jump straight to the target)."""
    own = (getattr(ctx, "thresholds", None) or {}).get("phasing_max_step_ratio")
    engine = (getattr(ctx, "phasing", None) or {}).get("max_step_ratio")
    value = own if own is not None else engine
    try:
        ratio = float(value) if value is not None else DEFAULT_MAX_STEP_RATIO
    except (TypeError, ValueError):
        ratio = DEFAULT_MAX_STEP_RATIO
    return max(1.0, ratio)


def observe_days(ctx) -> int:
    """Days to observe between steps: the rule's ``phasing_observe_days``, else the
    engine-wide ``phasing.observe_days``, else 7."""
    own = (getattr(ctx, "thresholds", None) or {}).get("phasing_observe_days")
    engine = (getattr(ctx, "phasing", None) or {}).get("observe_days")
    value = own if own is not None else engine
    try:
        days = int(value) if value is not None else DEFAULT_OBSERVE_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_OBSERVE_DAYS
    return max(1, days)


def count_plan(
    current: int, target: int, *, max_step_ratio: float = DEFAULT_MAX_STEP_RATIO
) -> list[int]:
    """The steps AFTER ``current`` that reach ``target``, each at most
    ``max_step_ratio`` times smaller than the one before.

    ``next = max(target, ceil(cur / max_step_ratio))``; when that makes no progress
    (1 -> 1 with a target of 0, or a ratio of 1) the next step is the target itself.
    Empty when ``current`` is already at or below ``target``.

    >>> count_plan(30, 1)
    [10, 4, 2, 1]
    >>> count_plan(3, 0)
    [1, 0]
    """
    cur, target = int(current), int(target)
    ratio = max(1.0, float(max_step_ratio))
    plan: list[int] = []
    while cur > target:
        nxt = max(target, math.ceil(cur / ratio))
        if nxt >= cur:
            nxt = target
        plan.append(nxt)
        cur = nxt
    return plan


def _norm_size(size: str) -> str:
    return str(size or "").upper().replace("XSMALL", "X_SMALL").replace("-", "_")


def ladder_plan(
    current: str, target: str, ladder: Sequence[str], *, steps_per_phase: int = 1
) -> list[str]:
    """The ladder rungs AFTER ``current`` down to ``target`` (smallest-first ``ladder``,
    e.g. ``savings.WAREHOUSE_SIZE_LADDER``), ``steps_per_phase`` rungs at a time; the
    last element is always ``target``. Empty when either size is unknown or ``target``
    is not below ``current``.

    >>> ladder_plan("X_LARGE", "SMALL", ["X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE"])
    ['LARGE', 'MEDIUM', 'SMALL']
    """
    rungs = [_norm_size(s) for s in ladder]
    cur, tgt = _norm_size(current), _norm_size(target)
    if cur not in rungs or tgt not in rungs:
        return []
    i, j = rungs.index(cur), rungs.index(tgt)
    if j >= i:
        return []
    step = max(1, int(steps_per_phase))
    plan: list[str] = []
    k = i
    while k > j:
        k = max(j, k - step)
        plan.append(ladder[k])
    return plan


def phasing_evidence(
    setting: str,
    current: int | str,
    plan: Sequence[int | str],
    *,
    observe_days: int,
    watch: str,
    step_savings: float,
    target_savings: float,
) -> dict:
    """The ``evidence.phasing`` block (the contract in docs/DATA_MODEL.md).

    ``plan`` is the steps AFTER ``current``: ``recommended_step`` is ``plan[0]`` and
    ``data_target`` is ``plan[-1]``. ``step_savings`` must be the finding's
    ``monthly_savings_low_usd`` and ``target_savings`` its ``monthly_savings_usd`` (=
    ``monthly_savings_high_usd``); a single-step plan has both equal."""
    if not plan:
        raise ValueError("phasing_evidence needs at least one step")
    return {
        "setting": setting,
        "current": current,
        "recommended_step": plan[0],
        "data_target": plan[-1],
        "plan": list(plan),
        "observe_days": int(observe_days),
        "watch": watch,
        "step_savings_usd": round(float(step_savings), 2),
        "target_savings_usd": round(float(target_savings), 2),
    }


def _join_steps(values: Sequence[str]) -> str:
    return ", then ".join(values)


def phase_target_clause(
    *,
    target: int | str,
    target_savings: float | None,
    fmt: Callable[[int | str], str] = str,
) -> str:
    """The ``why`` sentence for a multi-step plan: what the data supports, and why
    it is taken in steps. Starts with a space so it appends to a sentence."""
    worth = f" (~${target_savings:,.0f}/mo)" if target_savings else ""
    return (
        f" The data supports going straight to {fmt(target)}{worth}, but every step down "
        f"removes capacity your users may feel at the start of a burst, so do it in steps."
    )


def phase_sentence(
    *,
    plan: Sequence[int | str],
    observe_days: int,
    watch: str,
    fmt: Callable[[int | str], str] = str,
    verb: str = "lower it",
) -> str:
    """The ``how`` tail shared by every capacity rule: what to watch, for how long,
    and the remaining steps. Starts with a space so it appends to a sentence."""
    if len(plan) <= 1:
        return (
            f" Watch {watch} for about {observe_days} days afterwards; step back up if "
            f"they degrade."
        )
    rest = _join_steps([fmt(v) for v in plan[1:]])
    return (
        f" Watch {watch} for about {observe_days} days. If they hold, {verb} to {rest}, "
        f"watching each step the same way. Step back up if they degrade."
    )
