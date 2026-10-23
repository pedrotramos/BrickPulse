"""The cost-anomaly model — what the Cost Anomalies tab renders.

Like :mod:`models.apps` and :mod:`models.genie`, this sits deliberately OUTSIDE the
recommendation contract. An anomaly is a cost that moved away from its own recent
normal; whether that is waste, a legitimate backfill or a launch is a judgement only
someone who knows the workload can make. So nothing here is a finding: no savings
figure, no priority, no NBA score, and nothing contributes to any savings total. A
test pins the absence of those fields.

``excess_usd`` is the cost ABOVE the baseline over the flagged days — a size, not a
saving. See :mod:`finops.data.anomaly_sql` for the method.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostAnomaly(BaseModel):
    """One key (a SKU in a workspace, or one resource) whose recent cost broke away
    from its baseline."""

    #: ``sku`` (workspace × product × SKU — covers every dollar) or ``resource``
    #: (job / warehouse / cluster / endpoint / … — the thing to go and look at).
    grain: str
    #: ``spike`` | ``step_change`` | ``new_spender``.
    anomaly_type: str
    anomaly_key: str = ""

    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_url: str | None = None

    product: str | None = None
    sku_name: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None

    first_flag_date: str | None = None
    last_flag_date: str | None = None
    peak_date: str | None = None
    flagged_days: int = 0
    longest_run: int = 0

    peak_cost_usd: float = 0.0
    baseline_median_usd: float = 0.0
    baseline_mad_usd: float = 0.0
    baseline_active_days: int = 0
    #: Robust z-score of the most anomalous day. ``None`` for a new spender — there
    #: is no baseline to be N sigmas above.
    robust_z: float | None = None
    #: Cost above the baseline median, summed over the flagged days (for a new
    #: spender, their whole cost). A size, NOT a saving.
    excess_usd: float = 0.0
    #: The $/day floor this key had to clear (max of the absolute floor and the
    #: workspace-relative one).
    floor_usd: float = 0.0
    eval_cost_usd: float = 0.0
    #: Daily cost over baseline + evaluation window, oldest first.
    series: list[float] = Field(default_factory=list)

    #: The identity behind the largest usage record on the peak day.
    run_as: str | None = None
    owned_by: str | None = None


class AnomalyDay(BaseModel):
    """One day of total cost in scope — the trend the anomalies sit on."""

    usage_date: str
    cost_usd: float = 0.0


class AnomalyTypeCount(BaseModel):
    anomaly_type: str
    count: int = 0
    excess_usd: float = 0.0


class CostAnomalies(BaseModel):
    """Everything the Cost Anomalies tab renders."""

    baseline_days: int = 28
    eval_days: int = 7
    z_threshold: float = 4.0
    min_delta_usd: float = 200.0
    min_delta_share: float = 0.03
    #: First / last date of the evaluation window actually scored.
    eval_start: str | None = None
    eval_end: str | None = None

    #: Excess above baseline across SKU-grain anomalies. The SKU grain covers every
    #: dollar exactly once, so this is the one total that doesn't double count —
    #: resource-grain anomalies overlap it.
    total_excess_usd: float = 0.0
    anomaly_count: int = 0
    workspace_count: int = 0
    by_type: list[AnomalyTypeCount] = Field(default_factory=list)

    #: True when no anomaly data could be read at all — the UI omits figures rather
    #: than showing a confident "no anomalies".
    unavailable: bool = False
    #: Readable, and nothing broke away from its baseline.
    no_anomalies: bool = False
    missing_sections: list[str] = Field(default_factory=list)

    resource_anomalies: list[CostAnomaly] = Field(default_factory=list)
    sku_anomalies: list[CostAnomaly] = Field(default_factory=list)
    daily: list[AnomalyDay] = Field(default_factory=list)

    #: Why this tab shows no savings. Rendered verbatim.
    analysis_note: str = ""
    #: How an anomaly is detected, in plain language. Rendered verbatim.
    method_note: str = ""
    data_basis: str = ""
