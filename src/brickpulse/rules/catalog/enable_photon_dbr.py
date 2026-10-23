"""Rule: upgrade DBR / enable Photon on costly clusters.

Older Databricks Runtimes miss the price/performance gains of current LTS
runtimes and Photon. score() parses the leading major version of the cluster's
``dbr_version`` and flags clusters still on an old major that carry real spend.

Savings model:

The price/performance uplift from a runtime upgrade + Photon is a HEURISTIC
fraction of spend, not a measured saving. A real figure needs matched before/after
runs (or query fingerprints) of the SAME workload across the runtime/Photon change,
normalized for input volume — Photon accelerates SQL/Spark operators strongly but
does almost nothing for Python/ML, and the realized uplift depends entirely on the
workload's operator mix, which the DBR version string cannot reveal. The old rule
BOOKED a flat class-based fraction (5%/15%) as ``monthly_savings_usd``; that inflated
the account headline with an unverifiable estimate. So this rule now surfaces the
opportunity as **advisory $0**: which costly clusters are on an old runtime, and the
heuristic uplift as the *upper bound* of what an upgrade might reclaim (``high``, NOT
booked). The realized saving is quantifiable only by re-running the workload and
comparing measured DBUs — until then nothing is booked.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_LOW, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import classify_photon_workload, photon_savings

# Human labels for the workload class the upper-bound uplift was priced on, so the
# finding can say WHY it credited what it did.
_CLASS_LABEL = {
    "ml": "ML/Python-heavy runtime (Photon accelerates little of this)",
    "photon_on": "Photon already enabled (only the DBR upgrade remains)",
    "mixed": "mixed/unclassified all-purpose workload",
}

_BASIS = (
    "system.compute.clusters (DBR version + runtime class) + billing.usage x "
    "list_prices, {n}-day lookback. The ~{pct:.0f}% price/performance uplift is a "
    "HEURISTIC segmented by workload class (Photon strongly accelerates SQL/Spark, "
    "barely helps Python/ML), NOT a measured benchmark — it is shown as the UPPER "
    "BOUND of what an upgrade might reclaim, not a booked saving. The realized figure "
    "needs matched before/after runs of the same workload across the runtime/Photon "
    "change, normalized for input volume — advisory, $0 booked until then."
)


def _dbr_major(dbr_version) -> int | None:
    """Parse the leading integer of a DBR version string.

    e.g. "13.3.x" -> 13. Returns None for "custom:*", None, or anything whose
    leading token is not an integer (so score() can safely suppress it)."""
    if not dbr_version:
        return None
    text = str(dbr_version).strip()
    if text.lower().startswith("custom:"):
        return None
    lead = text.split(".", 1)[0]
    try:
        return int(lead)
    except (TypeError, ValueError):
        return None


@register
class EnablePhotonDbrRule(Rule):
    id = "enable_photon_dbr"
    # Savings model: the uplift is an unmeasured heuristic, so it is shown
    # as an upper bound only and nothing is booked — advisory $0. A real figure needs
    # matched before/after runs.
    default_savings_status = "advisory"
    version = "2.0.0"
    title = "Upgrade DBR / enable Photon on costly clusters"
    resource_type = "cluster"
    category = "compute"
    sql_file = "enable_photon_dbr.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge: the uplift is a fraction of extrapolated
        # spend, so a day or two of billing isn't a monthly figure worth quoting.
        # See finops_rules.history.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds

        major = _dbr_major(row.get("dbr_version"))
        # Unparseable / custom / missing DBR => can't confirm it's old => suppress.
        if major is None:
            return None
        # Already on a current-enough major => nothing to recommend.
        if major >= t["min_dbr_major"]:
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None  # no billed cost attributed => nothing to surface

        # Segment the heuristic uplift by workload class: Photon strongly
        # accelerates SQL/Spark, moderately helps ETL, and barely touches Python/ML,
        # so a flat 15% over-credits ML clusters. We classify from the runtime and
        # price the matching fraction — but this is the UPPER BOUND of what an upgrade
        # might reclaim, NOT a booked saving (still a heuristic, hence LOW confidence).
        workload_class = classify_photon_workload(row.get("dbr_version"))
        upper_bound, uplift = photon_savings(
            monthly_cost=monthly_cost,
            workload_class=workload_class,
            uplift_ml=float(t.get("uplift_ml", 0.05)),
            uplift_photon_on=float(t.get("uplift_photon_on", 0.05)),
            uplift_mixed=float(t.get("uplift_mixed", 0.15)),
        )
        if upper_bound <= 0:
            return None

        # the uplift is an unmeasured heuristic, so nothing is booked — the
        # figure is the max-addressable upper bound only. Advisory, $0 booked; the
        # opportunity is surfaced with its ceiling.
        savings = 0.0
        low, high, status = 0.0, round(upper_bound, 2), "advisory"

        class_label = _CLASS_LABEL.get(workload_class, _CLASS_LABEL["mixed"])
        why = (
            f"Cluster '{row.get('cluster_name') or row['cluster_id']}' runs on the "
            f"old runtime '{row.get('dbr_version')}' and spent ~${monthly_cost:,.0f}/mo "
            f"over the last {ctx.lookback_days} days. Upgrading to a current LTS "
            f"runtime plus Photon could reclaim UP TO ~${upper_bound:,.0f}/mo "
            f"(~{uplift * 100:.0f}% of spend for a {class_label}), but that is a "
            f"heuristic ceiling, not a measured saving — Photon accelerates SQL/Spark "
            f"far more than Python/ML, and the realized uplift depends on this "
            f"workload's operator mix. The dollar figure is shown as $0 until you "
            f"confirm it by re-running the workload and comparing measured DBUs."
        )
        how = (
            "Upgrade to a current LTS Databricks Runtime and enable Photon "
            "(Compute > select the cluster > Edit > Databricks Runtime Version + "
            "Use Photon Acceleration), then re-run the workload and compare the "
            "measured DBUs before vs after to quantify the realized saving — it is "
            "not predictable from the runtime version alone."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=row.get("cluster_name") or str(row["cluster_id"]),
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Advisory $0: the uplift is an unmeasured heuristic, shown as an upper
            # bound only; a real figure needs matched before/after runs.
            savings_status=status,
            effort=EffortBand.MEDIUM,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                # Still a heuristic uplift, not a measured benchmark, so LOW — but a
                # class-aware one, and the class is stated so the ceiling is auditable.
                confidence=CONFIDENCE_LOW,
                data_basis=_BASIS.format(n=ctx.lookback_days, pct=uplift * 100)
                + history_note(row, ctx),
                dbr_version=row.get("dbr_version"),
                dbr_major=major,
                workload_class=workload_class,
                uplift_fraction=uplift,
                upper_bound_usd=round(upper_bound, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                **history_evidence(row, ctx),
            ),
        )
