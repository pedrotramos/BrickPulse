"""Price-basis capability summary — which price views this snapshot can show.

Reports contract-rate and cloud-VM coverage for the data actually served.
The UI keeps list-price KPIs as baseline and labels contract figures separately.
"""

from __future__ import annotations

from pydantic import BaseModel


class PricingSummary(BaseModel):
    #: True when the served findings contain contract coverage observations.
    contract_available: bool = False
    #: Account-level contract coverage, 0–1: the list-$ share of total spend that had
    #: a contract price (from the spend snapshot), else the share of findings' list
    #: savings that carry a contract figure. None when contract data is absent.
    contract_coverage_account: float | None = None
    #: Findings carrying / lacking a contract figure (price_basis contract / list).
    contract_findings: int = 0
    list_fallback_findings: int = 0
    #: True when any finding carries a classic cloud-VM cost (cloud_infra_cost).
    cloud_available: bool = False
    #: Classic findings with cloud cost data vs all findings of classic resource types.
    cloud_findings: int = 0
    cloud_currency: str = "USD"
    #: "contract" when coverage ≥ 95 % (decision D1), else "list".
    default_basis: str = "list"
