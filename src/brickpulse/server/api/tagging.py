"""Tagging & Chargeback tab: governance-category findings, served on their own.

These findings (untagged_spend today) are relocated out of the main recommendation
surfaces — they carry $0 savings and measure unattributable spend instead, so they
do not count toward the recommendation total, do not appear as an option on an
optimizable resource, and do not enter the Next Best Actions ranking. This endpoint
is the ONLY read path that surfaces them (via ``fetch_sidecar``); see
``config.Settings.sidecar_category_set`` and ``data/base.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_recommendation_service, get_view_scope
from ..identity import Scope
from ..models import TaggingOverview
from ..services import RecommendationService

router = APIRouter(prefix="/api", tags=["tagging"])


@router.get("/tagging", response_model=TaggingOverview)
def tagging_overview(
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
) -> TaggingOverview:
    # get_view_scope honours the global workspace switcher (?workspace_id=), so the
    # tab follows the same scope as every other content view. Account/cloud/account-id
    # filters are global and applied at the source's fetch boundary.
    return service.tagging_overview(scope)
