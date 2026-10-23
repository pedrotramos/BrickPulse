"""Platform-admin account overview (leaderboard across all in-scope workspaces)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_recommendation_service, get_view_scope
from ..identity import Scope
from ..models import AccountOverview
from ..services import RecommendationService, aggregation

router = APIRouter(prefix="/api", tags=["account"])


@router.get("/account/overview", response_model=AccountOverview)
def account_overview(
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> AccountOverview:
    recs = service.source.fetch(scope)
    return aggregation.account_overview(recs)
