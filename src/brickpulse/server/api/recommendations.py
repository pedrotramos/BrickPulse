"""Recommendation list + detail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_recommendation_service, get_scope
from ..identity import Scope
from ..models import (
    Recommendation,
    RecommendationGroup,
    RecommendationGroupsSummary,
)
from ..services import RecommendationService

router = APIRouter(prefix="/api", tags=["recommendations"])


@router.get("/recommendations", response_model=list[Recommendation])
def list_recommendations(
    workspace_id: str | None = Query(None),
    rule_id: str | None = Query(None),
    resource_type: str | None = Query(None),
    priority: str | None = Query(None),
    min_savings: float | None = Query(None),
    sort: str = Query("nba", pattern="^(nba|savings)$"),
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> list[Recommendation]:
    return service.list(
        scope,
        workspace_id=workspace_id,
        rule_id=rule_id,
        resource_type=resource_type,
        priority=priority,
        min_savings=min_savings,
        sort=sort,
    )


def _csv(value: str | None) -> list[str] | None:
    """A comma-separated query param as a list (None when absent or blank)."""
    items = [v.strip() for v in value.split(",") if v.strip()] if value else []
    return items or None


# Params shared by the grouped list and its summary. All optional and additive: an
# older client omits them and gets exactly the previous behaviour.
_RULE_IDS_DESC = (
    "Comma-separated rule ids. ALL semantics: keep only resources "
    "where every listed recommendation type applies."
)
_EFFORT_DESC = (
    "Comma-separated effort bands (trivial,low,medium,high). Filters FINDINGS before "
    "grouping, so a group's figures are recomputed on the matching options only."
)
_STATUS_DESC = (
    "Comma-separated savings_status values (bookable,estimated,advisory). Filters "
    "FINDINGS before grouping, like effort."
)
_Q_DESC = (
    "Case-insensitive substring over resource name / id, workspace name and resource "
    "owner."
)
_VIEW_DESC = (
    "Quick-view preset (all, quick_wins, booked, high_priority, advisory), intersected "
    "with the explicit filters. Unknown values are ignored."
)


@router.get("/recommendation-groups", response_model=list[RecommendationGroup])
def list_recommendation_groups(
    workspace_id: str | None = Query(None),
    resource_type: str | None = Query(None),
    priority: str | None = Query(None),
    min_savings: float | None = Query(None),
    rule_ids: str | None = Query(None, description=_RULE_IDS_DESC),
    effort: str | None = Query(None, description=_EFFORT_DESC),
    status: str | None = Query(None, description=_STATUS_DESC),
    q: str | None = Query(None, max_length=200, description=_Q_DESC),
    view: str | None = Query(None, description=_VIEW_DESC),
    sort: str = Query("nba", pattern="^(nba|savings|effort)$"),
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> list[RecommendationGroup]:
    """Overview rollup: one row per resource, collapsing the several rules that
    may flag it into a single group whose savings is the max option (not the
    sum — options overlap). The detail view expands the options."""
    return service.list_grouped(
        scope,
        workspace_id=workspace_id,
        resource_type=resource_type,
        priority=priority,
        min_savings=min_savings,
        rule_ids=_csv(rule_ids),
        effort=_csv(effort),
        status=_csv(status),
        q=q,
        view=view,
        sort=sort,
    )


# Declared before /recommendation-groups/{group_id} so "summary" is never read as an id.
@router.get("/recommendation-groups/summary", response_model=RecommendationGroupsSummary)
def recommendation_groups_summary(
    workspace_id: str | None = Query(None),
    resource_type: str | None = Query(None),
    priority: str | None = Query(None),
    min_savings: float | None = Query(None),
    rule_ids: str | None = Query(None, description=_RULE_IDS_DESC),
    effort: str | None = Query(None, description=_EFFORT_DESC),
    status: str | None = Query(None, description=_STATUS_DESC),
    q: str | None = Query(None, max_length=200, description=_Q_DESC),
    view: str | None = Query(None, description=_VIEW_DESC),
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> RecommendationGroupsSummary:
    """The Recommendations page summary strip for the same params as the grouped
    list: resources, composed potential, quick wins, advisory-only count, and the
    group count each quick-view preset would show under the other filters. (A
    ``sort`` param, if sent, is ignored: totals don't depend on order.)"""
    return service.grouped_summary(
        scope,
        workspace_id=workspace_id,
        resource_type=resource_type,
        priority=priority,
        min_savings=min_savings,
        rule_ids=_csv(rule_ids),
        effort=_csv(effort),
        status=_csv(status),
        q=q,
        view=view,
    )


@router.get("/recommendation-groups/page")
def recommendation_groups_page(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    workspace_id: str | None = Query(None),
    resource_type: str | None = Query(None),
    priority: str | None = Query(None),
    min_savings: float | None = Query(None, ge=0),
    rule_ids: str | None = Query(None),
    effort: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    view: str | None = Query(None),
    sort: str = Query("nba", pattern="^(nba|savings|effort)$"),
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> dict:
    return service.grouped_page(
        scope, page=page, page_size=page_size, workspace_id=workspace_id,
        resource_type=resource_type, priority=priority, min_savings=min_savings,
        rule_ids=_csv(rule_ids), effort=_csv(effort), status=_csv(status), q=q, view=view, sort=sort,
    )


@router.get("/recommendation-groups/{group_id}", response_model=RecommendationGroup)
def get_recommendation_group(
    group_id: str,
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> RecommendationGroup:
    """One resource's findings, within the caller's workspace scope."""
    group = service.get_group(scope, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Recommendation group not found")
    return group


@router.get("/recommendations/{recommendation_id}", response_model=Recommendation)
def get_recommendation(
    recommendation_id: str,
    scope: Scope = Depends(get_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> Recommendation:
    """A finding by recommendation_id or finding_key (always), in scope."""
    rec = service.get(scope, recommendation_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return rec
