"""Resource-grouped view of recommendations.

Several rules can flag the *same* resource with overlapping, often
mutually-exclusive fixes (e.g. a warehouse that could be auto-stopped, moved to
serverless, OR right-sized down a step). Listing each as its own row in the
overview both clutters the table and — critically — makes the headline savings
double-count, because you cannot bank two overlapping fixes on one resource.

A :class:`RecommendationGroup` collapses those into one row per resource. The
overview shows "there is something to do here" (a synthesized action label + a
single, non-double-counted savings figure); the detail page expands the group's
``options`` so the user can compare and pick. This is a pure read-time rollup
over the same :class:`Recommendation` objects — no new persistence.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from .recommendation import Recommendation
from .serverless_fit import ServerlessFit


def group_id(workspace_id: str, resource_type: str, resource_id: str) -> str:
    """Stable id for a resource across snapshots (snapshot-independent) so a
    group deep-link survives the nightly recompute."""
    raw = f"{workspace_id}|{resource_type}|{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class RecommendationGroup(BaseModel):
    group_id: str = Field(..., description="Stable hash of workspace+resource_type+resource_id")
    workspace_id: str
    workspace_name: str | None = None
    workspace_url: str | None = None
    resource_type: str  # cluster | warehouse | job
    resource_id: str
    resource_name: str
    # Short synthesized headline for the overview row ("3 ways to optimise this
    # warehouse" when there are options; the single rule title when there's one).
    action_label: str
    category: str  # representative (top option's) category
    option_count: int
    # The top option's rule, so list rows (which omit ``options``) can still
    # localize a single-option ``action_label``.
    top_rule_id: str | None = None
    # Representative figures. Savings is the MAX of the options, never the sum:
    # the options overlap, so summing would promise money you can only bank once.
    # Waste is likewise the max, i.e. the resource's own monthly spend.
    monthly_savings_usd: float
    monthly_waste_usd: float
    # The naive sum of option savings, kept only so the UI can show *how much*
    # was collapsed / avoid surprising anyone reading the raw findings.
    options_savings_sum_usd: float
    # The same composed savings at "Your rates" (contract where an option has one, list
    # otherwise) and the cloud-VM layer. None without contract / cloud data. price_basis
    # is 'contract' only when every dollar option is contract-priced, 'list' when at
    # least one fell back (the UI marks it), None when there is no contract data.
    monthly_savings_contract_usd: float | None = None
    price_basis: str | None = None
    monthly_cloud_savings_usd: float | None = None
    effort_band: str  # the lowest-effort option (easiest win)
    priority: str  # the highest priority among the options
    nba_score: float  # the top option's rank
    # Per-resource "should this go serverless?" verdict (warehouses/jobs), shown on
    # the detail page. None where serverless doesn't apply to the resource type.
    # See services/serverless_fit.py — computed at build time from the options.
    serverless_fit: ServerlessFit | None = None
    # The individual findings, best-first, for the detail page.
    options: list[Recommendation] = Field(default_factory=list)


class QuickWinsSummary(BaseModel):
    count: int = 0
    value_usd: float = 0.0


class GroupViewCounts(BaseModel):
    """How many resource groups each quick-view preset would show under the current
    filters (every filter except the preset itself), so the chips carry real counts
    without five list fetches."""

    all: int = 0
    quick_wins: int = 0
    booked: int = 0
    high_priority: int = 0
    advisory: int = 0


class RecommendationGroupsSummary(BaseModel):
    """``GET /api/recommendation-groups/summary``: the summary strip of the
    Recommendations page for the same params as the list. Read-time only, never
    persisted, so it is not part of the recommendation schema.

    * ``potential_usd`` is the composed non-advisory figure (one booking per spend
      pool, see services/aggregation._addressable_savings_total), the same formula as
      the Overview / Workspace "Savings potential" tile.
    * ``quick_wins`` mirrors the Overview's Quick wins tile: each resource's best
      counted option, kept when its effort is trivial or low.
    * ``advisory_only`` counts groups whose every option is advisory."""

    resources: int = 0
    potential_usd: float = 0.0
    quick_wins: QuickWinsSummary = Field(default_factory=QuickWinsSummary)
    advisory_only: int = 0
    views: GroupViewCounts = Field(default_factory=GroupViewCounts)
