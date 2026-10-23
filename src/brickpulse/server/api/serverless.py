"""The "About Serverless" endpoint — the platform-admin case for moving.

Reads the recommendation source **before** the hidden-rule filter (via
``source.load``): the old-DBR findings are hidden from the recommendation GUI but
are precisely the evidence this page is built on. See
``services.serverless_case`` for the reasoning.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..data import SpendSource
from ..data.fleet_source import FleetSource
from ..deps import (
    get_fleet_source,
    get_recommendation_service,
    get_spend_source,
    get_view_scope,
)
from ..identity import Scope
from ..models.serverless import ServerlessCase
from ..services import RecommendationService, serverless_case

router = APIRouter(prefix="/api", tags=["serverless"])


@router.get("/serverless-case", response_model=ServerlessCase)
def get_serverless_case(
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
    spend_source: SpendSource = Depends(get_spend_source),
    fleet_source: FleetSource = Depends(get_fleet_source),
) -> ServerlessCase:
    # `load` (not `fetch`) so hidden-but-computed rules still inform the case.
    recs = service.source.load(scope)
    spend = spend_source.account_spend(scope)
    fleet = fleet_source.fleet_facts(scope)
    return serverless_case.build_case(
        recs,
        spend=spend,
        fleet=fleet,
        lookback_days=fleet.get("lookback_days", 30),
    )
