"""The recommendation schema — the stability contract.

This pydantic model is the single Python source of truth for a recommendation.
It MUST stay in sync (additively only) with:
  * precompute/ddl/finops_recommendations.sql  (the Delta table)
  * frontend/src/api/types.ts                  (the TypeScript mirror)
See docs/DATA_MODEL.md.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, computed_field, model_validator


class Recommendation(BaseModel):
    recommendation_id: str = Field(..., description="Stable hash of rule+workspace+resource+snapshot")
    snapshot_date: date
    rule_id: str
    rule_version: str
    rule_title: str
    category: str  # compute | sql | jobs
    workspace_id: str
    workspace_name: str | None = None
    # Base URL of the workspace the resource lives in (e.g.
    # https://my-workspace.cloud.databricks.com), from system.access.workspaces_latest.
    # The deep link MUST use this host — the account spans many workspaces, each on
    # its own host, so the app's own host is only correct for its own workspace.
    workspace_url: str | None = None
    resource_type: str  # cluster | warehouse | job
    resource_id: str
    resource_name: str
    # Who owns / created this resource, resolved at precompute time from
    # billing.usage.identity_metadata (see precompute owner_map). Both optional:
    # some resource types carry no identity on their usage rows, and an unknown
    # owner never suppresses the finding — it renders as "owner unknown".
    resource_owner: str | None = None
    resource_creator: str | None = None
    why: str
    how: str
    monthly_waste_usd: float
    monthly_savings_usd: float  # point estimate (kept for compatibility)
    # Savings-status scaffold.
    # savings_status is bookable | estimated | advisory. The headline books ONLY
    # monthly_savings_low_usd, and only for `bookable` findings; estimated/advisory
    # contribute zero to the booked total but stay visible and rankable. The low/high
    # bounds default to the point estimate (see the validator) so `low <= point <=
    # high` holds and, with everything defaulting to bookable, the headline is
    # unchanged until specific rules are reclassified.
    savings_status: str = "bookable"
    monthly_savings_low_usd: float | None = None   # conservative floor — the only value booked
    monthly_savings_high_usd: float | None = None  # upper scenario, never booked
    # action-aware aggregation. affected_spend_scope is the spend POOL this
    # action reclaims from — findings sharing a scope are deduped to one bankable figure so
    # the same dollar is never booked twice; None falls back to this resource's own
    # identity (the historical per-resource basis). savings_composition is
    # exclusive|additive: exclusive options are mutually-exclusive whole-resource
    # alternatives (a migration supersedes source tuning), additive options compose onto
    # the remaining counterfactual (auto-stop + right-size on one warehouse). Defaults keep
    # every rule at max-per-resource until it opts in. See services/aggregation.py.
    affected_spend_scope: str | None = None
    savings_composition: str = "exclusive"
    # Contract rates + cloud VM cost (finops_rules.price_basis; docs/DATA_MODEL.md).
    # All None unless the account has system.billing.account_prices /
    # cloud_infra_cost data. The list figures above are ALWAYS complete and remain the
    # ranking basis; a contract figure exists only when >= contract_min_coverage of the
    # resource's usage had a contract price (price_basis='contract'). Cloud VM savings
    # are a separate layer, never added into the Databricks dollars.
    price_basis: str | None = None  # list | contract
    contract_price_coverage: float | None = None
    monthly_savings_contract_usd: float | None = None
    monthly_savings_contract_low_usd: float | None = None
    monthly_savings_contract_high_usd: float | None = None
    cloud_cost_30d_usd: float | None = None
    monthly_cloud_savings_usd: float | None = None
    # Snapshot-independent finding identifier (a link from yesterday still resolves).
    finding_key: str | None = None
    effort_band: str  # trivial | low | medium | high
    effort_score: int
    priority: str  # HIGH | MEDIUM | LOW
    nba_score: float  # Next-Best-Action rank (savings per unit effort)
    evidence: dict = Field(default_factory=dict)
    computed_at: datetime

    @model_validator(mode="after")
    def _default_savings_bounds(self) -> "Recommendation":
        """Fill the low/high bounds from the point estimate when a producer left
        them unset (the common case today) — so downstream aggregation always sees
        real numbers and ``low <= point <= high`` holds. Also protects the read path
        for rows written before these columns existed (they arrive as None)."""
        if self.monthly_savings_low_usd is None:
            self.monthly_savings_low_usd = self.monthly_savings_usd
        if self.monthly_savings_high_usd is None:
            self.monthly_savings_high_usd = self.monthly_savings_usd
        return self

    @model_validator(mode="after")
    def _default_finding_key(self) -> "Recommendation":
        """Derive finding_key when it wasn't supplied.

        The finding_key is the snapshot-independent id of this rule's finding on this
        resource; links built from it survive the nightly recompute."""
        if not self.finding_key:
            from ..finding_key import finding_key

            self.finding_key = finding_key(
                self.rule_id, self.workspace_id, self.resource_type, self.resource_id
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def group_id(self) -> str:
        """Stable id of the RESOURCE this finding belongs to.

        Several rules routinely flag the same resource, and the UI shows them
        together on one resource page — so every finding needs to name its group.
        Derived (never stored) from workspace+type+resource, identical to
        ``models.group.group_id``; a computed field so it lands in the serialized
        payload and the frontend can link to the grouped view without
        re-implementing the hash."""
        from .group import group_id

        return group_id(self.workspace_id, self.resource_type, self.resource_id)
