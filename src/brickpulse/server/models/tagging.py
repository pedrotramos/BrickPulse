"""Response model for the Tagging & Chargeback tab.

Governance-category findings (untagged_spend today) are relocated out of the main
recommendation surfaces — they carry $0 savings by design and measure
*unattributable* spend instead, so ranking them next to real-dollar fixes created
sprawl. This payload backs their own tab: the headline is the size of the
governance gap (spend that cannot be charged back), not a savings figure.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .recommendation import Recommendation


class TaggingOverview(BaseModel):
    #: Sum of unattributable monthly spend across the findings (each resource's
    #: own billed spend that has no owner/cost-centre tag). This is the metric the
    #: tab leads with, in place of a savings total.
    total_unattributable_monthly_usd: float
    #: Number of findings shown on the tab. Deliberately NOT added to the main
    #: recommendation total — that count excludes this category (see data/base.py).
    recommendation_count: int
    #: Distinct workspaces with at least one untagged-spend finding.
    workspace_count: int
    items: list[Recommendation] = Field(default_factory=list)
