"""Roll-up models for the platform-admin overview and workspace drill-down."""

from __future__ import annotations

from pydantic import BaseModel


class CategoryBreakdown(BaseModel):
    category: str
    monthly_savings_usd: float
    recommendation_count: int


class SavingsTier(BaseModel):
    """One slice of the headline estimate by trust tier — ``booked`` (a bookable
    finding, banked regardless of its data-confidence label) or an estimated finding's
    ``high`` / ``medium`` / ``low`` confidence. Composed per spend scope so the tiers sum
    to ``total_monthly_savings_addressable_usd`` (no dollar counted twice). See
    services/aggregation.book_scope_breakdown."""
    tier: str  # booked | high | medium | low
    monthly_savings_usd: float


class SavingsSlice(BaseModel):
    """One slice of the headline estimate by rule or category, composed per spend scope
    (services/aggregation._savings_breakdown) so a breakdown's slices sum to
    ``total_monthly_savings_addressable_usd``. ``key`` is the rule id or category."""
    key: str
    monthly_savings_usd: float
    resource_count: int


class PricedTotals(BaseModel):
    """The headline at "Your rates" plus the separate cloud-VM layer. Same per-spend-scope composition
    as the list headline, with each finding valued at its contract figure where it
    has one and at list otherwise — so the totals are BLENDED and always shipped with
    their split and counts, never as a bare number. None on the parent when the
    snapshot carries no contract / cloud data at all."""

    contract_available: bool = False
    # Blended equivalents of the list headline fields.
    monthly_savings_usd: float = 0.0              # booked lower bound
    monthly_savings_addressable_usd: float = 0.0  # the headline estimate
    monthly_savings_low_usd: float = 0.0
    monthly_savings_high_usd: float = 0.0
    # Split of the addressable estimate: at contract rates vs still at list.
    contract_part_usd: float = 0.0
    list_part_usd: float = 0.0
    contract_findings: int = 0
    list_findings: int = 0
    # Cloud VM layer (classic compute; cloud_infra_cost) — never added to the above.
    cloud_available: bool = False
    monthly_cloud_savings_usd: float = 0.0
    cloud_findings: int = 0        # findings with cloud cost data
    classic_findings: int = 0      # findings whose rule can save cloud VM cost


class WorkspaceSummary(BaseModel):
    workspace_id: str
    workspace_name: str | None = None
    monthly_savings_usd: float  # booked lower bound (bookable findings' low)
    # The fuller estimate (booked + estimated high/medium/low-confidence) for this
    # workspace; >= monthly_savings_usd. The leaderboard ranks and shows THIS so a
    # workspace whose opportunity is mostly estimated isn't buried under the booked
    # floor. Defaults to the booked value for older snapshots.
    monthly_savings_addressable_usd: float = 0.0
    monthly_waste_usd: float
    recommendation_count: int
    top_rule_id: str | None = None
    top_category: str | None = None
    # The addressable estimate at "Your rates" (blended); None without contract data.
    monthly_savings_addressable_contract_usd: float | None = None


class AccountOverview(BaseModel):
    # Booked lower bound: sum of bookable findings' conservative low.
    total_monthly_savings_usd: float
    # The fuller, ESTIMATED opportunity of which the booked total is the floor:
    # per-resource best point estimate across non-advisory findings. >= booked.
    # Defaults to the booked total for backward compatibility. This is the headline
    # figure the KPI now leads with (booked + estimated), with the range below.
    total_monthly_savings_addressable_usd: float = 0.0
    # The (low, high) band around that estimate — composed conservative low and
    # upper-scenario high, non-advisory. The estimate point sits inside this band.
    total_monthly_savings_low_usd: float = 0.0
    total_monthly_savings_high_usd: float = 0.0
    # The estimate split by trust tier (booked / high / medium / low), summing to the
    # addressable estimate. Empty on older snapshots (the KPI falls back to booked).
    savings_tiers: list[SavingsTier] = []
    # The same estimate split by category and by rule (the biggest levers), composed
    # like the tiers. Empty on older snapshots.
    savings_by_category: list[SavingsSlice] = []
    savings_by_rule: list[SavingsSlice] = []
    total_monthly_waste_usd: float
    recommendation_count: int
    workspace_count: int
    workspaces: list[WorkspaceSummary]
    priced: PricedTotals | None = None


class WorkspaceDetail(BaseModel):
    workspace_id: str
    workspace_name: str | None = None
    monthly_savings_usd: float
    # Estimated (addressable) opportunity for this workspace; >= monthly_savings_usd.
    monthly_savings_addressable_usd: float = 0.0
    # (low, high) band around that estimate + its per-tier split (see AccountOverview).
    monthly_savings_low_usd: float = 0.0
    monthly_savings_high_usd: float = 0.0
    savings_tiers: list[SavingsTier] = []
    monthly_waste_usd: float
    recommendation_count: int
    by_category: list[CategoryBreakdown]
    priced: PricedTotals | None = None
