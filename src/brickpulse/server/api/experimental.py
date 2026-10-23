"""Labs (Experimental) tab: Net-new opportunity findings, served on their own.

These rules (``sql_repeated_failures``, ``manual_maintenance_overlap`` today) are
still being validated, so they are relocated out of the main recommendation surfaces
— they contribute nothing to the headline total, don't appear as an option on an
optimizable resource, and don't enter the Next Best Actions ranking or the savings
leaderboard. This endpoint is the ONLY read path that surfaces them (via
``fetch_experimental``); see ``Rule.experimental``, ``config.Settings.experimental_rule_ids``
and ``data/base.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_recommendation_service, get_view_scope
from ..identity import Scope
from ..models import ExperimentalOverview
from ..services import RecommendationService

router = APIRouter(prefix="/api", tags=["experimental"])


@router.get("/experimental", response_model=ExperimentalOverview)
def experimental_overview(
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> ExperimentalOverview:
    # get_view_scope honours the global workspace switcher (?workspace_id=), so the
    # Labs tab follows the same scope as every other content view. Account/cloud/
    # account-id filters are global and applied at the source's fetch boundary.
    return service.experimental_overview(scope)
