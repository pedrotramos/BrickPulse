"""Next-Best-Action endpoint: top recommendations by savings-vs-effort score."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..deps import get_recommendation_service, get_view_scope
from ..identity import Scope
from ..models import Recommendation
from ..services import RecommendationService

router = APIRouter(prefix="/api", tags=["nba"])


@router.get("/next-best-actions", response_model=list[Recommendation])
def next_best_actions(
    limit: int = Query(10, ge=1, le=100),
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> list[Recommendation]:
    return service.next_best_actions(scope, limit=limit)
