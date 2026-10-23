"""Rule: Delta Sharing re-materializing shared data repeatedly.

EXPERIMENTAL (Labs tab). The "Delta Sharing" opportunity: a share whose tables
are materialized over and over is paying to rebuild the same data repeatedly
(recompute + cross-region egress).

**Operational, $0.** system.sharing.materialization_history records the materialization
EVENT, not its cost, and egress is a cloud charge outside DBUs — so this rule books and
addresses $0 in DBU terms and reports the materialization COUNT instead. Experimental
(Labs, see ``Rule.experimental``) — out of the headline entirely.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_sharing_materialization
from brickpulse.rules.registry import register

_BASIS = (
    "system.sharing.materialization_history (one row per materialization event), "
    "{n}-day lookback. Events are counted directly, so the signal has high confidence. No "
    "dollar figure is shown: the table records the event, not its compute, and Delta "
    "Sharing egress is a cloud-provider charge outside Databricks DBUs, so this is an "
    "operational finding, not a booked or addressable DBU saving. Experimental (Labs): "
    "confirm the recipients still need each materialization before changing the cadence."
)


@register
class DeltaSharingMaterializationRule(Rule):
    id = "delta_sharing_materialization"
    experimental = True
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Delta Sharing re-materializing shared data repeatedly"
    resource_type = "share"
    category = "sharing"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "delta_sharing_materialization.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        t = ctx.thresholds
        materializations = int(row.get("materializations") or 0)
        if materializations < int(t.get("min_materializations", 10)):
            return None

        observed_days = int(row.get("observed_days") or 0)
        distinct_tables = int(row.get("distinct_tables") or 0)
        distinct_recipients = int(row.get("distinct_recipients") or 0)
        provider = row.get("provider_name")
        name = row.get("share_name") or "share"
        per_day = materializations / max(1, observed_days)

        if per_day >= 24 or materializations >= 500:
            priority = RulePriority.HIGH
        elif per_day >= 4 or materializations >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        why = (
            f"Share '{name}'"
            + (f" (provider {provider})" if provider else "")
            + f" materialized its data {materializations:,} times over the last "
            f"{ctx.lookback_days} days"
            + (f" across {observed_days} day(s)" if observed_days else "")
            + f", about {per_day:.0f}/day, spanning {distinct_tables:,} table(s) for "
            f"{distinct_recipients:,} recipient(s). Each materialization recomputes and "
            f"copies the dataset and may incur compute and cross-region egress. Recipient "
            f"read frequency is not present in this data, so compare cadence with demand."
        )
        how = (
            "Check how often recipients actually query the materialized share. If the "
            "cadence outpaces reads, materialize less frequently, or share live "
            "(non-materialized) where the recipient's region and latency allow. Savings "
            "land on compute + cloud egress, not your DBU bill, so no DBU saving is booked."
        )

        viz = viz_sharing_materialization(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            materializations=materializations,
            distinct_tables=distinct_tables,
            distinct_recipients=distinct_recipients,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="share",
            resource_id=str(name),
            resource_name=str(name),
            monthly_waste_usd=0.0,
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days),
                viz=viz,
                materializations=materializations,
                observed_days=observed_days,
                distinct_tables=distinct_tables,
                distinct_recipients=distinct_recipients,
                provider_name=provider,
                operational_only=True,
            ),
        )
