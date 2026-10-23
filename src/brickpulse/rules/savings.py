"""Data-driven, deliberately-conservative savings math.

This replaces the old flat fractions (``0.15 * cost``) with estimates computed
from the resource's *actual* usage series. The guiding rule, from the design
discussion: **the estimate must never exceed the true avoidable spend** — we
would rather under-promise than lose the customer's trust. Accuracy is the
secondary goal.

Every function here is pure (no I/O) and is exercised by the ``benchmark/``
harness against an independent state-machine simulator, so "conservative" and
"accurate enough" are asserted, not asserted-by-faith. Both the live rule
``score()`` and the demo source call these, so the two modes agree by
construction.

Key conservatism levers, all deriving from **hourly** billing granularity:
* We only ever count *whole idle hours*. An hour that served even one short
  query is billed as fully active and contributes nothing to savings — so a
  real sub-hour idle tail is silently dropped (rounding down).
* For auto-stop we subtract the warm tail the *proposed* setting still leaves
  after each active session, so we never claim to reclaim time the fix wouldn't.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Databricks SQL warehouse auto-stop floor (minutes). The shortest setting a
# customer can pick, so the best case we may promise.
MIN_AUTOSTOP_MINUTES = 10

# Serverless SQL warehouses bill per-second and restart far faster than classic,
# so a shorter auto-stop reclaims real idle tail. But "shortest possible" is the
# wrong target: the Databricks UI itself enforces a 5-minute floor for serverless
# auto-stop (1 min is reachable only via the API), and a setting that stops the
# warehouse between every pair of queries makes the thing feel broken to its
# users — each restart is a visible wait on the next query. So we bound the
# recommendation to a usability-safe window and pick a value inside it from the
# warehouse's OWN measured query pattern (see choose_serverless_autostop).
MIN_AUTOSTOP_MINUTES_SERVERLESS = 5   # UI floor; never recommend below this
MAX_AUTOSTOP_MINUTES_SERVERLESS = 10  # serverless default; never recommend above

#: Billed/perceived cost of one extra warehouse start, in minutes. Serverless
#: starts in seconds rather than the minutes a classic warehouse takes, but it is
#: NOT free: the next query waits, and we charge that wait against the reclaim so
#: a churn-heavy setting cannot look like a win.
STARTUP_PENALTY_MIN_SERVERLESS = 1.0

#: How many *extra* stop/start cycles per day a shorter auto-stop may add before
#: we stop treating it as a free config tweak. Chosen so an interactive warehouse
#: restarts at most a couple of times in a working day beyond what it already does.
MAX_EXTRA_STOPS_PER_DAY = 3.0

#: The upper bound we will still recommend, with a HIGH-CHURN warning attached.
#: Between this and ``MAX_EXTRA_STOPS_PER_DAY`` the saving is real but the restart
#: cost is material, so the finding must say so rather than be silently dropped —
#: a warehouse polled hourly can be 80%+ idle tail, which is exactly the case worth
#: telling an admin about even though it isn't a no-brainer.
#: Above this, churn is severe enough that we suppress instead of recommending.
MAX_EXTRA_STOPS_PER_DAY_CHURNY = 30.0


def warehouse_autostop_savings(
    *,
    warm_hours: float,
    idle_warm_hours: float,
    active_sessions: int,
    monthly_cost: float,
    proposed_autostop_minutes: int = MIN_AUTOSTOP_MINUTES,
) -> tuple[float, float]:
    """Savings from shortening a warehouse's auto-stop, computed from measured
    idle time rather than a guessed fraction.

    ``warm_hours``      billed hours the warehouse was up (active + idle).
    ``idle_warm_hours`` whole hours it was up but served no query (the waste).
    ``active_sessions`` number of distinct query sessions — each one still
                        leaves a ``proposed_autostop_minutes`` warm tail we
                        cannot reclaim, so we subtract them.

    Returns ``(monthly_savings_usd, reclaimable_hours)``. Conservative because
    idle_warm_hours already excludes any hour with activity, and we further
    remove the unavoidable per-session tail.
    """
    if warm_hours <= 0 or monthly_cost <= 0 or idle_warm_hours <= 0:
        return 0.0, 0.0
    tail_hours = active_sessions * proposed_autostop_minutes / 60.0
    reclaimable = max(0.0, idle_warm_hours - tail_hours)
    fraction = min(1.0, reclaimable / warm_hours)
    return round(monthly_cost * fraction, 2), round(reclaimable, 1)


# --- classic/pro -> serverless SQL warehouse migration ---------------------
# Databricks SQL list DBU rates (AWS US East, list price; $/DBU). Override per
# your contract / cloud / region via thresholds.yaml. The critical asymmetry:
# the SERVERLESS rate is ALL-IN (it includes the underlying cloud VM), whereas
# the classic and pro rates are DBU-ONLY — the VM those warehouses run on is
# billed separately by the cloud provider and does NOT appear in
# system.billing.usage. Comparing the measured (DBU-only) classic/pro bill to
# serverless directly therefore understates classic/pro and is apples-to-oranges;
# we correct for it with an ESTIMATED VM cost below.
CLASSIC_SQL_DBU_RATE = 0.22
PRO_SQL_DBU_RATE = 0.55
SERVERLESS_SQL_DBU_RATE = 0.70

#: Estimated cloud VM/infra cost for a classic/pro SQL warehouse, as $/DBU
#: consumed. This is the piece missing from system.billing.usage (cloud-billed),
#: so it is an ESTIMATE, surfaced as such in the finding — never presented as a
#: measured figure. Default ~= the classic DBU rate: for classic SQL the
#: underlying VM roughly doubles the DBU-only bill. Tier-independent (classic and
#: pro of the same size run the same instances), so it is a flat $/DBU add-on.
EST_VM_COST_PER_DBU = 0.22


@dataclass
class ServerlessMigration:
    """Estimated economics of moving one classic/pro SQL warehouse to serverless.

    All figures are monthly USD except the two fractions. ``net_monthly_savings``
    can be NEGATIVE — a mostly-active warehouse pays the higher serverless rate on
    work that still has to run, and that premium can exceed the idle saving. The
    caller suppresses non-positive findings.
    """

    net_monthly_savings: float  # idle_saved - active_delta; may be negative
    all_in_monthly_est: float   # estimated CURRENT all-in cost (measured DBU + est VM)
    idle_saved_est: float       # all-in cost removed by serverless scale-to-zero
    active_delta_est: float     # extra (+) or saved (-) on active hours at serverless rate
    idle_fraction: float        # idle_warm_hours / warm_hours (0..1)
    est_vm_cost_per_dbu: float  # the VM estimate used, so the UI can label it


def serverless_migration_savings(
    *,
    warm_hours: float,
    idle_warm_hours: float,
    monthly_cost: float,
    tier_dbu_rate: float,
    serverless_dbu_rate: float = SERVERLESS_SQL_DBU_RATE,
    est_vm_cost_per_dbu: float = EST_VM_COST_PER_DBU,
) -> ServerlessMigration:
    """Estimate the net monthly saving of migrating a classic/pro SQL warehouse
    to serverless, modelling BOTH levers honestly:

    * **Idle removal (the win):** serverless scales to zero between bursts, so it
      eliminates the *all-in* idle warm-pool cost — the DBU bill we measured PLUS
      the estimated cloud VM that sat warm alongside it.
    * **Active-hours rate premium (the cost):** the active work still runs, now at
      the higher serverless all-in rate. That premium is charged against the
      saving, so a warehouse whose active spend dominates can net out to a LOSS.

    ``monthly_cost`` is the measured DBU-only bill (``DBUs * tier_dbu_rate``) for
    the window, already normalized to a month. ``tier_dbu_rate`` is the classic or
    pro rate that produced it, so ``monthly_cost / tier_dbu_rate`` recovers the DBU
    count and lets us price the (missing) VM and the serverless alternative.

    Unlike the old model — which credited 85% of the idle DBU cost and dropped the
    active premium entirely, so it could never recommend against a migration — this
    is bounded by real economics on both sides. The VM figure is an ESTIMATE (see
    ``est_vm_cost_per_dbu``); the returned struct carries it so the finding can say
    so. Conservative choices: whole idle hours only (hourly granularity rounds the
    idle share down), and we ignore serverless' faster hardware (fewer DBU-seconds
    for the same work), which would only improve the active-hours comparison.
    """
    if warm_hours <= 0 or monthly_cost <= 0 or tier_dbu_rate <= 0:
        return ServerlessMigration(0.0, 0.0, 0.0, 0.0, 0.0, est_vm_cost_per_dbu)
    idle_fraction = min(1.0, max(0.0, idle_warm_hours / warm_hours))
    active_fraction = 1.0 - idle_fraction
    # Gross the measured DBU-only bill up to an estimated all-in cost by adding the
    # VM cost billed separately. monthly_cost = DBUs * tier_dbu_rate, so the VM add
    # is DBUs * est_vm = monthly_cost * (est_vm / tier_dbu_rate).
    vm_gross_up = est_vm_cost_per_dbu / tier_dbu_rate
    all_in_monthly = monthly_cost * (1.0 + vm_gross_up)
    # Scale-to-zero removes the all-in idle cost entirely.
    idle_saved = all_in_monthly * idle_fraction
    # Active DBUs re-priced from the classic/pro all-in rate to the serverless
    # all-in rate. Positive => serverless costs MORE on active hours (classic);
    # negative => serverless is cheaper on active hours too (common for pro).
    active_delta = monthly_cost * active_fraction * (
        serverless_dbu_rate / tier_dbu_rate - 1.0 - vm_gross_up
    )
    net = idle_saved - active_delta
    return ServerlessMigration(
        net_monthly_savings=round(net, 2),
        all_in_monthly_est=round(all_in_monthly, 2),
        idle_saved_est=round(idle_saved, 2),
        active_delta_est=round(active_delta, 2),
        idle_fraction=round(idle_fraction, 4),
        est_vm_cost_per_dbu=est_vm_cost_per_dbu,
    )


def classify_photon_workload(dbr_version) -> str:
    """Classify a cluster's Photon/DBR-upgrade opportunity from its runtime string.

    Photon accelerates SQL/Spark operators strongly but does almost nothing for
    Python/ML code, so a flat uplift over-credits ML clusters. The DBR
    version string is a reliable signal for two cases; the rest stays 'mixed':

    * ``ml``        — an ML runtime ('-ml' / '-cpu-ml' / '-gpu-ml'): Photon barely
                      applies, so the uplift is near the floor.
    * ``photon_on`` — Photon already in the runtime string: the Photon lever is
                      already pulled, so only the DBR upgrade itself remains.
    * ``mixed``     — anything else (a general all-purpose runtime we cannot split
                      into SQL-BI vs ETL from the version alone): conservative floor.
    """
    text = str(dbr_version or "").lower()
    if "-ml" in text or "ml-" in text or "-cpu-ml" in text or "-gpu-ml" in text:
        return "ml"
    if "photon" in text:
        return "photon_on"
    return "mixed"


def photon_savings(
    *,
    monthly_cost: float,
    workload_class: str,
    uplift_ml: float = 0.05,
    uplift_photon_on: float = 0.05,
    uplift_mixed: float = 0.15,
) -> tuple[float, float]:
    """Monthly saving from a DBR upgrade + Photon, priced by workload class.

    Returns ``(monthly_savings_usd, uplift_fraction_used)``. The fraction is chosen
    by :func:`classify_photon_workload`; unknown classes fall to the mixed floor.
    Still a heuristic (hence the caller keeps LOW confidence and a disclaimer), but a
    class-aware one that no longer over-credits ML/Python clusters."""
    if monthly_cost <= 0:
        return 0.0, 0.0
    fraction = {
        "ml": uplift_ml,
        "photon_on": uplift_photon_on,
        "mixed": uplift_mixed,
    }.get(workload_class, uplift_mixed)
    return round(monthly_cost * fraction, 2), round(fraction, 4)


def rightsize_savings(
    *,
    p95_cpu: float,
    monthly_cost: float,
    target_utilization: float = 0.6,
    headroom: float = 0.15,
    max_savings_fraction: float = 0.6,
    keep_fraction: float | None = None,
) -> float:
    """Savings from shrinking an under-utilized cluster so its p95 CPU sits near
    ``target_utilization`` with ``headroom`` to spare.

    Uses p95 (not mean) so we keep capacity for the observed peak, and caps the
    claim at ``max_savings_fraction`` — CPU is only a proxy for the binding
    resource (memory/IO may bind first), so this rule is intentionally cautious
    and flagged lower-confidence for warehouses (which expose no CPU at all).

    ``keep_fraction`` overrides the p95-derived share of capacity kept — used to
    price an intermediate step of a phased downsize (``step_workers / workers``)
    with the same cap.
    """
    if monthly_cost <= 0 or p95_cpu <= 0:
        return 0.0
    if keep_fraction is None:
        keep_fraction = min(1.0, (p95_cpu * (1.0 + headroom)) / target_utilization)
    keep_fraction = min(1.0, max(0.0, float(keep_fraction)))
    savings_fraction = min(max_savings_fraction, max(0.0, 1.0 - keep_fraction))
    return round(monthly_cost * savings_fraction, 2)


# --- gap-based auto-stop savings (fine resolution, higher accuracy) --------
# The hourly path above is bounded by hourly granularity. When per-query
# timestamps are available (system.query.history), we reconstruct the true idle
# gaps and reclaim far more accurately — recovery ~95% vs ~50% — while a small
# safety margin keeps us conservative against effects the gap model omits
# (notably cold-start billing: a shorter auto-stop means more restarts).

#: Assumed billed minutes per cold start. A shorter auto-stop stops the warehouse
#: more often, and each restart is billed; we net this out of the reclaim
#: explicitly (below). Set a touch above typical classic-warehouse startup so the
#: correction is conservative even when real startup is faster.
RESTART_PENALTY_MIN = 4

#: Small residual margin for effects the model still omits (per-second rounding,
#: rate variation across multi-cluster scale-out). Much smaller than the old flat
#: fraction because cold starts — the dominant effect — are now handled directly.
GAP_RESIDUAL_MARGIN = 0.03


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent ``[start, end)`` minute intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def reclaimable_minutes(
    busy_intervals: list[tuple[int, int]],
    horizon_min: int,
    current_autostop_min: int,
    proposed_autostop_min: int = MIN_AUTOSTOP_MINUTES,
    restart_penalty_min: int = RESTART_PENALTY_MIN,
) -> float:
    """Net minutes reclaimed by shortening auto-stop, from the *actual* gaps
    between queries.

    For each idle gap we reclaim ``min(gap, current) - min(gap, proposed)``. But
    a gap that exceeds the proposed (yet not the current) auto-stop causes an
    *extra* stop/start cycle under the new setting — a billed cold start we would
    not otherwise pay. We subtract ``restart_penalty_min`` for each so the
    estimate reflects the true net gain, not just the raw idle time."""
    merged = merge_intervals(busy_intervals)
    if not merged:
        return 0.0
    reclaim = 0.0
    extra_restarts = 0
    for i, (_s, e) in enumerate(merged):
        next_start = merged[i + 1][0] if i + 1 < len(merged) else horizon_min
        gap = max(0, next_start - e)
        reclaim += min(gap, current_autostop_min) - min(gap, proposed_autostop_min)
        if proposed_autostop_min < gap <= current_autostop_min:
            extra_restarts += 1
    return max(0.0, reclaim - extra_restarts * restart_penalty_min)


def warehouse_autostop_savings_from_gaps(
    *,
    reclaimable_min: float,
    warm_minutes_current: float,
    monthly_cost: float,
    residual_margin: float = GAP_RESIDUAL_MARGIN,
) -> tuple[float, float]:
    """Price net gap-based reclaimable minutes into monthly dollars.

    ``warm_minutes_current`` is billed warm time under today's setting, so
    ``monthly_cost / warm_minutes_current`` is the effective $/warm-minute. A
    small ``residual_margin`` keeps us under true savings for unmodeled effects.
    Returns ``(monthly_savings_usd, reclaimable_hours)``."""
    if warm_minutes_current <= 0 or monthly_cost <= 0 or reclaimable_min <= 0:
        return 0.0, 0.0
    fraction = min(1.0, reclaimable_min / warm_minutes_current)
    savings = monthly_cost * fraction * (1.0 - residual_margin)
    return round(savings, 2), round(reclaimable_min / 60.0, 1)


def serverless_autostop_savings_from_gaps(
    *,
    reclaimable_min: float,
    warm_minutes_current: float,
    monthly_cost: float,
    residual_margin: float = GAP_RESIDUAL_MARGIN,
) -> tuple[float, float]:
    """Savings from shortening a SERVERLESS warehouse's auto-stop.

    Serverless warehouses bill per-second and start in seconds, so — unlike the
    classic path — shortening auto-stop incurs **no cold-start restart penalty**:
    every reclaimed idle minute is a real saving. The gap arithmetic therefore
    reclaims down to a 1-minute floor (see the SQL / ``reclaimable_minutes`` with
    ``restart_penalty_min=0``) and we price it against the billed warm time.

    This deliberately mirrors ``warehouse_autostop_savings_from_gaps`` but is a
    separate function so the "no restart penalty" assumption is explicit and
    unit-tested, not implied. The classic path stays restart-aware.

    Returns ``(monthly_savings_usd, reclaimable_hours)``."""
    if warm_minutes_current <= 0 or monthly_cost <= 0 or reclaimable_min <= 0:
        return 0.0, 0.0
    fraction = min(1.0, reclaimable_min / warm_minutes_current)
    savings = monthly_cost * fraction * (1.0 - residual_margin)
    return round(savings, 2), round(reclaimable_min / 60.0, 1)


def choose_serverless_autostop(
    *,
    gap_histogram: dict[int, float] | None,
    current_autostop_min: float,
    lookback_days: int,
    min_minutes: int = MIN_AUTOSTOP_MINUTES_SERVERLESS,
    max_minutes: int = MAX_AUTOSTOP_MINUTES_SERVERLESS,
    startup_penalty_min: float = STARTUP_PENALTY_MIN_SERVERLESS,
    max_extra_stops_per_day: float = MAX_EXTRA_STOPS_PER_DAY,
    churny_stops_per_day: float = MAX_EXTRA_STOPS_PER_DAY_CHURNY,
) -> tuple[int | None, dict]:
    """Pick the auto-stop value for ONE serverless warehouse from its own
    measured inter-query gap distribution.

    The naive answer — "set it to the floor" — is wrong in practice. Auto-stop
    only reclaims the part of an idle gap that extends beyond the setting, so the
    *marginal* gain of going from 10 to 5 minutes is limited to gaps that fall in
    that band. Meanwhile every gap longer than the setting but shorter than
    today's becomes a NEW stop/start cycle: the user's next query waits for the
    warehouse to come back. Below some point you are trading a few dollars for a
    warehouse that feels broken.

    So we evaluate each candidate value in ``[min_minutes, max_minutes]`` against
    the warehouse's real pattern and pick the shortest one that is still
    comfortable:

    * ``reclaimed`` — minutes of idle tail the candidate removes vs today.
    * ``extra_stops`` — how many additional restarts per day it introduces.
    * a candidate is ``acceptable`` only within ``max_extra_stops_per_day``.

    Among the acceptable candidates we take the one with the best net reclaim
    (reclaim minus the startup penalty for the restarts it adds), preferring the
    LONGER setting on a tie so we never trade usability for nothing.

    **High-churn fallback.** If nothing is comfortable but some candidate is still
    within ``churny_stops_per_day``, we return it anyway and set
    ``analysis["high_churn"] = True``. Silently dropping these hides real money: a
    warehouse polled hourly can be mostly idle tail, worth thousands a month, and
    the admin — not this function — is the one who knows whether anything
    latency-sensitive depends on it. The caller is expected to surface the warning
    alongside the saving. Beyond ``churny_stops_per_day`` we do suppress: at that
    point the restarts stop being a caveat and become a different workload.

    ``gap_histogram`` maps an upper-bound bucket (minutes) to the count of idle
    gaps that fall at or below it — see the rule SQL, which emits gaps bucketed
    at 1/2/3/5/7/10/15/20/30/60/inf minutes.

    Returns ``(chosen_minutes | None, analysis)``. ``None`` means no candidate is
    both shorter than today's setting and usability-safe, so the rule suppresses
    the finding rather than nagging about a warehouse that is already tuned.
    """
    if not gap_histogram:
        return None, {}
    days = max(1.0, float(lookback_days))
    # Bucket upper bounds, ascending. Counts are per-bucket (not cumulative).
    buckets = sorted((int(k), float(v)) for k, v in gap_histogram.items() if v)
    if not buckets:
        return None, {}
    current = float(current_autostop_min) if current_autostop_min else float("inf")

    def _gap_minutes(bound: int) -> float:
        """Representative length for gaps in a bucket. We use the bucket's own
        upper bound, which is the conservative choice: it maximizes the idle time
        we assume the CURRENT setting already reclaims, so the candidate's
        marginal gain is never overstated."""
        return float(bound)

    candidates: list[dict] = []
    for minutes in range(int(min_minutes), int(max_minutes) + 1):
        if minutes >= current:
            continue  # not a shortening — nothing to recommend
        reclaimed = 0.0
        extra_stops = 0.0
        for bound, count in buckets:
            gap = _gap_minutes(bound)
            # Idle tail billed today vs under the candidate, per gap.
            reclaimed += count * (min(gap, current) - min(gap, float(minutes)))
            # A gap longer than the candidate but within today's setting is a
            # stop the warehouse does not make today => a new cold start.
            if minutes < gap <= current:
                extra_stops += count
        stops_per_day = extra_stops / days
        net = reclaimed - extra_stops * startup_penalty_min
        candidates.append({
            "minutes": minutes,
            "reclaimed_min": round(reclaimed, 1),
            "extra_stops": int(extra_stops),
            "extra_stops_per_day": round(stops_per_day, 2),
            "net_reclaimed_min": round(net, 1),
            "acceptable": stops_per_day <= max_extra_stops_per_day and net > 0,
        })

    if not candidates:
        return None, {"candidates": []}
    analysis = {
        "candidates": candidates,
        "max_extra_stops_per_day": max_extra_stops_per_day,
        "churny_stops_per_day": churny_stops_per_day,
        "startup_penalty_min": startup_penalty_min,
        "high_churn": False,
    }

    def _pick(pool: list[dict]) -> dict:
        # Best net reclaim; tie-break toward the LONGER (more forgiving) setting.
        return max(pool, key=lambda c: (c["net_reclaimed_min"], c["minutes"]))

    ok = [c for c in candidates if c["acceptable"]]
    if ok:
        analysis["chosen"] = _pick(ok)
        return int(analysis["chosen"]["minutes"]), analysis

    # Nothing comfortable. Before giving up, check whether a candidate is merely
    # churn-heavy rather than unusable — those are worth showing WITH a warning.
    churny = [
        c for c in candidates
        if c["net_reclaimed_min"] > 0 and c["extra_stops_per_day"] <= churny_stops_per_day
    ]
    if churny:
        analysis["chosen"] = _pick(churny)
        analysis["high_churn"] = True
        return int(analysis["chosen"]["minutes"]), analysis
    return None, analysis


def serverless_autostop_savings_at(
    *,
    net_reclaimed_min: float,
    warm_minutes_current: float,
    monthly_cost: float,
    residual_margin: float = GAP_RESIDUAL_MARGIN,
) -> tuple[float, float]:
    """Price a chosen auto-stop value's NET reclaimed minutes into monthly dollars.

    Unlike :func:`serverless_autostop_savings_from_gaps` (which prices the raw
    reclaim down to the floor), this takes the net figure from
    :func:`choose_serverless_autostop` — already reduced by the startup cost of
    the restarts the new setting introduces — so the dollars correspond to the
    value we actually recommend, not to a theoretical minimum nobody should set.

    Returns ``(monthly_savings_usd, reclaimable_hours)``."""
    if warm_minutes_current <= 0 or monthly_cost <= 0 or net_reclaimed_min <= 0:
        return 0.0, 0.0
    fraction = min(1.0, net_reclaimed_min / warm_minutes_current)
    savings = monthly_cost * fraction * (1.0 - residual_margin)
    return round(savings, 2), round(net_reclaimed_min / 60.0, 1)


#: A conservative "just turn it on" auto-stop for a warehouse that currently has
#: NONE. Well above the tuned 5-10 min window, so it adds essentially no restarts
#: beyond the long idle gaps the warehouse already has — the safe way to bank the
#: bulk of the saving without a churn judgement. 60 min is the classic default.
ENABLE_BASELINE_AUTOSTOP_MIN = 60


def _reclaim_between(gap_histogram: dict[int, float], hi_min: float, lo_min: float) -> tuple[float, float]:
    """Idle minutes reclaimed by moving the auto-stop from ``hi_min`` down to
    ``lo_min``, plus the extra stops that move introduces, from the bucketed gap
    distribution. Per gap the billed idle tail is ``min(gap, setting)``, so the
    reclaim is ``count * (min(gap, hi) - min(gap, lo))`` and a gap in
    ``(lo, hi]`` becomes one new stop. Pure; shared by the tier split and mirrors
    the per-candidate math in :func:`choose_serverless_autostop`."""
    reclaimed = 0.0
    extra_stops = 0.0
    for bound, count in gap_histogram.items():
        gap = float(bound)
        reclaimed += count * (min(gap, hi_min) - min(gap, lo_min))
        if lo_min < gap <= hi_min:
            extra_stops += count
    return reclaimed, extra_stops


def autostop_savings_tiers(
    *,
    gap_histogram: dict[int, float],
    current_autostop_min: float,
    chosen_minutes: int,
    warm_minutes_current: float,
    monthly_cost: float,
    lookback_days: int,
    baseline_minutes: int = ENABLE_BASELINE_AUTOSTOP_MIN,
    startup_penalty_min: float = STARTUP_PENALTY_MIN_SERVERLESS,
    residual_margin: float = GAP_RESIDUAL_MARGIN,
) -> dict:
    """Split the total auto-stop saving into the two levers behind it, so the UI
    can say which dollars come from *enabling* auto-stop vs. *tightening* it.

    For a warehouse with no auto-stop (or one longer than ``baseline_minutes``),
    almost all of the reclaim is "turn it on at all" — a long baseline already
    reclaims the multi-hour weekend/overnight gaps that dominate the bill — and only
    a small tail comes from tuning down into the 5-10 min window at the cost of
    extra restarts. Reporting one blended number against the short setting
    over-attributes the saving to a churny change that is mostly unnecessary.

    Returns ``{"enable": {...} | None, "tighten": {...}, "baseline_minutes"}`` where
    each tier carries ``from_minutes``/``to_minutes``, ``monthly_savings_usd``,
    ``reclaimed_hours`` and (tighten only) ``extra_stops_per_day``. ``enable`` is
    ``None`` when auto-stop is already at/below the baseline (nothing to enable —
    the whole saving is the tighten tier). Tiers never sum to more than the total
    reclaim priced against ``warm_minutes_current`` (the telescoping is exact)."""
    days = max(1.0, float(lookback_days))
    current = float(current_autostop_min) if current_autostop_min else float("inf")

    def _price(reclaimed_min: float) -> float:
        if warm_minutes_current <= 0 or monthly_cost <= 0 or reclaimed_min <= 0:
            return 0.0
        frac = min(1.0, reclaimed_min / warm_minutes_current)
        return round(monthly_cost * frac * (1.0 - residual_margin), 2)

    # The two levers meet at the baseline: everything above it is "enable" (a long,
    # low-churn stop), everything from the baseline down to the chosen value is
    # "tighten". If today's setting is already below the baseline there is no enable
    # tier and the split degenerates to a single tighten tier from today's value.
    hinge = min(current, float(baseline_minutes))

    enable = None
    if current > baseline_minutes:
        r_enable, _ = _reclaim_between(gap_histogram, current, float(baseline_minutes))
        enable = {
            "from_minutes": None if current == float("inf") else int(current),
            "to_minutes": int(baseline_minutes),
            "monthly_savings_usd": _price(r_enable),
            "reclaimed_hours": round(r_enable / 60.0, 1),
        }

    r_tighten, stops_tighten = _reclaim_between(gap_histogram, hinge, float(chosen_minutes))
    net_tighten = max(0.0, r_tighten - stops_tighten * startup_penalty_min)
    tighten = {
        "from_minutes": int(hinge) if hinge != float("inf") else None,
        "to_minutes": int(chosen_minutes),
        "monthly_savings_usd": _price(net_tighten),
        "reclaimed_hours": round(net_tighten / 60.0, 1),
        "extra_stops_per_day": round(stops_tighten / days, 2),
    }
    return {"enable": enable, "tighten": tighten, "baseline_minutes": int(baseline_minutes)}


# --- evidence.savings_curve (plan F.2, PR F1b) ------------------------------
# "Saved so far" values a PARTIAL auto-stop move (240 -> 30 when we proposed 10) on
# the finding's own curve: curve(30) / curve(10) of the estimate, not a straight line
# (finops_rules.change_tracking.curve_points). Both auto-stop rules emit the curve
# as [{"value": minutes, "monthly_usd": ...}, ...] at these candidate settings.

#: Candidate auto-stop values (minutes) the auto-stop rules value in the curve.
#: Overridable per rule via ``savings_curve_minutes`` in thresholds.yaml.
SAVINGS_CURVE_MINUTES = (10, 15, 20, 30, 45, 60, 90, 120)


def _setting_minutes(value) -> float:
    """An auto-stop setting as minutes; 0 / None ("never stops") is infinity."""
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return float("inf")
    return out if out > 0 else float("inf")


def _as_list(value) -> list:
    """An array column as a list: a list (Spark / JSON bridge), a numpy array (SQL
    connector), JSON text, or absent (an older SQL template without the column)."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    try:
        return list(value)
    except TypeError:
        return []


def autostop_curve_values(
    proposed_minutes, current_autostop_min, candidates=SAVINGS_CURVE_MINUTES
) -> list[int]:
    """The settings a savings curve is valued at: the proposal first, then every
    candidate strictly between the proposal and today's setting (a value at or
    above today's setting saves nothing; the tracker anchors today's setting at $0)."""
    proposed = int(proposed_minutes)
    current = _setting_minutes(current_autostop_min)
    out = [proposed]
    for c in sorted({int(c) for c in candidates or ()}):
        if proposed < c < current:
            out.append(c)
    return out


def savings_curve(points, estimate: float) -> list[dict]:
    """Normalize ``[(minutes, monthly_usd), ...]`` into ``evidence.savings_curve``.

    Sorted ascending by setting. The first point (the proposal) is pinned to the
    finding's ``estimate``, so the curve's top equals ``monthly_savings_usd``; every
    later point is clamped to [0, the previous point], so the curve is monotone (a
    longer auto-stop never saves more) and never claims more than the estimate.
    Points without a numeric saving are dropped. ``[]`` when there is no estimate."""
    cleaned: dict[int, float] = {}
    for value, usd in points:
        try:
            v, y = int(value), float(usd)
        except (TypeError, ValueError):
            continue
        if y != y:  # NaN
            continue
        cleaned[v] = max(cleaned.get(v, 0.0), y)
    if not cleaned or not estimate or estimate <= 0:
        return []
    out: list[dict] = []
    cap = float(estimate)
    for i, v in enumerate(sorted(cleaned)):
        y = round(cap if i == 0 else min(cap, max(0.0, cleaned[v])), 2)
        out.append({"value": v, "monthly_usd": y})
        cap = y
    return out


def classic_autostop_curve(
    *,
    curve_minutes,
    curve_reclaimable_minutes,
    target_minutes: int,
    current_autostop_min,
    estimate: float,
    warm_minutes_current: float,
    monthly_cost: float,
    candidates=SAVINGS_CURVE_MINUTES,
) -> list[dict]:
    """``evidence.savings_curve`` for the classic ``warehouse_autostop`` rule.

    The SQL computes the exact-interval counterfactual at every candidate in the
    same pass as the target (``curve_minutes`` / ``curve_reclaimable_minutes``,
    aligned). Each is priced with :func:`warehouse_autostop_savings_from_gaps`, the
    function that priced the estimate, so the point at the target equals it.
    ``[]`` when the row has no curve columns (a template older than PR F1b)."""
    if not _as_list(curve_minutes):
        return []
    allowed = set(autostop_curve_values(target_minutes, current_autostop_min, candidates))
    points: list[tuple[int, float]] = [(int(target_minutes), estimate)]
    for m, r in zip(_as_list(curve_minutes), _as_list(curve_reclaimable_minutes), strict=False):
        try:
            m, r = int(m), float(r)
        except (TypeError, ValueError):
            continue
        if m == int(target_minutes) or m not in allowed:
            continue
        usd, _ = warehouse_autostop_savings_from_gaps(
            reclaimable_min=r, warm_minutes_current=warm_minutes_current,
            monthly_cost=monthly_cost,
        )
        points.append((m, usd))
    return savings_curve(points, estimate)


def serverless_autostop_curve(
    *,
    gap_histogram: dict[int, float],
    current_autostop_min,
    proposed_minutes: int,
    estimate: float,
    warm_minutes_current: float,
    monthly_cost: float,
    startup_penalty_min: float = STARTUP_PENALTY_MIN_SERVERLESS,
    candidates=SAVINGS_CURVE_MINUTES,
) -> list[dict]:
    """``evidence.savings_curve`` for ``warehouse_autostop_serverless``, from the gap
    histogram the rule already has: at each candidate, the NET reclaim (idle tail
    removed minus the startup penalty of the restarts it adds), priced like the
    estimate with :func:`serverless_autostop_savings_at`.

    Above 60 min the open-ended histogram bucket is keyed at its average gap, so the
    90/120 points are approximate; :func:`savings_curve` still bounds them by the
    estimate and keeps the curve monotone."""
    current = _setting_minutes(current_autostop_min)
    points: list[tuple[int, float]] = [(int(proposed_minutes), estimate)]
    for m in autostop_curve_values(proposed_minutes, current_autostop_min, candidates)[1:]:
        reclaimed, extra_stops = _reclaim_between(gap_histogram or {}, current, float(m))
        usd, _ = serverless_autostop_savings_at(
            net_reclaimed_min=reclaimed - extra_stops * startup_penalty_min,
            warm_minutes_current=warm_minutes_current,
            monthly_cost=monthly_cost,
        )
        points.append((m, usd))
    return savings_curve(points, estimate)


def cluster_autoterm_savings(
    *,
    reclaimable_gross_min: float,
    num_reclaim_gaps: int,
    running_minutes: float,
    monthly_cost: float,
    restart_penalty_min: int = RESTART_PENALTY_MIN,
    residual_margin: float = GAP_RESIDUAL_MARGIN,
) -> tuple[float, float]:
    """Price gap-based reclaimable idle for a cluster's auto-termination.

    Unlike the old ``monthly_cost * idle_fraction * fudge`` model, the inputs
    here come from the *actual* contiguous idle gaps (the SQL sessionizes
    per-minute CPU): ``reclaimable_gross_min`` is the idle time in gaps longer
    than the proposed timeout, i.e. what auto-termination could actually reclaim
    (scattered idle between bursts, shorter than the timeout, is NOT counted).

    Each reclaimed gap costs one extra cold start, so we net
    ``restart_penalty_min`` per gap, then price against the billed running time
    with a small residual margin. Returns ``(monthly_savings_usd, reclaimable_hours)``.

    IMPORTANT: this is an *upper bound* on realizable savings — CPU idle is only
    a proxy for the command/session inactivity auto-termination actually keys on,
    and that signal is not exposed for interactive clusters. Callers flag this as
    medium (not high) confidence."""
    if running_minutes <= 0 or monthly_cost <= 0 or reclaimable_gross_min <= 0:
        return 0.0, 0.0
    net = max(0.0, reclaimable_gross_min - num_reclaim_gaps * restart_penalty_min)
    fraction = min(1.0, net / running_minutes)
    return round(monthly_cost * fraction * (1.0 - residual_margin), 2), round(net / 60.0, 1)


# --- spot-with-fallback savings (Azure/AWS/GCP classic compute) ------------
# Moving on-demand workers to spot saves the spot/on-demand VM price gap, but:
#   * only the WORKER fraction of a cluster can go spot (the driver stays
#     on-demand to preserve session/REPL state) => cap at workers/(workers+1),
#   * only a portion of a workload is eviction-tolerant => movable_fraction,
#   * some savings are lost replaying evicted work => restart_overhead.
# The estimate is deliberately conservative: it uses the MID movable scenario
# and the size-aware cap, and never claims driver spend.


def spot_fallback_savings(
    *,
    on_demand_cost: float,
    workers: int,
    movable_fraction: float = 0.50,
    spot_discount: float = 0.70,
    restart_overhead: float = 0.10,
) -> tuple[float, float]:
    """Savings from moving an on-demand classic cluster to SPOT_WITH_FALLBACK.

    ``on_demand_cost``   billed on-demand compute cost over the window (already
                         in monthly terms when called from a rule).
    ``workers``          worker count (fixed or autoscale max); caps the spot
                         fraction at ``workers / (workers + 1)`` because the
                         driver never goes spot. 0 workers => nothing movable.
    ``movable_fraction`` share of the workload eviction-tolerant enough to move
                         (mid scenario 0.50; conservative default).
    ``spot_discount``    per-VM spot vs on-demand price gap (0.70 = 70% cheaper).
    ``restart_overhead`` fraction of savings lost replaying evicted work.

    Returns ``(monthly_savings_usd, effective_spot_fraction)``. Conservative: it
    prices only the size-capped, movable, eviction-netted worker spend.
    """
    if on_demand_cost <= 0 or workers < 1:
        return 0.0, 0.0
    max_spot_frac = workers / (workers + 1.0)
    eff_frac = min(1.0, max(0.0, movable_fraction)) * max_spot_frac
    savings = (
        on_demand_cost * eff_frac * spot_discount * (1.0 - restart_overhead)
    )
    return round(max(0.0, savings), 2), round(eff_frac, 3)


# --- serverless cost-optimized (Standard) mode savings ---------------------
# Serverless Jobs/DLT on PERFORMANCE_OPTIMIZED can switch to Standard
# (cost-optimized) mode, which uses ~SERVERLESS_STANDARD_DBU_REDUCTION fewer
# DBUs at the same $/DBU, net of slower runtime. Short
# workloads are penalized because the cold-start overhead is proportionally
# large, so they are less suitable and we discount their savings.

#: DBU reduction Standard mode delivers vs Performance-Optimized (~50%).
SERVERLESS_STANDARD_DBU_REDUCTION = 0.50


def serverless_cost_optimized_savings(
    *,
    perf_optimized_cost: float,
    duration_suitability: float,
    dbu_reduction: float = SERVERLESS_STANDARD_DBU_REDUCTION,
) -> float:
    """Savings from moving a serverless Job/pipeline to Standard mode.

    ``perf_optimized_cost``  current cost of PERFORMANCE_OPTIMIZED serverless
                             usage over the window (monthly when called).
    ``duration_suitability`` 0..1 factor discounting short workloads whose
                             cold-start overhead makes Standard a poor fit
                             (1.0 = long/well-suited, ~0.25 = short).
    Returns the monthly savings, bounded to the reducible DBU share and scaled
    by suitability so short workloads are not over-promised.
    """
    if perf_optimized_cost <= 0 or duration_suitability <= 0:
        return 0.0
    savings = perf_optimized_cost * dbu_reduction * min(1.0, duration_suitability)
    return round(max(0.0, savings), 2)


# --- failing-job reliability waste (G1) ------------------------------------
# Every run that ends in a terminal error still consumed compute: the driver
# spun up, the task executed until it raised, and those DBUs were billed. That
# spend produced nothing. We measure it directly — the sum of billed cost on
# runs whose result_state is a failure — rather than estimating a fraction, so
# the "waste" is real dollars, not a guess.
#
# The SAVING is smaller than that waste, for two independent reasons:
#
#   1. Excess-failure share. Some failures are unavoidable noise (a flaky
#      upstream, the occasional bad input that SHOULD fail fast). Only the share
#      of failures ABOVE a healthy baseline is addressable, so we scale by
#      ``(fail_rate - baseline) / fail_rate``.
#
#   2. Completion cost. Fixing a failing run does NOT make its compute vanish —
#      it turns a failed run into a SUCCESSFUL one, which runs to completion and
#      still bills DBUs (often MORE, since the failure was cutting it short). The
#      only way the full failed spend disappears is to RETIRE the job. Since
#      retiring a job that half-works is rarely feasible, we credit the realistic
#      fix outcome: the failed-run spend MINUS what those runs will cost once they
#      complete cleanly, estimated from this job's own successful runs. A job that
#      never once succeeds has no completion cost to net out and is a genuine
#      retire candidate, so there we fall back to the full excess-failure spend.


def failing_job_savings(
    *,
    failed_run_cost: float,
    fail_rate: float,
    baseline_fail_rate: float,
    avg_failed_run_cost: float = 0.0,
    avg_success_run_cost: float | None = None,
) -> float:
    """Recoverable monthly spend from fixing a job's excess failures.

    ``failed_run_cost``      billed compute on failed runs over the window
                             (monthly when called) — measured, not estimated.
    ``fail_rate``            share of terminal runs that failed (0..1).
    ``baseline_fail_rate``   the failure rate we treat as unavoidable noise; only
                             failures ABOVE this are considered recoverable.
    ``avg_failed_run_cost``  mean billed cost of a failed run (per-run; any window,
                             the units cancel in the ratio below).
    ``avg_success_run_cost`` mean billed cost of a SUCCESSFUL run of the same job,
                             or ``None`` if the job never succeeded in the window.

    We first scale the measured failed-run spend by the *excess* failure fraction
    ``(fail_rate - baseline) / fail_rate`` (reason 1 above). We then net out the
    completion cost (reason 2): a fixed run becomes a successful run that still
    bills ``avg_success_run_cost``, so only the fraction
    ``1 - avg_success_run_cost / avg_failed_run_cost`` of each failed run's spend
    is genuinely reclaimed. Algebraically the result is
    ``excess * (failed_run_cost - failed_runs * avg_success_run_cost)`` — the
    per-failure form, rewritten as a ratio so it is unit-safe against a monthly
    ``failed_run_cost``.

    Conservative throughout: never exceeds the spend actually burned on failures,
    never counts the healthy-baseline share, and returns 0 when a clean run would
    cost as much as the failed one (fixing frees no compute, it just moves the
    spend from wasted to productive). Falls back to the retire basis (full
    excess-failure spend) only when there is no successful run to price.
    """
    if failed_run_cost <= 0 or fail_rate <= 0:
        return 0.0
    excess = (fail_rate - baseline_fail_rate) / fail_rate
    if excess <= 0:
        return 0.0
    excess = min(1.0, excess)
    # Retire basis: scrap the job and the whole excess-failure spend disappears.
    retire_savings = failed_run_cost * excess
    # No successful run to price the fixed state (or no per-run failed cost to form
    # the ratio): a never-succeeding job is a retire candidate, so credit the full
    # excess-failure spend.
    if avg_success_run_cost is None or avg_failed_run_cost <= 0:
        return round(retire_savings, 2)
    # Fraction of each failed run's spend actually freed by fixing it: what it costs
    # now, minus what it will cost once it completes successfully.
    reclaim_fraction = 1.0 - (avg_success_run_cost / avg_failed_run_cost)
    if reclaim_fraction <= 0:
        return 0.0
    return round(retire_savings * reclaim_fraction, 2)


# --- over-provisioned job compute (jobs-compute rightsize) -----------------
# A job cluster that is oversized wastes compute on nodes that do little. Three
# symptoms, each with a different STRUCTURAL recoverable base:
#   * driver_bound   -- the workers sit idle while the driver does all the work
#                       (a single-node/pandas job in disguise). Going single-node
#                       frees the entire WORKER share of the cluster cost.
#   * idle_workers   -- too many workers, all lightly used. Roughly halving the
#                       worker count frees about half the worker share.
#   * memory_oversize-- far more memory provisioned than touched. A smaller-memory
#                       node class frees a slice of the total node cost.
# We then apply a conservative haircut (``savings_fraction``) on top, because CPU
# and memory are proxies for the binding resource and some runs may need the
# capacity at their peak. The fraction is intentionally cautious and tunable once
# the rule is validated on live data.

#: A memory-class downsize frees ~half the node cost (a memory-optimized instance
#: is roughly twice the price of the general-purpose one at the same core count).
_MEMORY_DOWNSIZE_BASE = 0.5


def jobs_compute_rightsize_savings(
    *,
    symptom: str,
    monthly_cost: float,
    worker_cost_fraction: float,
    savings_fraction: float,
) -> float:
    """Conservative monthly saving from rightsizing over-provisioned job compute.

    ``symptom``               ``driver_bound`` | ``idle_workers`` | ``memory_oversize``.
    ``monthly_cost``          the job's measured monthly compute spend.
    ``worker_cost_fraction``  share of that spend attributable to WORKER nodes
                              (0..1), estimated from node counts x per-node cores.
    ``savings_fraction``      conservative haircut applied to the structural base.

    Recoverable base by symptom: ``driver_bound`` frees the whole worker share (go
    single-node), ``idle_workers`` half of it (roughly halve the workers),
    ``memory_oversize`` a fixed slice of total spend (a smaller-memory node class).
    Bounded to ``[0, monthly_cost]``; returns 0 for non-positive cost or an
    unknown symptom.
    """
    if monthly_cost <= 0:
        return 0.0
    wf = min(1.0, max(0.0, worker_cost_fraction))
    if symptom == "driver_bound":
        base = wf
    elif symptom == "idle_workers":
        base = 0.5 * wf
    elif symptom == "memory_oversize":
        base = _MEMORY_DOWNSIZE_BASE
    else:
        return 0.0
    saving = monthly_cost * base * max(0.0, savings_fraction)
    return round(min(saving, monthly_cost), 2)


# --- unused-data production waste (G2) -------------------------------------
# A job that writes a table nothing ever reads is producing dead data: the
# compute to build it is spent every run, and no downstream query, dashboard,
# or model consumes the output. system.access.table_lineage records every
# write (target_table) and every read (source_table); a table that appears only
# as a target, never as a source, and whose producing job's other outputs are
# also unread, is a strong unused-output signal.
#
# Confidence is LOW by construction: lineage read-capture is incomplete (a read
# through a path, an external tool, or an un-instrumented surface may not
# register), so "zero reads observed" is not "provably never read". We therefore
# recommend VERIFICATION and only credit a conservative share of the producing
# cost, never the whole thing.


def unused_data_recoverable_fraction(
    *,
    observed_days: int,
    lookback_days: int,
    floor: float = 0.5,
    high: float = 0.7,
    consistent_day_frac: float = 0.8,
) -> float:
    """Pick the credited share for an unused-data-job finding by how STRONG the
    "dead data" evidence is.

    A job that produced on nearly every day of the window and was still never read
    is a stronger signal that the output is genuinely dead than one that wrote only
    a few times, so it earns the ``high`` credit; otherwise we keep the conservative
    ``floor``. This scales on OBSERVED consistency — the signal we have in a
    lookback window — not on table age, which a 30-day window cannot see. The result
    never exceeds ``high``, keeping the estimate under true avoidable spend.
    """
    if lookback_days <= 0:
        return floor
    day_frac = max(0.0, min(1.0, observed_days / float(lookback_days)))
    return high if day_frac >= consistent_day_frac else floor


def unused_data_job_savings(
    *,
    production_cost: float,
    recoverable_fraction: float = 0.5,
) -> float:
    """Recoverable monthly spend from retiring a job whose output is never read.

    ``production_cost``       billed compute of the producing job over the
                              window (monthly when called).
    ``recoverable_fraction``  share we treat as safely reclaimable given that
                              lineage reads are incompletely captured (default
                              0.5 — we assume up to half the "no reads" signal
                              could be a capture gap, so we halve the claim).

    Deliberately conservative: pausing the job reclaims 100% of its compute, but
    because we cannot PROVE the output is unread we only promise a fraction and
    flag the finding low-confidence with a "verify then pause" action.
    """
    if production_cost <= 0 or recoverable_fraction <= 0:
        return 0.0
    return round(production_cost * min(1.0, recoverable_fraction), 2)


# --- warehouse size right-sizing (G3) --------------------------------------
# A SQL warehouse's t-shirt size sets its DBU/hour rate: each step down the
# ladder (e.g. SMALL -> X_SMALL) roughly HALVES the rate (the cluster is half
# the nodes). Downsizing is safe when the current size is not actually needed —
# i.e. queries never queue for capacity, never spill to disk, and the data
# volumes are small — so the same work would run on a smaller warehouse for
# proportionally less money (a little slower, which is acceptable when nothing
# is latency-bound). We size the saving to the DBU-rate ratio of the proposed
# vs current size, applied only to the active compute cost, and cap the number
# of steps down by how much idle headroom the load actually shows.

#: DBU/hour ladder factor between adjacent warehouse sizes. Each size up doubles
#: the cluster (and the rate); each size down halves it. Databricks' published
#: ladder (2X_SMALL .. 4X_LARGE) is a strict power-of-two progression.
WAREHOUSE_SIZE_STEP_FACTOR = 2.0

#: Canonical warehouse size ladder, smallest first. A size's DBU/hr rate is
#: WAREHOUSE_SIZE_STEP_FACTOR^index. Used to price a proposed downsize as the
#: exact rate ratio between two rungs — not a guessed percentage.
WAREHOUSE_SIZE_LADDER = [
    "2X_SMALL",
    "X_SMALL",
    "SMALL",
    "MEDIUM",
    "LARGE",
    "X_LARGE",
    "2X_LARGE",
    "3X_LARGE",
    "4X_LARGE",
    "5X_LARGE",
]

#: Spark task SLOTS per warehouse size. A 2X-Small is a single node of ~4 slots
#: (cores) and each step up the ladder doubles the cluster, hence the slots — the
#: same power-of-two progression as the DBU rate. Used only to express measured
#: slot occupancy (from total_task_duration_ms) as a % of capacity, and to check a
#: proposed smaller size would still cover the observed PEAK slot demand. This is a
#: capacity ESTIMATE, not a billed quantity Databricks publishes per size, so the
#: rule treats it as corroborating evidence + a safety gate, never the sole basis
#: for the dollar figure (which comes from the exact DBU-rate ratio).
WAREHOUSE_SIZE_SLOTS = {
    size: 4 * (2 ** i) for i, size in enumerate(WAREHOUSE_SIZE_LADDER)
}


def warehouse_size_slots(size: str) -> int | None:
    """Estimated Spark task-slot count for a warehouse ``size``; None if unknown."""
    if not size:
        return None
    return WAREHOUSE_SIZE_SLOTS.get(
        str(size).upper().replace("XSMALL", "X_SMALL")
    )


def max_safe_steps_down_by_slots(
    *,
    current_size: str,
    peak_slots_used: float,
    headroom_factor: float = 2.0,
    max_steps: int = 3,
) -> int:
    """How many ladder steps down still leave the PEAK measured slot demand within
    the smaller size's capacity, divided by ``headroom_factor`` for safety.

    Each step halves the slot count, so a size at ``s`` slots stepped ``k`` down has
    ``s / 2**k`` slots; we require ``peak_slots_used * headroom_factor`` to fit under
    that. Returns 0 when even one step would not keep the safety margin (so the
    caller keeps the size), capped at ``max_steps``. Pure; the dollar saving is
    priced separately from the exact rate ratio.

    ``headroom_factor=2`` means we only step down while peak demand uses under half
    the smaller size's slots — deliberately conservative, because a larger size also
    speeds each individual query, an effect slot occupancy alone does not capture."""
    slots = warehouse_size_slots(current_size)
    if not slots or peak_slots_used is None or peak_slots_used < 0:
        return 0
    need = max(0.0, float(peak_slots_used)) * float(headroom_factor)
    steps = 0
    while steps < max_steps:
        smaller = slots / (2 ** (steps + 1))
        if smaller < 4:  # never below a single 2X-Small node
            break
        if need > smaller:
            break
        steps += 1
    return steps


def warehouse_downsize_savings(
    *,
    monthly_cost: float,
    current_size: str,
    steps_down: int,
    runtime_ratio: float = 1.0,
) -> tuple[float, str | None]:
    """Savings from dropping a warehouse ``steps_down`` rungs on the size ladder.

    Each rung halves the DBU/hour *rate*, so moving from ladder index ``i`` to
    ``i - steps_down`` multiplies the rate by ``size_ratio = 2^-steps_down``. But
    the same query on a smaller warehouse has fewer task slots, so a compute-bound
    query runs LONGER, clawing back part of the rate cut. This prices that
    elasticity explicitly:

        candidate_cost = current_cost * size_ratio * runtime_ratio
        saving         = current_cost * (1 - size_ratio * runtime_ratio)

    ``runtime_ratio`` is how much longer the work takes at the smaller size and
    must be >= 1 (a smaller warehouse never runs the same work faster):

      * ``1.0`` — the work still fits the smaller size's slots, so it does not
        slow down and the full rate cut is realised. This is the value the caller
        passes when a MEASURED slot signal shows the peak demand still fits the
        target size (the only case the rule books today), and the default so
        existing rate-only callers are unchanged.
      * ``1/size_ratio`` (e.g. 2.0 for one step) — the work is fully elastic /
        compute-bound: halving the slots doubles the runtime, the candidate cost
        equals the current cost and the saving is 0.

    The candidate rate is clamped to 1.0 so an over-elastic input can only zero
    the saving, never invert it. Returns ``(monthly_savings_usd, proposed_size)``;
    ``proposed_size`` is ``None`` (savings 0) when the size is unknown or already
    at the floor, so the rule suppresses those.

    The rate ladder is exact; the inferences are *how many* steps are safe and the
    runtime_ratio — both decided upstream from the measured slot/queue/spill
    headroom, not here."""
    if monthly_cost <= 0 or steps_down < 1:
        return 0.0, None
    size = (current_size or "").upper().replace("XSMALL", "X_SMALL")
    if size not in WAREHOUSE_SIZE_LADDER:
        return 0.0, None
    idx = WAREHOUSE_SIZE_LADDER.index(size)
    target_idx = idx - steps_down
    if target_idx < 0:
        target_idx = 0
        steps_down = idx
    if steps_down < 1:
        return 0.0, None
    rate_ratio = WAREHOUSE_SIZE_STEP_FACTOR ** (-steps_down)  # e.g. 1 step -> 0.5
    # Runtime elasticity: a smaller size runs compute-bound work longer, so the
    # candidate cost is the rate cut times that runtime growth. Clamp the growth
    # to >= 1 and the resulting rate to <= 1 (can only erase the saving).
    candidate_rate = min(1.0, rate_ratio * max(1.0, float(runtime_ratio)))
    savings = monthly_cost * (1.0 - candidate_rate)
    return round(max(0.0, savings), 2), WAREHOUSE_SIZE_LADDER[target_idx]


# --- warehouse scale-OUT (under-provisioned: queries queue at capacity) -----
# The mirror image of warehouse_rightsize. Right-size shrinks a warehouse whose
# queries never queue and never spill; scale-out GROWS a warehouse whose queries
# wait for capacity. The lever is deliberately max_clusters, NOT the t-shirt size:
#   * queueing at capacity = too few clusters for the CONCURRENCY (many queries
#     arriving at once, each small) -> add a cluster, work runs in parallel.
#   * spilling to disk        = too few nodes for one query's DATA -> size up.
# Verified in the field: warehouses that queue overwhelmingly do NOT spill (a
# concurrency problem, not a per-query-memory one), so the honest fix for the
# common case is another cluster, which multi-cluster load balancing spins up
# only while the queue exists and stops when it drains.
#
# This is NOT a savings rule. Adding a cluster raises the bill during busy hours;
# it buys back user wait time and reliability, not dollars. So — like
# untagged_spend — the finding books $0 savings and ranks on the size of the
# measured queue pain instead, and the caller frames it as a performance fix.

#: Ceiling on how many clusters we will propose in a single step. A warehouse
#: pinned at max_clusters=1 under heavy contention wants headroom, but jumping it
#: to 8 in one recommendation is a bill surprise; we step conservatively and let
#: the operator widen further after watching the queue drain.
MAX_SCALE_OUT_STEP = 2


#: Rule of thumb: a DBSQL cluster comfortably serves ~10 concurrent
#: queries even for heavy CTAS-style work, so peak concurrent queries / this is a
#: principled floor on the clusters a warehouse needs. Used as corroborating evidence
#: and to choose the step size, never to blindly jump many clusters at once.
DEFAULT_MAX_QUERIES_PER_CLUSTER = 10


def concurrency_implied_clusters(
    *,
    peak_concurrent_queries: float | None,
    max_queries_per_cluster: float = DEFAULT_MAX_QUERIES_PER_CLUSTER,
) -> int | None:
    """Clusters implied by peak concurrency, using the ~10-queries-per-
    cluster heuristic: ``ceil(peak_concurrent_queries / max_queries_per_cluster)``.

    Returns the ideal cluster count (>=1), or ``None`` when no concurrency signal is
    available (the caller then falls back to the queue-share step). This is a
    concurrency FLOOR, deliberately coarse — heavy queries want fewer per cluster, so
    10 is a safe upper bound that will not under-provision."""
    if peak_concurrent_queries is None or max_queries_per_cluster <= 0:
        return None
    peak = max(0.0, float(peak_concurrent_queries))
    if peak <= 0:
        return None
    import math

    return max(1, math.ceil(peak / float(max_queries_per_cluster)))


def warehouse_scale_out_target(
    *,
    current_max_clusters: int,
    queued_pct: float,
    ceiling: int = MAX_SCALE_OUT_STEP,
) -> int | None:
    """Recommended ``max_clusters`` for a warehouse whose queries queue at
    capacity, or ``None`` when no increase is warranted.

    Multi-cluster load balancing adds a cluster when queries queue and removes it
    when the queue drains, so the extra capacity is only paid for while it is
    needed. We raise the CEILING by one step (two under severe queueing) rather
    than the floor, so idle-hour cost is unchanged — the warehouse still scales
    back to ``min_clusters`` when quiet. Conservative by design: a single step,
    widened later by the operator once they have watched the queue clear."""
    cur = max(1, int(current_max_clusters or 1))
    if queued_pct <= 0:
        return None
    # One extra cluster normally; two when a large share of queries are waiting,
    # which one added cluster is unlikely to fully absorb.
    step = 2 if queued_pct >= 0.25 else 1
    step = min(step, max(1, ceiling))
    target = cur + step
    return target if target > cur else None


# --- idle model serving endpoints ------------------------------------------

def idle_serving_endpoint_savings(
    *,
    monthly_cost: float,
    requests: int,
    scale_to_zero_available: bool,
    treat_as_zero_traffic: bool = False,
    is_low_traffic: bool = False,
    low_traffic_reclaim_frac: float = 0.0,
) -> float:
    """Reclaimable monthly spend on a continuously-billing serving endpoint that
    serves no, or very little, traffic.

    ``monthly_cost``            billed serving cost, already monthly.
    ``requests``                inference requests measured in the window.
    ``scale_to_zero_available`` True when the fix is a CONFIG change (drop
                                min_concurrency to 0 / enable scale-to-zero),
                                False when the only lever is deleting the
                                endpoint (e.g. provisioned throughput).
    ``is_low_traffic``          True when the endpoint serves a little traffic but
                                far too little to justify always-warm replicas.
    ``low_traffic_reclaim_frac`` conservative share of the always-on cost a
                                scale-to-zero config would reclaim on a low-traffic
                                endpoint (the rest covers the active windows).

    Unlike the lineage-based rules we are not inferring disuse from a proxy: the
    request count is measured per-request in ``system.serving.endpoint_usage``.

    * **Zero traffic:** the whole always-on cost is avoidable (warm replicas nobody
      calls) — credit 100%, whether the lever is scale-to-zero or deletion.
    * **Low traffic:** scale-to-zero is now GA and can save
      most of the always-on cost on an endpoint hit only occasionally, at the price
      of a cold start on the first request in an idle window. That is a config
      change only (never deletion — something still depends on it), so we credit a
      conservative fraction and flag the latency trade-off. Requires
      ``scale_to_zero_available``; otherwise there is no cheap lever and we book $0.
    * **Anything busier:** the endpoint is genuinely in use — book $0.
    """
    if monthly_cost <= 0:
        return 0.0
    if requests <= 0 or treat_as_zero_traffic:
        # Zero (or within-tolerance) traffic: the whole always-on cost is avoidable,
        # whether the lever is scale-to-zero or deletion.
        _ = scale_to_zero_available
        return round(monthly_cost, 2)
    if is_low_traffic and scale_to_zero_available and low_traffic_reclaim_frac > 0:
        return round(monthly_cost * min(1.0, low_traffic_reclaim_frac), 2)
    return 0.0


# --- idle Databricks Apps --------------------------------------------------
# A Databricks App that is deployed and running holds serverless compute and bills
# continuously (billing_origin_product = 'APPS'). An app nobody opens is pure waste,
# the direct analogue of an idle serving endpoint. The access signal comes from
# system.access.audit (app HTTP access).


def idle_app_savings(
    *,
    monthly_cost: float,
    is_idle: bool,
) -> float:
    """Reclaimable monthly spend on a continuously-billing app judged idle.

    An app with no access in the window bills for compute nobody uses, so the whole
    always-on cost is avoidable (stop or delete it). The caller owns the idle
    decision (access within its tolerance); this books the full cost when idle and
    nothing otherwise — the same binary the idle-serving-endpoint rule uses for its
    high-certainty tier.
    """
    if monthly_cost <= 0 or not is_idle:
        return 0.0
    return round(monthly_cost, 2)


# --- helpers to derive the inputs above from an hourly state series --------

def summarize_hourly_states(states: list[int]) -> dict:
    """Roll a flat hourly state series (0=stopped, 1=active, 2=idle-warm) into
    the aggregates the savings functions need. ``active_sessions`` counts runs of
    active hours (a proxy for distinct query sessions)."""
    warm = sum(1 for s in states if s in (1, 2))
    idle_warm = sum(1 for s in states if s == 2)
    sessions = 0
    prev_active = False
    for s in states:
        active = s == 1
        if active and not prev_active:
            sessions += 1
        prev_active = active
    return {
        "warm_hours": float(warm),
        "idle_warm_hours": float(idle_warm),
        "active_sessions": sessions,
    }


def continuous_pipeline_savings(
    *,
    billed_hours: float,
    active_hours: float,
    monthly_cost: float,
    startup_overhead_frac: float = 0.15,
) -> tuple[float, float]:
    """Savings from moving a continuous DLT pipeline to a triggered schedule.

    Returns ``(savings, idle_hours)``.

    A continuous pipeline holds compute up permanently, so it bills every hour in
    the window; a triggered one bills only while an update runs. The recoverable
    spend is therefore the cost of the hours that were billed but had NO update in
    flight — a figure counted from two system tables rather than modelled:

        idle_fraction = (billed_hours - active_hours) / billed_hours

    Three deliberate conservatisms, so the estimate stays under true avoidable
    spend:

    * **Only idle hours count.** The active-hours cost is work that still has to
      run after the change, so it is never included.
    * **Whole hours only.** Hour granularity means a partly-active hour counts as
      fully active, which rounds the saving DOWN.
    * **A startup overhead is subtracted.** A triggered pipeline pays to spin
      compute up for each run; ``startup_overhead_frac`` gives that back out of the
      reclaimed idle cost, so a pipeline triggered very often keeps less of it.
    """
    if billed_hours <= 0 or monthly_cost <= 0:
        return 0.0, 0.0
    idle_hours = max(0.0, billed_hours - max(0.0, active_hours))
    if idle_hours <= 0:
        return 0.0, 0.0
    idle_fraction = min(1.0, idle_hours / billed_hours)
    idle_cost = monthly_cost * idle_fraction
    savings = idle_cost * (1.0 - startup_overhead_frac)
    return round(max(0.0, savings), 2), round(idle_hours, 1)
