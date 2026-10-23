"""Rule: enable / shorten SQL warehouse auto-stop.

A SQL warehouse with a missing, zero, or overly long auto-stop keeps clusters
warm between queries, burning DBUs while idle. Savings are computed from the
warehouse's *actual* hourly idle pattern (reconstructed from billing +
query.history), not a flat fraction — see ``finops_rules.savings`` and the
``benchmark/`` harness that proves the estimate stays conservative.

Scope: CLASSIC / PRO warehouses only (``warehouse_type <> 'SERVERLESS'``). A
shorter auto-stop here trades idle warm-pool cost against a real cold-start
restart cost, which the savings math accounts for. Serverless warehouses are
covered by the sibling rule ``warehouse_autostop_serverless``, where restarts
are effectively free so the reclaim/floor differ.
"""

from __future__ import annotations

from brickpulse.rules.change_tracking import TrackedSetting
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
from brickpulse.rules.evidence import (
    CONFIDENCE_HIGH,
    evidence_block,
    calendar_grid,
    hourly_states_to_grid,
    viz_hour_heatmap,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import (
    SAVINGS_CURVE_MINUTES,
    classic_autostop_curve,
    summarize_hourly_states,
    warehouse_autostop_savings,
    warehouse_autostop_savings_from_gaps,
)

_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_BASIS = (
    "system.compute.warehouse_events + system.query.history + billing.usage, "
    "{n}-day lookback. Billing is hourly, so within-hour idle is rounded down "
    "(the real figure may be slightly higher)."
)


@register
class WarehouseAutoStopRule(Rule):
    id = "warehouse_autostop"
    # The dollars are priced on exact warehouse_events running intervals + a
    # concurrency-merged, exact-interval counterfactual priced from the corrected
    # cost. Interval-priced findings set savings_status="bookable" in score(); this
    # default covers the coarse hourly fallback (no warehouse_events), which stays
    # out of the booked headline.
    default_savings_status = "estimated"
    version = "2.1.0"
    title = "Enable / shorten SQL warehouse auto-stop"
    resource_type = "warehouse"
    # "Saved so far" (finops_rules.change_tracking): auto-stop moved toward the target.
    # Partial moves are valued on evidence.savings_curve (PR F1b: the SQL prices the
    # same counterfactual at 10/15/.../120 min in one pass). A finding without one
    # (hourly fallback, older snapshot) is valued linearly; "off" counts as one day.
    tracked_setting = TrackedSetting(
        resource="warehouse", column="auto_stop_minutes",
        baseline_key="auto_stop_minutes", proposed_key="target_autostop_minutes",
        direction="decrease", off_values=(0, None), label="Auto-stop (minutes)",
    )
    category = "sql"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "warehouse_autostop.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        t = ctx.thresholds
        # Too little history to judge: the reclaimable idle tail is a property of
        # the warehouse's usage rhythm, which one day of queries can't establish.
        # See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None  # no billed cost attributed => nothing to save

        # Hourly state series reconstructed by the SQL (0=stopped 1=active
        # 2=idle-warm), most-recent 14 days, ordered. Drives the heatmap viz and
        # the fallback pricing. Absent => we can't price it, so suppress.
        states = row.get("hourly_states") or []
        if not states:
            return None
        agg = summarize_hourly_states(list(states))  # for the heatmap + display

        # Preferred: price the reclaimable idle against the warehouse's
        # ACTUAL running time (warm_minutes from system.compute.warehouse_events)
        # and the exact-interval counterfactual (reclaimable_minutes). Fall back to
        # the coarser hourly rollup only when warehouse_events didn't yield a running
        # interval (~50% recovery, still conservative).
        reclaim_min = row.get("reclaimable_minutes")
        warm_min = row.get("warm_minutes")
        interval_priced = reclaim_min is not None and warm_min
        if interval_priced:
            savings, reclaimable = warehouse_autostop_savings_from_gaps(
                reclaimable_min=float(reclaim_min),
                warm_minutes_current=float(warm_min),
                monthly_cost=monthly_cost,
            )
        else:
            savings, reclaimable = warehouse_autostop_savings(
                warm_hours=agg["warm_hours"],
                idle_warm_hours=agg["idle_warm_hours"],
                active_sessions=agg["active_sessions"],
                monthly_cost=monthly_cost,
            )
        if savings <= 0:
            return None

        current = row.get("auto_stop_minutes")
        target = int(t.get("target_autostop_minutes", 10))
        setting = f"{current}-min" if current else "no"
        name = row.get("warehouse_name") or str(row["warehouse_id"])
        # Prefer the true running time from warehouse_events; fall back to the
        # billed-hour count when the interval math didn't run (no events).
        running_min = row.get("running_minutes")
        warm_desc = (
            f"~{float(running_min) / 60.0:,.0f} running hours"
            if running_min
            else f"the last {len(states)} billed hours"
        )
        why = (
            f"SQL warehouse '{name}' has {setting} auto-stop and spent "
            f"~${monthly_cost:,.0f}/mo. It was warm but idle for ~{reclaimable:,.0f} "
            f"of {warm_desc}. A {target}-minute auto-stop would "
            f"reclaim ~${savings:,.0f}/mo of that idle time."
        )
        how = (
            f"Set auto-stop to {target} minutes (Compute > SQL Warehouses > select the "
            "warehouse > Edit > Auto stop). Users may see a cold start after idle gaps; "
            "the saving already deducts the measured restart cost."
        )

        # evidence.savings_curve (plan F.2, PR F1b): the same exact-interval
        # counterfactual at each candidate auto-stop between the target and today's
        # setting, priced like the estimate, so "Saved so far" can value a partial
        # move. Interval path only: the hourly fallback has no per-setting reclaim,
        # so its findings carry no curve and are valued linearly.
        curve = (
            classic_autostop_curve(
                curve_minutes=row.get("curve_minutes"),
                curve_reclaimable_minutes=row.get("curve_reclaimable_minutes"),
                target_minutes=target,
                current_autostop_min=current,
                estimate=savings,
                warm_minutes_current=float(warm_min),
                monthly_cost=monthly_cost,
                candidates=t.get("savings_curve_minutes", SAVINGS_CURVE_MINUTES),
            )
            if interval_priced
            else []
        )

        grid, dates = calendar_grid(list(states), row.get("grid_start"))
        viz = viz_hour_heatmap(
            grid=grid, day_labels=_DAY_LABELS, reclaimable_hours=reclaimable,
            day_dates=dates,
        )
        evidence = evidence_block(
            confidence=CONFIDENCE_HIGH,
            data_basis=_BASIS.format(n=ctx.lookback_days) + history_note(row, ctx) + coverage_note(row),
            viz=viz,
            **history_evidence(row, ctx),
                **coverage_evidence(row),
            warehouse_size=row.get("warehouse_size"),
            dbus_30d=row.get("dbus_30d"),
            auto_stop_minutes=current,
            target_autostop_minutes=target,
            cost_30d_usd=row.get("cost_30d_usd"),
            warm_hours=agg["warm_hours"],
            idle_warm_hours=agg["idle_warm_hours"],
            avoidable_usd=savings,
            **({"savings_curve": curve} if curve else {}),
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            effort=EffortBand.TRIVIAL,
            priority=RulePriority.HIGH,
            why=why,
            how=how,
            evidence=evidence,
            # interval-priced rows (real running time + exact counterfactual
            # + corrected cost, capped at the corrected cost) are trustworthy enough
            # to book; the coarse hourly fallback stays out of the headline.
            savings_status="bookable" if interval_priced else None,
        )


#: Reshape now lives in finops_rules.evidence so the pipeline rule shares it.
_to_grid = hourly_states_to_grid
