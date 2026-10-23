"""Workspace spend models — what the platform actually costs.

Separate from the recommendation contract on purpose: recommendations describe
*opportunities* on individual resources, while this describes the **total billed
spend** of a workspace. The two are read together (savings as a share of spend)
but they come from different sources and neither should be derived from the other.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkuSpend(BaseModel):
    """One row of the "where does the money go" breakdown."""

    sku_group: str
    spend_usd: float
    dbus: float = 0.0


class SpendDay(BaseModel):
    """One day of the spend trend.

    A window total says how much; only the series says whether it is getting
    worse. Same shape as ``AppDay``/``GenieDay`` so the frontend can reuse the
    trend chart rather than growing a third variant."""

    usage_date: str
    spend_usd: float
    dbus: float = 0.0


class WorkspaceSpend(BaseModel):
    workspace_id: str
    workspace_name: str | None = None
    lookback_days: int = 30
    total_spend_usd: float = 0.0     # billed spend across the window
    monthly_spend_usd: float = 0.0   # normalized to a 30-day month
    total_dbus: float = 0.0
    serverless_spend_usd: float = 0.0
    classic_spend_usd: float = 0.0
    # Contract / cloud views; None when the account has no such data. The contract
    # figure is BLENDED (contract where priced, list otherwise) and must always be
    # read with contract_priced_share. Cloud VM cost is never added into the above.
    monthly_spend_contract_usd: float | None = None
    contract_priced_share: float | None = None
    monthly_cloud_cost_usd: float | None = None
    by_sku: list[SkuSpend] = Field(default_factory=list)
    #: Day-by-day spend, oldest first. Empty when the snapshot predates the column.
    by_day: list[SpendDay] = Field(default_factory=list)


class AccountSpend(BaseModel):
    """Account-wide spend rollup for the overview KPI."""

    total_monthly_spend_usd: float = 0.0
    total_serverless_spend_usd: float = 0.0
    total_classic_spend_usd: float = 0.0
    # Contract / cloud rollups (None when no workspace carries the data). The contract
    # total is blended; contract_priced_share is its list-$-weighted coverage.
    total_monthly_spend_contract_usd: float | None = None
    contract_priced_share: float | None = None
    total_monthly_cloud_cost_usd: float | None = None
    workspace_count: int = 0
    # Workspaces that billed in the window but sit outside this workspace's region
    # (metastore), so their spend is excluded from the totals above. Account-level
    # context only — 0 for a workspace-scoped user, for whom it would be noise. Lets
    # the UI say how much of the account is not covered rather than hiding it.
    out_of_scope_workspaces: int = 0
    lookback_days: int = 30
    # True when no spend data could be read at all (no snapshot table yet and the
    # live query failed or was not permitted). Lets the UI omit the KPI honestly
    # instead of rendering a confident $0.
    unavailable: bool = False
    workspaces: list[WorkspaceSpend] = Field(default_factory=list)
    by_sku: list[SkuSpend] = Field(default_factory=list)
    #: Account-wide daily spend (every workspace's series summed per day), so the
    #: overview can show one trend line rather than N.
    by_day: list[SpendDay] = Field(default_factory=list)
