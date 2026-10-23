"""The "Saved so far" models: what changed on flagged resources, valued from our estimate.

Read from the savings tracker's history tables (``finops_savings_daily`` +
``finops_change_events``, written by ``precompute/change_tracker.py``). Separate from
the recommendation contract on purpose: a recommendation is an open opportunity, this
is a change that already happened. Neither is derived from the other, and nothing here
feeds the "Savings potential" headline or the NBA ranking.

Back-compat: the pipeline and the app deploy separately, so every field has a
default, and ``by_kind`` / ``not_counted`` are open dicts keyed by whatever the tracker
wrote. The UI falls back to the raw key for one it doesn't know (``LABEL[x] ?? x``).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class RealizedMonth(BaseModel):
    """Booked saving in one calendar month of the window (``YYYY-MM``)."""

    month: str
    saved_usd: float = 0.0


class RealizedKind(BaseModel):
    """Findings in one change bucket.

    ``count`` is the findings booked in the window, by their latest state.
    ``monthly_usd`` is this bucket's share of ``run_rate_usd``. Each deduped pool on the
    run-rate day is split across the kinds in effect in it, in proportion to their own
    valuations, so the buckets sum to the run-rate. A finding no longer in effect
    (moved back, workspace retired) counts but adds $0."""

    count: int = 0
    monthly_usd: float = 0.0


class RealizedNotCounted(BaseModel):
    """Changes the tracker saw but did not book, for one ``not_counted_reason``."""

    count: int = 0
    workspace_count: int = 0
    monthly_usd: float = 0.0


class RealizedExample(BaseModel):
    """The biggest partial change, shown in the tooltip ("auto-stop 240 → 30 min")."""

    finding_key: str
    rule_id: str | None = None
    resource_name: str | None = None
    setting: str | None = None
    baseline_value: str | None = None
    after_value: str | None = None
    proposed_value: str | None = None
    fraction: float | None = None
    estimate_before_usd: float | None = None
    valued_monthly_usd: float = 0.0


class RealizedSavings(BaseModel):
    """``GET /api/savings-realized`` (and ``/{workspace_id}``).

    * ``unavailable=true``: there is no history to read. That's live mode, or the
      tracker tables don't exist yet. The UI hides the tile.
    * Available with ``saved_usd == 0``: tracking is running but nothing is booked yet.
      The UI shows "$0 · since deployment on <tracking_since>".

    ``saved_usd`` is ``SUM(saved_usd)`` of ``finops_savings_daily`` over
    ``window_start..window_end``. The tracker already applied the headline's
    ``book_scope`` dedup per spend pool and day. ``run_rate_usd`` is the booked monthly
    value on the last day of the window that has data.
    """

    unavailable: bool = False
    #: live_mode | no_tracker_tables (only when unavailable).
    unavailable_reason: str | None = None

    saved_usd: float = 0.0
    run_rate_usd: float = 0.0
    #: Flagged resources with a booked change still in effect at the window end.
    change_count: int = 0

    #: First tracker snapshot day (``MIN(snapshot_date)`` of the ledger).
    tracking_since: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    #: Last day the tracker has booked rows for (the run-rate day).
    data_through: date | None = None

    by_month: list[RealizedMonth] = Field(default_factory=list)
    #: full | partial | deleted | away (moved back since) | <raw change_kind>.
    by_kind: dict[str, RealizedKind] = Field(default_factory=dict)
    #: workspace_retired | workspace_status_unknown | before_first_seen | advisory | ...
    not_counted: dict[str, RealizedNotCounted] = Field(default_factory=dict)
    partial_example: RealizedExample | None = None
