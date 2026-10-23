"""Next-Best-Action ranking + conversion of rule findings into recommendations.

NBA score = ``monthly_savings_usd / effort_score`` scaled by a priority
multiplier, so "big savings, low effort" surfaces first. Weights are config
(``effort.yaml`` + the multipliers below) so the tradeoff can be re-tuned
without code changes; the score is stored so live and precompute rank
identically.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from brickpulse.rules import EffortBand, Rule, RuleFinding
from brickpulse.rules.pricing import effort_score as _effort_score

from ..models import Recommendation
from ..i18n import message_refs

# Priority weighting applied on top of savings-per-effort.
PRIORITY_MULTIPLIER: dict[str, float] = {"HIGH": 1.25, "MEDIUM": 1.0, "LOW": 0.8}


def compute_nba(monthly_savings_usd: float, effort_score: int, priority: str) -> float:
    if effort_score <= 0:
        effort_score = 1
    base = monthly_savings_usd / effort_score
    return round(base * PRIORITY_MULTIPLIER.get(priority, 1.0), 4)


def recommendation_id(rule_id: str, workspace_id: str, resource_id: str, snapshot: date) -> str:
    raw = f"{rule_id}|{workspace_id}|{resource_id}|{snapshot.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def build_recommendation(
    rule: Rule,
    finding: RuleFinding,
    *,
    snapshot: date | None = None,
    effort_overrides: dict[str, int] | None = None,
    workspace_name: str | None = None,
    workspace_url: str | None = None,
    resource_owner: str | None = None,
    resource_creator: str | None = None,
) -> Recommendation:
    snapshot = snapshot or date.today()
    escore = _effort_score(EffortBand(finding.effort), effort_overrides)
    nba = compute_nba(finding.monthly_savings_usd, escore, finding.priority.value)
    # Savings-status scaffold: resolve the finding's status/bounds. A per-finding
    # value wins; otherwise the rule's default_savings_status (itself "bookable" until
    #). The low/high bounds default to the point estimate, so the
    # booked headline (bookable low only) is unchanged until rules are reclassified.
    point = finding.monthly_savings_usd
    savings_status = finding.savings_status or getattr(rule, "default_savings_status", "bookable")
    savings_low = finding.monthly_savings_low_usd if finding.monthly_savings_low_usd is not None else point
    savings_high = finding.monthly_savings_high_usd if finding.monthly_savings_high_usd is not None else point
    # resolve how this finding aggregates against other findings on the same
    # scope. A per-finding value wins; otherwise the rule default ("exclusive").
    savings_composition = finding.savings_composition or getattr(
        rule, "default_savings_composition", "exclusive"
    )
    return Recommendation(
        recommendation_id=recommendation_id(
            rule.id, finding.workspace_id, finding.resource_id, snapshot
        ),
        snapshot_date=snapshot,
        rule_id=rule.id,
        rule_version=rule.version,
        rule_title=rule.title,
        category=rule.category,
        workspace_id=finding.workspace_id,
        workspace_name=workspace_name,
        workspace_url=workspace_url,
        resource_type=finding.resource_type,
        resource_id=finding.resource_id,
        resource_name=finding.resource_name,
        # A rule that read ownership itself wins; otherwise the billing-derived
        # owner_map value passed in. Either may be None ("owner unknown").
        resource_owner=finding.resource_owner or resource_owner,
        resource_creator=finding.resource_creator or resource_creator,
        why=finding.why,
        how=finding.how,
        monthly_waste_usd=finding.monthly_waste_usd,
        monthly_savings_usd=finding.monthly_savings_usd,
        savings_status=savings_status,
        monthly_savings_low_usd=savings_low,
        monthly_savings_high_usd=savings_high,
        affected_spend_scope=finding.affected_spend_scope,
        savings_composition=savings_composition,
        price_basis=getattr(finding, "price_basis", None),
        contract_price_coverage=getattr(finding, "contract_price_coverage", None),
        monthly_savings_contract_usd=getattr(finding, "monthly_savings_contract_usd", None),
        monthly_savings_contract_low_usd=getattr(finding, "monthly_savings_contract_low_usd", None),
        monthly_savings_contract_high_usd=getattr(finding, "monthly_savings_contract_high_usd", None),
        cloud_cost_30d_usd=getattr(finding, "cloud_cost_30d_usd", None),
        monthly_cloud_savings_usd=getattr(finding, "monthly_cloud_savings_usd", None),
        effort_band=finding.effort.value,
        effort_score=escore,
        priority=finding.priority.value,
        nba_score=nba,
        evidence={**finding.evidence, "messages": message_refs(rule.id, finding.evidence)},
        computed_at=datetime.now(timezone.utc),
    )
