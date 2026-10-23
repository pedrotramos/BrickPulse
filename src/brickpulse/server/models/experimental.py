"""Response model for the Labs (Experimental) tab.

Net-new opportunity rules are still being validated, so they are RELOCATED out of the main
recommendation surfaces — they never count toward the headline, never rank in Next
Best Actions, and never appear as an option on an optimizable resource. This payload
backs their own tab. Their headline is ADDRESSABLE monthly spend (the compute the
finding identifies as potentially avoidable), never a booked saving: experimental
findings are advisory, so ``monthly_savings_usd`` is $0 by design.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .recommendation import Recommendation


class ExperimentalOverview(BaseModel):
    #: The addressable monthly spend the experimental findings flag (``monthly_waste_usd``
    #: — the compute flagged as potentially avoidable), each spend pool counted ONCE
    #: (``aggregation.waste_total``): two rules on one cluster don't double its spend.
    #: Shown in place of a savings total, because these book $0.
    total_addressable_monthly_usd: float
    #: Number of experimental findings shown on the tab. Deliberately NOT added to
    #: the main recommendation total (see data/base.py).
    recommendation_count: int
    #: Distinct resources those findings are on (several rules can flag one resource).
    #: Additive; defaults to 0 for an older client/server pairing.
    resource_count: int = 0
    #: Distinct experimental rules that produced at least one finding.
    rule_count: int
    #: Distinct workspaces with at least one experimental finding.
    workspace_count: int
    items: list[Recommendation] = Field(default_factory=list)
