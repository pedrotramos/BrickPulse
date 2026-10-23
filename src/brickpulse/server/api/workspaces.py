"""Workspace list + per-workspace drill-down."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_recommendation_service, get_scope
from ..identity import Scope
from ..models import WorkspaceDetail, WorkspaceSummary
from ..services import RecommendationService, aggregation

router = APIRouter(prefix="/api", tags=["workspaces"])


@router.get("/workspaces", response_model=list[WorkspaceSummary])
def list_workspaces(
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> list[WorkspaceSummary]:
    recs = service.source.fetch(scope)
    return aggregation.account_overview(recs).workspaces


@router.get("/workspaces/{workspace_id}/summary", response_model=WorkspaceDetail)
def workspace_summary(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> WorkspaceDetail:
    recs = service.source.fetch(scope)
    return aggregation.workspace_detail(workspace_id, recs)
