"""Price-basis capabilities for explicitly labelled contract and cloud figures."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..data import SpendSource
from ..deps import get_recommendation_service, get_spend_source, get_view_scope
from ..identity import Scope
from ..models.pricing import PricingSummary
from ..services import RecommendationService
from ..services.pricing_summary import pricing_summary

router = APIRouter(prefix="/api", tags=["pricing"])


@router.get("/pricing", response_model=PricingSummary)
def pricing(
    scope: Scope = Depends(get_view_scope),
    service: RecommendationService = Depends(get_recommendation_service),
    spend: SpendSource = Depends(get_spend_source),
) -> PricingSummary:
    """Derived from the findings actually served. Spend coverage is the account-level
    share; spend failures just fall back to the findings-derived share."""
    recs = service.source.fetch(scope)
    share = None
    if any(r.price_basis for r in recs):
        try:
            share = spend.account_spend(scope).contract_priced_share
        except Exception:
            share = None
    return pricing_summary(recs, spend_contract_share=share, currency=get_settings().currency)
