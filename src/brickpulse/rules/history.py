"""The minimum-observation guard: don't judge a resource we've barely seen.

Every rule prices waste from a *sample* of a resource's behaviour, then
extrapolates it to a month (:func:`finops_rules.pricing.to_monthly`). That
extrapolation is only meaningful once the sample is long enough to be
representative. A cluster created yesterday afternoon that idled for six hours
is statistically indistinguishable from one that has idled for a month — but
recommending "terminate this zombie" on one afternoon of data is sampling noise
dressed up as a finding, and a single such recommendation costs the app its
credibility with a platform admin.

So each rule's SQL emits ``observed_days``: the number of DISTINCT calendar days
on which the resource was actually seen doing something, measured on that rule's
own primary evidence source (node-timeline samples for clusters, query history
for warehouses, run/write events for jobs). :func:`insufficient_history` turns
that into a suppression decision, and rules call it as their first guard.

Two deliberate choices:

* **Distinct days, not a contiguous streak.** A weekday-only cluster observed
  Mon-Fri has five days of solid evidence even though the series breaks every
  weekend; demanding an unbroken run would suppress exactly the well-understood
  resources we most want to report on. What we are ruling out is the resource
  that has only *existed* for a day or two.
* **Engine-wide, not per-rule.** "Too new to judge" is a property of the
  resource and its data, not of the criterion being applied, so the threshold
  lives at the top level of ``config/thresholds.yaml`` (``min_observation_days``)
  and every rule inherits it through :class:`~finops_rules.contract.RuleContext`.

The guard is also bounded by the window itself: a 3-day lookback can never
produce 5 observed days, so :func:`effective_min_days` clamps the requirement to
the lookback and the caller is expected to configure a window at least as long
as the threshold (see ``Settings.live_lookback_days``).
"""

from __future__ import annotations

from .contract import DEFAULT_MIN_OBSERVATION_DAYS, RuleContext

#: Below this lookback, a window can fall entirely inside one part of a monthly cycle
#: (a quiet week, or conversely a month-end close / billing run), so the idle share it
#: measures — and therefore the extrapolated saving — may not represent a full month.
#: A quiet window OVERSTATES idle and savings, which is the error we most want to avoid,
#: so short-window findings carry a cyclicality caveat.
#: The precompute (customer-facing) mode runs a 30-day window and never trips this; only
#: the shorter live-mode lookback does.
SHORT_LOOKBACK_DAYS = 14


def effective_min_days(ctx: RuleContext) -> int:
    """The observation minimum actually enforced under ``ctx``.

    Clamped to the lookback window because a window shorter than the threshold
    could never satisfy it — that would silently suppress every finding rather
    than filter the unrepresentative ones. Never below 1: a resource with zero
    observed days has no evidence at all and must always be suppressed."""
    configured = int(getattr(ctx, "min_observation_days", DEFAULT_MIN_OBSERVATION_DAYS) or 0)
    return max(1, min(configured, int(ctx.lookback_days or 1)))


def observed_days(row: dict) -> int | None:
    """Distinct days the resource was observed active, as the rule's SQL measured
    it. ``None`` when the column is absent — an older snapshot or a rule whose
    template predates the guard, which :func:`insufficient_history` treats as
    "unknown, don't block" so a missing column can never silently empty the app."""
    value = row.get("observed_days")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def insufficient_history(row: dict, ctx: RuleContext) -> bool:
    """True when ``row`` has too few observed days to support a recommendation.

    Rules call this FIRST, before any pricing:

        if insufficient_history(row, ctx):
            return None

    Returns False when ``observed_days`` is missing entirely (unknown history is
    not evidence of short history)."""
    days = observed_days(row)
    if days is None:
        return False
    return days < effective_min_days(ctx)


def history_note(row: dict, ctx: RuleContext) -> str:
    """One sentence for a finding's ``data_basis`` stating how much observation
    backs it. Makes the sample size auditable in the UI instead of implicit, and
    reads honestly for a resource sitting just above the bar."""
    days = observed_days(row)
    if days is None:
        return ""
    minimum = effective_min_days(ctx)
    note = (
        f" Measured across {days} day{'s' if days != 1 else ''} of observed "
        f"activity (minimum {minimum} before any action is recommended)."
    )
    return note + short_lookback_note(ctx)


def short_lookback_note(ctx: RuleContext) -> str:
    """Cyclicality caveat for a short lookback.

    A window shorter than :data:`SHORT_LOOKBACK_DAYS` can sit entirely in one part of
    a monthly pattern (a quiet week understates load; a month-end close overstates it),
    so the extrapolated monthly figure may not be representative. Empty for the 30-day
    precompute window; non-empty only for the shorter live-mode lookback."""
    lookback = int(getattr(ctx, "lookback_days", 0) or 0)
    if 0 < lookback < SHORT_LOOKBACK_DAYS:
        return (
            f" NOTE: this is a short {lookback}-day lookback — it can miss monthly "
            f"cycles (month-end closes, billing runs), so treat the figure as "
            f"indicative and confirm over a full month before acting."
        )
    return ""


def history_evidence(row: dict, ctx: RuleContext) -> dict:
    """Observation-window keys for a finding's ``evidence`` dict, so the detail
    page's raw table shows the sample size behind the dollar figure."""
    days = observed_days(row)
    if days is None:
        return {}
    return {
        "observed_days": days,
        "min_observation_days": effective_min_days(ctx),
    }
