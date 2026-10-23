"""Rule: provisioned-throughput serving capacity held through hours of no traffic.
A provisioned-throughput (PT) endpoint bills its reserved capacity every hour,
whether or not a request arrives. This rule finds PT endpoints that DO serve traffic
(so they are not the idle case) but sit through long stretches of billed hours with
zero logged requests. That capacity is what a lower floor or scale-to-zero would not
pay for.

**Scope is narrower than the plan's two findings, on purpose.** The live account
showed two gaps:

* The provisioned capacity is not in system tables for current PT endpoints.
  ``served_entities.foundation_model_config`` is NULL on every billing PT
  endpoint, because they are sized in model units. So "p99 throughput far below the
  floor" cannot be computed. This rule prices only the hours with no traffic at
  all, which makes it a lower bound on overcapacity.
* Pay-per-token and PT bill the same DBU SKU, and the per-model DBU-per-token rate
  is in no system table. So "switch to pay-per-token" cannot be priced honestly
  and is not emitted.

**No overlap with ``idle_serving_endpoints``.** An endpoint with zero logged
requests is that rule's zero-traffic case (or simply has usage tracking off), so
``score()`` suppresses it here. The two rules share ``resource_type`` so a
resource's findings group together.

**Estimated saving.** The figure is the PT spend billed through long no-request
stretches — what scale-to-zero reclaims. Doing so trades a minutes-long cold start for
it, and request logs sampled per hour can't prove the capacity was unneeded, so it is
``estimated`` (shown and ranked), not booked. Promoted out of Labs 2026-09-23.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block, viz_serving_traffic
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.billing.usage (product_features.model_serving.offering_type = "
    "'PROVISIONED_THROUGHPUT', priced per billed hour x list_prices) + "
    "system.serving.endpoint_usage (per-request log, via serving.served_entities), "
    "{n}-day lookback. Counts only runs of >= {run}h consecutive billed hours with "
    "zero logged requests, and only after the first logged request (so usage tracking "
    "is known to be on). The reserved capacity itself (model units) is not in system "
    "tables, so overcapacity during busy hours is NOT measured and the figure is a "
    "lower bound. Pay-per-token is not priced as an alternative: it bills the same "
    "DBU SKU at a model-specific rate that no system table carries."
)


def _normalize_days(days) -> list[dict]:
    """Coerce the SQL's per-day series (Spark Rows or dicts) into plain dicts."""
    out = []
    for entry in days or []:
        if entry is None:
            continue
        row = entry.asDict() if hasattr(entry, "asDict") else dict(entry)
        if row.get("d") is None:
            continue
        out.append({"d": str(row["d"]), "w": int(row.get("w") or 0), "r": int(row.get("r") or 0)})
    return sorted(out, key=lambda x: x["d"])


@register
class ServingProvisionedOvercapacityRule(Rule):
    id = "serving_provisioned_overcapacity"
    # Estimated: idle PT hours are measured; the cold-start trade-off is not.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "Provisioned-throughput capacity held through hours with no traffic"
    resource_type = "serving_endpoint"
    category = "serving"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    sql_file = "serving_provisioned_overcapacity.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        # Without a matched entity row there is no request data for this endpoint;
        # absence of data is not absence of traffic.
        if not int(row.get("entity_matched") or 0):
            return None
        # Zero logged requests: either truly idle (idle_serving_endpoints' case) or
        # usage tracking is off. Either way it's not this rule's finding.
        requests = int(row.get("requests_window") or 0)
        if requests <= int(t.get("max_requests_defer_to_idle", 0)):
            return None
        days_billed = int(row.get("days_billed") or 0)
        if days_billed < int(t.get("min_days_billed", 7)):
            return None  # not a standing reservation

        cost_window = float(row.get("cost_30d_usd") or 0.0)
        reclaim_window = float(row.get("reclaimable_cost_usd") or 0.0)
        if cost_window <= 0 or reclaim_window <= 0:
            return None
        share = max(0.0, min(1.0, reclaim_window / cost_window))
        if share < float(t.get("min_idle_cost_share", 0.3)):
            return None

        monthly_cost = to_monthly(cost_window, ctx.lookback_days)
        addressable = min(to_monthly(reclaim_window, ctx.lookback_days), monthly_cost)
        if addressable < float(t.get("min_addressable_usd", 50)):
            return None

        billed_hours = int(row.get("billed_hours") or 0)
        active_hours = int(row.get("active_hours") or 0)
        reclaim_hours = int(row.get("reclaimable_idle_hours") or 0)
        longest = int(row.get("longest_idle_run_hours") or 0)
        run_h = int(t.get("min_idle_run_hours", 3))
        name = row.get("endpoint_name") or str(row["endpoint_id"])
        entity = row.get("entity_name") or row.get("entity_type") or "model"

        if addressable >= 2000 or share >= 0.8:
            priority = RulePriority.HIGH
        elif addressable >= 500 or share >= 0.5:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        why = (
            f"Provisioned-throughput endpoint '{name}' (serving {entity}) billed "
            f"reserved capacity for {billed_hours:,} hours over the last "
            f"{ctx.lookback_days} days (~${monthly_cost:,.0f}/mo), but requests arrived "
            f"in only {active_hours:,} of them ({requests:,} requests in total). "
            f"{reclaim_hours:,} billed hours fell in stretches of {run_h}+ hours with no "
            f"request at all (longest: {longest:,}h). That is {share * 100:.0f}% of "
            f"the PT spend, ~${addressable:,.0f}/mo. It's a lower bound: capacity "
            f"above what the busy hours needed is not measurable from system tables."
        )
        how = (
            f"Check when '{name}' is actually called (Serving > {name} > Metrics). If "
            f"traffic has clear quiet periods, lower the provisioned floor or enable "
            f"scale-to-zero, or stop the endpoint outside working hours from a "
            f"scheduled job. PT scale-up takes minutes, so keep a warm floor if callers "
            f"can't tolerate a cold start. At low, sporadic volume, also compare the "
            f"pay-per-token price for this model: it bills only per request. The "
            f"saving is an estimate until you've confirmed the quiet hours are real."
        )

        viz = viz_serving_traffic(
            days=_normalize_days(row.get("days_series")),
            lookback_days=ctx.lookback_days,
            days_billed=days_billed,
            total_requests=requests,
            last_request=str(row.get("last_request_hour")) if row.get("last_request_hour") else None,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="serving_endpoint",
            resource_id=str(row["endpoint_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked.
            monthly_savings_usd=round(addressable, 2),
            savings_status="estimated",
            monthly_savings_low_usd=round(addressable, 2),
            effort=EffortBand.MEDIUM,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days, run=run_h)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                provisioned_throughput=True,
                entity_type=row.get("entity_type"),
                entity_name=row.get("entity_name"),
                created_by=row.get("created_by"),
                billed_hours=billed_hours,
                active_hours=active_hours,
                idle_hours=int(row.get("idle_hours") or 0),
                reclaimable_idle_hours=reclaim_hours,
                longest_idle_run_hours=longest,
                idle_cost_share=round(share, 4),
                requests_window=requests,
                tokens_window=row.get("tokens_window"),
                peak_hour_tokens=row.get("peak_hour_tokens"),
                days_billed=days_billed,
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
                addressable_monthly_usd=round(addressable, 2),
            ),
        )
