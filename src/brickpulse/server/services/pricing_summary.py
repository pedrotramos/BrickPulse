"""Summarize which price bases a set of recommendations can be shown in."""

from __future__ import annotations

from brickpulse.rules.price_basis import PRICE_BASIS_CONTRACT, PRICE_BASIS_LIST

from ..models import Recommendation
from ..models.pricing import PricingSummary

#: Decision D1 (2026-09-23): default to "Your rates" only when at least this share of
#: the account's spend is contract-priced; otherwise the blended total would lean on
#: list prices more than the label suggests.
DEFAULT_CONTRACT_BASIS_MIN_COVERAGE = 0.95


def pricing_summary(
    recs: list[Recommendation],
    *,
    spend_contract_share: float | None = None,
    currency: str = "USD",
) -> PricingSummary:
    contract = [r for r in recs if r.price_basis == PRICE_BASIS_CONTRACT]
    fallback = [r for r in recs if r.price_basis == PRICE_BASIS_LIST]
    available = bool(contract or fallback)
    coverage: float | None = None
    if available:
        if spend_contract_share is not None:
            coverage = round(float(spend_contract_share), 4)
        else:
            total = sum(r.monthly_savings_usd for r in contract + fallback)
            covered = sum(r.monthly_savings_usd for r in contract)
            coverage = round(covered / total, 4) if total > 0 else (1.0 if contract else 0.0)
    cloud = [r for r in recs if r.cloud_cost_30d_usd is not None]
    return PricingSummary(
        contract_available=available,
        contract_coverage_account=coverage,
        contract_findings=len(contract),
        list_fallback_findings=len(fallback),
        cloud_available=bool(cloud),
        cloud_findings=len(cloud),
        cloud_currency=currency,
        default_basis=(
            "contract"
            if available and coverage is not None and coverage >= DEFAULT_CONTRACT_BASIS_MIN_COVERAGE
            else "list"
        ),
    )
