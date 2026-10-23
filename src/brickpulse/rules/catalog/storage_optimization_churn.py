"""Rule: tables generating heavy Predictive Optimization maintenance.

EXPERIMENTAL (Labs tab). The "storage lifecycle" opportunity: a table that Predictive
Optimization must maintain HEAVILY — repeated compaction / clustering / vacuum, high
DBUs — is usually being written in a fragmenting pattern (frequent tiny appends, churny
MERGEs) that forces constant re-optimization. Fixing the write pattern at the source
cuts both the PO maintenance cost and the downstream query cost.

**Operational, $0.** We report the estimated DBUs PO spent (as PO itself reports them);
we do NOT convert to dollars, because PO bills under its own SKU and the per-table dollar
split is not reliably reconstructable from these tables. So the finding books and
addresses $0 and reports the PO DBUs instead. Experimental (Labs, see
``Rule.experimental``) — out of the headline entirely.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_storage_po
from brickpulse.rules.history import insufficient_history
from brickpulse.rules.registry import register

_BASIS = (
    "system.storage.predictive_optimization_operations_history (estimated DBUs PO spent "
    "per table, by operation type), {n}-day lookback. The DBUs are read DIRECTLY from PO's "
    "own accounting, so the signal has high confidence. No dollar figure is shown: PO bills "
    "under its own SKU and the per-table dollar split is not reliably reconstructable from "
    "these tables, so this is an operational finding, not a booked or addressable DBU "
    "saving. Experimental (Labs): a heavily-maintained table is not always fixable, so "
    "confirm the write pattern before acting."
)


@register
class StorageOptimizationChurnRule(Rule):
    id = "storage_optimization_churn"
    experimental = True
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Tables generating heavy Predictive Optimization maintenance"
    resource_type = "table"
    category = "storage"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "storage_optimization_churn.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A one-day burst of PO work (e.g. a backfill) is not the recurring pattern this
        # rule targets — require enough distinct days first.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds
        po_dbus = float(row.get("po_dbus") or 0.0)
        if po_dbus < float(t.get("min_po_dbus", 50)):
            return None
        operations = int(row.get("operations") or 0)
        if operations < int(t.get("min_operations", 30)):
            return None

        name = row.get("table_full_name") or str(row["table_id"])
        top_op = (row.get("top_operation") or "").strip() or None
        top_op_dbus = float(row.get("top_operation_dbus") or 0.0)
        observed_days = int(row.get("observed_days") or 0)
        per_day = po_dbus / max(1, observed_days)

        # Priority on how much PO maintenance the table draws.
        if po_dbus >= 500 or per_day >= 30:
            priority = RulePriority.HIGH
        elif po_dbus >= 150 or per_day >= 8:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        op_clause = ""
        if top_op:
            gloss = {
                "COMPACTION": "compaction",
                "CLUSTERING": "clustering (data being re-organised repeatedly)",
                "VACUUM": "vacuum (retiring a lot of deleted/overwritten files)",
            }.get(top_op, top_op)
            op_clause = (
                f" The dominant work is {gloss}, {top_op_dbus:,.0f} of those DBUs."
            )
        why = (
            f"Predictive Optimization spent about {po_dbus:,.0f} estimated DBUs across "
            f"{operations:,} operations maintaining table '{name}' over the last "
            f"{ctx.lookback_days} days (~{per_day:,.0f} DBU/day).{op_clause} This measures "
            f"maintenance intensity, not its cause. Inspect the table's write pattern before "
            f"changing ingestion or clustering."
        )
        how = (
            "Look at how this table is written: batch small streaming appends, use "
            "liquid clustering / a sensible partitioning key, and avoid row-by-row MERGEs "
            "where a bulk write would do. Reducing the fragmentation at the source cuts "
            "PO's maintenance work and speeds up reads. No dollar saving is booked. The "
            "DBUs shown are PO's own estimate, and not every heavy table is worth changing."
        )

        viz = viz_storage_po(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            po_dbus=po_dbus,
            operations=operations,
            compaction_dbus=float(row.get("compaction_dbus") or 0.0),
            clustering_dbus=float(row.get("clustering_dbus") or 0.0),
            vacuum_dbus=float(row.get("vacuum_dbus") or 0.0),
            top_operation=top_op,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="table",
            resource_id=str(row["table_id"]),
            resource_name=name,
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
                po_dbus=round(po_dbus, 2),
                operations=operations,
                observed_days=observed_days,
                compaction_dbus=row.get("compaction_dbus"),
                clustering_dbus=row.get("clustering_dbus"),
                vacuum_dbus=row.get("vacuum_dbus"),
                top_operation=top_op,
                operational_only=True,
            ),
        )
