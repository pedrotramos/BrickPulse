"""Rule: repeatedly-failing SQL queries burning warehouse compute.

One of the "SQL workload efficiency" opportunities:
a warehouse that keeps running the SAME failing query is spending compute on work
that produces nothing. Unlike a slow query (a tuning opportunity, hard to price),
a *repeated failure* is directly avoidable work — fix or remove the query and that
slot time disappears.

**Advisory, $0 booked.** We measure the compute the failed queries consumed
(apportioned from Spark task-time), but whether the bill actually drops depends on
whether the warehouse would otherwise sit idle — so the dollar figure is the
ADDRESSABLE spend, not a booked saving.
Promoted out of Labs (2026-09-23) as an advisory rule: shown on the main surfaces, $0 booked.

The finding ranks on ``monthly_waste_usd`` = that addressable failed-compute spend,
the same convention the other advisory rules (warehouse_scale_out, untagged_spend)
use for their non-savings headline metric.
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
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_sql_failures
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.query.history (execution_status = 'FAILED' on the warehouse's queries; "
    "total_task_duration_ms apportions the compute) + system.billing.usage x "
    "list_prices for the warehouse's corrected cost, {n}-day lookback. Failures are "
    "directly counted, not inferred. The failure signal has high confidence. The dollar "
    "figure is the compute the failed queries consumed (an ADDRESSABLE upper bound): "
    "eliminating the failing work reclaims it only if the warehouse would otherwise "
    "idle, so no saving is booked."
)


@register
class SqlRepeatedFailuresRule(Rule):
    id = "sql_repeated_failures"
    # Advisory: the failure signal is measured, but the reclaimable dollars depend on
    # the warehouse's idle behaviour, which we don't model here.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Repeatedly-failing SQL queries burning warehouse compute"
    resource_type = "warehouse"
    category = "sql"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "sql_repeated_failures.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A single bad afternoon (one incident) is not the recurring pattern this
        # rule targets — require enough distinct days first (finops_rules.history).
        if insufficient_history(row, ctx):
            return None
        # Book $0, but still report a warehouse cost, so keep the same coverage bar.
        if underpriced(row):
            return None

        t = ctx.thresholds
        queries = int(row.get("queries") or 0)
        failed = int(row.get("failed_queries") or 0)
        if failed < int(t.get("min_failed_queries", 20)):
            return None
        fail_rate = float(row.get("fail_rate") or 0.0)
        if fail_rate < float(t.get("min_fail_rate", 0.05)):
            return None  # occasional failures are normal; only a habit is a finding

        # Apportion the warehouse's compute to failed queries by Spark task-time
        # (slot occupancy) — a query that failed after a big scan cost more than one
        # that failed at parse time. Fall back to execution-duration share when task
        # duration is unpopulated (some queries never report it).
        total_task = float(row.get("total_task_ms") or 0.0)
        failed_task = float(row.get("failed_task_ms") or 0.0)
        if total_task > 0:
            share = failed_task / total_task
        else:
            total_exec = float(row.get("total_exec_ms") or 0.0)
            failed_exec = float(row.get("failed_exec_ms") or 0.0)
            share = (failed_exec / total_exec) if total_exec > 0 else 0.0
        share = max(0.0, min(1.0, share))

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        addressable = monthly_cost * share
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None  # too little compute at stake to be worth an admin's time

        distinct_shapes = int(row.get("distinct_failed_shapes") or 0)
        top_stmt = (row.get("top_failed_statement") or "").strip() or None
        top_count = int(row.get("top_failed_count") or 0)
        name = row.get("warehouse_name") or str(row["warehouse_id"])

        # Priority on how much of the load fails AND how much compute it burns: a
        # warehouse failing a third of its queries and burning real money is HIGH.
        if fail_rate >= 0.25 or addressable >= 500:
            priority = RulePriority.HIGH
        elif fail_rate >= 0.10 or addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        top_clause = ""
        if top_stmt and top_count > 1:
            snippet = top_stmt[:120] + ("…" if len(top_stmt) > 120 else "")
            top_clause = (
                f" The single most-repeated failing query ran {top_count:,} times: "
                f"“{snippet}”."
            )
        why = (
            f"SQL warehouse '{name}' failed {failed:,} of its {queries:,} queries "
            f"({fail_rate * 100:.0f}%) over the last {ctx.lookback_days} days across "
            f"{distinct_shapes:,} distinct failing query shape(s). Failed queries still "
            f"consume warehouse compute — about {share * 100:.0f}% of the warehouse's "
            f"Spark task-time, worth ~${addressable:,.0f}/mo — but produce nothing."
            f"{top_clause} The data identifies repeated failed work, but it does not "
            f"identify the cause or whether the caller is scheduled or interactive."
        )
        how = (
            "Open Query History filtered to this warehouse and Status = Failed, group "
            "by the repeating statement, and fix or retire the job behind it (a broken "
            "reference, a permission gap, an OOM that needs a bigger size, or a "
            "schedule that should be paused). No dollar saving is booked: the reclaim "
            "is real only if the warehouse would otherwise be idle during that time. "
            "confirm against its auto-stop and busy hours first."
        )

        viz = viz_sql_failures(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            fail_rate=fail_rate,
            failed_queries=failed,
            total_queries=queries,
            failed_compute_share=share,
            addressable_monthly_usd=addressable,
            top_failed_statement=top_stmt,
            top_failed_count=top_count,
            distinct_failed_shapes=distinct_shapes,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            # Addressable failed-compute spend (the headline metric for this advisory
            # finding). Ranking uses monthly_savings, which is $0 by design.
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,  # find the job behind it, then fix/retire
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_size=row.get("warehouse_size"),
                warehouse_type=row.get("warehouse_type"),
                queries=queries,
                failed_queries=failed,
                fail_rate=row.get("fail_rate"),
                distinct_failed_shapes=distinct_shapes,
                failed_compute_share=round(share, 4),
                top_failed_statement=top_stmt,
                top_failed_count=top_count,
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
