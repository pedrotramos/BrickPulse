"""Rule: move on-demand classic clusters to SPOT_WITH_FALLBACK.

Classic (customer-managed) clusters running purely on-demand workers pay the
full VM price. SPOT_WITH_FALLBACK runs workers on spot capacity (much cheaper)
and only falls back to on-demand when spot is unavailable, so the underlying VM
spend drops toward the spot price with a graceful safety net.

Savings model:

The spot/on-demand discount is a CLOUD VM concept. Databricks ``billing.usage``
meters DBUs — the Databricks software fee — and the DBU rate is IDENTICAL whether
the underlying worker runs on spot or on-demand; the VM cost that spot actually
reduces is billed separately by the cloud (AWS CUR / Azure / GCP export), which
this app does not have. So a spot saving cannot be quantified from Databricks data:
toggling to spot leaves the metered DBUs unchanged, hence **$0 measurable savings**.
The rule therefore surfaces the opportunity as **advisory $0** — which on-demand
clusters are candidates, and the qualitative next step — and books nothing. A dollar
figure needs the cloud VM bill (node-minutes x real spot/on-demand rates, excluding
the driver, net of eviction-replay), which belongs in a future cloud-billing
integration, not a DBU-derived guess.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import (
    coverage_evidence,
    coverage_note,
    underpriced,
)
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.compute.clusters (availability + worker count + cloud) + billing.usage x "
    "list_prices, {n}-day lookback. The cluster's DBU spend is shown for context only: "
    "Databricks meters DBUs, and the DBU rate is the SAME on spot and on-demand, so the "
    "spot saving lives entirely in the cloud VM bill (AWS CUR / Azure / GCP export) that "
    "this app does not have. No dollar saving is bookable from Databricks data "
    "— this is an advisory opportunity, quantifiable only against the cloud bill."
)


@register
class SpotFallbackRule(Rule):
    id = "spot_fallback"
    # Savings model: spot reduces the CLOUD VM bill, not DBUs, so no dollar
    # saving is measurable from Databricks data — advisory $0, opportunity surfaced only.
    default_savings_status = "advisory"
    version = "2.0.0"
    title = "Move on-demand clusters to spot (with fallback)"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "spot_fallback.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge: one day of on-demand spend projects to a
        # monthly figure the cluster may never repeat. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # (context-only) cost figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        workers = int(row.get("workers") or 0)
        if workers < 1:
            return None  # single-node: driver stays on-demand, nothing to move

        # DBU spend is context only (how big is this cluster), NOT the spot-addressable
        # amount — spot reduces the VM bill, not the DBUs metered here.
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < t["min_monthly_cost_usd"]:
            return None  # too small to be worth surfacing as an opportunity

        source = (row.get("cluster_source") or "").strip().upper()
        is_automated = source in ("JOB", "PIPELINE", "PIPELINE_MAINTENANCE")
        workload_label = "automated batch" if is_automated else "interactive"
        cloud = row.get("cloud")

        # no dollar saving is measurable from Databricks data — the DBU rate is
        # identical on spot vs on-demand, so a spot toggle changes the metered spend by
        # $0. Advisory, $0 booked; the opportunity is surfaced qualitatively.
        savings = 0.0
        low, high, status = 0.0, 0.0, "advisory"

        name = row.get("cluster_name") or str(row["cluster_id"])
        why = (
            f"Cluster '{name}' runs {workers} on-demand worker(s) ({workload_label}) and "
            f"spent ~${monthly_cost:,.0f}/mo in DBUs on classic compute over the last "
            f"{ctx.lookback_days} days. Moving workers to spot (with fallback) can cut the "
            f"underlying CLOUD VM bill, but Databricks meters DBUs — the DBU rate is the "
            f"same on spot and on-demand — so the dollar saving isn't measurable here and "
            f"is shown as $0. Quantify it from your cloud billing export (AWS CUR / Azure / "
            f"GCP): worker node-minutes x the real spot/on-demand gap, net of eviction replay."
        )
        how = (
            "Set the cluster's worker availability to SPOT_WITH_FALLBACK "
            "(Compute > select the cluster > Edit > Advanced > Availability), keeping the "
            "driver on-demand. Confirm the workload tolerates occasional worker eviction, "
            "then measure the realized saving against your cloud VM bill — it is not "
            "visible in Databricks DBU billing."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Advisory $0: spot savings live in the cloud VM bill, unmeasurable from DBUs.
            savings_status=status,
            effort=EffortBand.LOW,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx) + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                availability=row.get("availability"),
                cloud=cloud,
                cluster_source=row.get("cluster_source"),
                workload_class=workload_label,
                worker_node_type=row.get("worker_node_type"),
                # The on-demand cloud VM spend a spot move would act on (only when
                # billing.cloud_infra_cost has data). Not priced as a saving: the spot
                # price is not in system tables.
                **({"cloud_cost_at_risk_usd": round(to_monthly(row["cloud_cost_30d_usd"], ctx.lookback_days), 2)}
                   if row.get("cloud_cost_30d_usd") is not None else {}),
                workers=workers,
                dbus_30d=row.get("dbus_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )
