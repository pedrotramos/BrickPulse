"""Rule: SQL warehouse ``min_clusters`` above 1 holds clusters nobody needs (Tier 2, C5).

``min_clusters`` is a FLOOR: whenever the warehouse runs, at
least that many clusters run, whether or not a single query needs the second one.
Multi-cluster autoscaling already adds clusters on demand, so with ``min_clusters = 1``
the warehouse still scales out when queries arrive together — the floor only buys
capacity for the first seconds of a burst.

The costly shape is not only an always-on warehouse. A serverless warehouse woken
dozens of times an hour by short queries brings up ``min_clusters`` clusters on EVERY
start, and they all sit out the idle timer. At hourly grain every hour looks busy and
the excess spreads thin enough to vanish, so the SQL rebuilds cluster-minutes from the
``warehouse_events`` sequence and compares them, minute by minute, with the clusters
concurrency actually needed (``ceil(queries overlapping the minute / 10)``). No other
rule sees this: ``warehouse_autostop*`` has nothing to shorten at the 1–2 minute floor,
``warehouse_rightsize`` holds back once queueing is above 2 %, and
``warehouse_keepalive_queries`` doesn't fire when the queries do real work.

**Pricing.** excess cluster-minutes × the warehouse's measured $/cluster-minute (billed
list cost over rebuilt cluster-minutes). Only clusters held by the FLOOR count —
clusters the autoscaler added above it would be added with ``min_clusters = 1`` too.

**Interaction with ``warehouse_scale_out`` (R3).** Lowering the floor removes capacity;
scale-out adds it. This rule stays silent whenever scale-out's own gate holds (queued
share and p95 wait both at or above its thresholds, mirrored here) and whenever one
cluster covers the load in fewer than ``min_one_cluster_share`` of running minutes, so
the two can never recommend opposite changes for one warehouse.

**Estimated saving** (promoted out of Labs 2026-09-23). The minute-level excess is
measured and IS what the bill drops by, so it is the saving — ``estimated`` (shown and
ranked, not booked), because the "first seconds of a burst" queueing cost of a lower
floor is not measured. Composes ``exclusive`` with the auto-stop / right-size findings on
the same warehouse (the pool books the best one, never the sum).

**Phased (2026-10-06, docs/ADDING_A_RULE.md "Rules that reduce capacity").** The data
may support min_clusters 1 from 30, but every cluster removed is capacity users may
feel at the start of a burst. So the finding recommends one bounded step now
(``phasing.count_plan``: 30 -> 10 -> 4 -> 2 -> 1, at most 3x per step) and leads with
the FULL saving: ``monthly_savings_usd`` = ``monthly_savings_high_usd`` = the saving at
min 1, ``monthly_savings_low_usd`` = the first step's, priced by the SQL in the same pass
(``step_excess_cluster_min``). ``proposed_min_clusters`` is the step (what to set NOW).
"""

from __future__ import annotations

from dataclasses import replace

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block, viz_daily_stack
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.phasing import (
    count_plan,
    max_step_ratio,
    observe_days,
    phase_sentence,
    phase_target_clause,
    phasing_evidence,
)
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.compute.warehouse_events (cluster_count after every STARTING / RUNNING / "
    "SCALED_UP / SCALED_DOWN / STOPPING / STOPPED event, rebuilt into cluster-minutes) "
    "vs system.query.history (queries overlapping each running minute, / ~{qpc} per "
    "cluster = clusters needed), priced at the warehouse's own billed list cost per "
    "cluster-minute (system.billing.usage x list_prices), {n}-day lookback. Only "
    "clusters held by the min_clusters FLOOR count; overlap-per-minute overstates "
    "concurrency, so the figure leans low. It does not price the brief queueing a "
    "lower floor may add at the start of a burst — an ESTIMATED saving, shown but not "
    "booked."
)


# Published DBU/h per cluster by warehouse size (the same for serverless, pro and
# classic). Only used as a sanity ceiling on the measured rate.
_DBU_PER_HOUR = {
    "2X_SMALL": 4, "X_SMALL": 6, "SMALL": 12, "MEDIUM": 24, "LARGE": 40,
    "X_LARGE": 80, "2X_LARGE": 144, "3X_LARGE": 272, "4X_LARGE": 528,
}


_WATCH = "the queued-query share and p95 query latency"


def _is_service(user: str | None) -> bool:
    """A service principal / service account rather than a person (no ``@``)."""
    return bool(user) and "@" not in str(user)


@register
class WarehouseMinClustersRule(Rule):
    id = "warehouse_min_clusters"
    # Estimated: measured counterfactual, shown + ranked, not booked.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "Lower min_clusters on a SQL warehouse that rarely needs more than one"
    resource_type = "warehouse"
    # "Saved so far" (finops_rules.change_tracking): min clusters moved toward 1. Valued
    # on evidence.phasing when present, else linearly.
    tracked_setting = TrackedSetting(
        resource="warehouse", column="min_clusters",
        baseline_key="min_clusters", proposed_key="proposed_min_clusters",
        proposed_value=1, direction="decrease", uses_phasing=True,
        label="Minimum clusters",
    )
    category = "sql"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    cloud_savings = "none"
    sql_file = "warehouse_min_clusters.sql.j2"

    def render_sql(self, ctx: RuleContext) -> str:
        """Inject the phasing step ratio (engine-wide ``phasing.max_step_ratio`` or this
        rule's ``phasing_max_step_ratio``) so the SQL prices the same first step that
        score() recommends."""
        thresholds = {**ctx.thresholds, "phasing_max_step_ratio": max_step_ratio(ctx)}
        return super().render_sql(replace(ctx, thresholds=thresholds))

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        min_clusters = int(row.get("min_clusters") or 1)
        if min_clusters <= 1:
            return None

        # R3: never recommend less capacity where warehouse_scale_out recommends more.
        # Silent at or above max_queued_pct on the queued share ALONE; scale_out needs at
        # least its min_queued_pct (same default, 0.10) plus a p95 wait, so the two can
        # never both fire. Stricter on purpose: a warehouse queueing half its queries for
        # under 2s each is not one to take capacity from.
        queued_pct = float(row.get("queued_pct") or 0.0)
        p95_wait = float(row.get("p95_queue_wait_s") or 0.0)
        if queued_pct >= float(t.get("max_queued_pct", 0.10)):
            return None

        running_min = float(row.get("running_min") or 0.0)
        one_cluster_min = float(row.get("one_cluster_min") or 0.0)
        if running_min <= 0:
            return None
        one_share = one_cluster_min / running_min
        if one_share < float(t.get("min_one_cluster_share", 0.90)):
            return None  # concurrency genuinely needs >1 cluster too often

        actual = float(row.get("actual_cluster_min") or 0.0)
        excess = min(float(row.get("excess_cluster_min") or 0.0), actual)
        window_cost = float(row.get("cost_window_usd") or 0.0)
        if actual <= 0 or excess <= 0 or window_cost <= 0:
            return None

        # Sanity: the rebuilt cluster-minutes must explain the bill. A warehouse whose
        # events are missing for part of the window would inflate $/cluster-minute.
        # (Rebuilt minutes EXCEEDING the bill only lowers the rate — conservative.)
        dbus = float(row.get("dbus_30d") or 0.0)
        dbu_per_cluster_hour = dbus / (actual / 60.0)
        published = _DBU_PER_HOUR.get(str(row.get("warehouse_size") or "").upper())
        if published and dbu_per_cluster_hour > published * float(t.get("max_rate_ratio", 1.25)):
            return None

        rate = window_cost / actual  # measured list $ per cluster-minute
        addressable_window = excess * rate
        addressable = to_monthly(addressable_window, ctx.lookback_days)
        if addressable < float(t.get("min_addressable_usd", 50)):
            return None
        monthly_cost = to_monthly(window_cost, ctx.lookback_days)
        excess_share = excess / actual

        # Phased plan: one bounded step now, the rest after watching the KPIs.
        ratio = max_step_ratio(ctx)
        plan = count_plan(min_clusters, 1, max_step_ratio=ratio)
        data_warning = None
        sql_step = row.get("step_min_clusters")
        if sql_step is not None and int(sql_step) != plan[0]:
            # The SQL priced a different first step (a ratio mismatch between the
            # render and the scoring context). Trust the step the dollars belong to.
            step = max(1, min(int(sql_step), min_clusters - 1))
            plan = [step] + count_plan(step, 1, max_step_ratio=ratio)
            data_warning = (
                f"The first step was priced at min_clusters = {step}, which differs from "
                f"the step plan's own first step; the plan below starts from the priced "
                f"step. Re-run the snapshot to realign them."
            )
        step = plan[0]
        if len(plan) == 1:
            step_window = addressable_window  # one step straight to the target
        elif row.get("step_excess_cluster_min") is not None:
            step_window = min(float(row.get("step_excess_cluster_min") or 0.0), excess) * rate
        else:
            # Older candidate rows without the step column: per running minute the
            # floor-only clusters are min - 1 at the target and min - step at the step.
            step_window = addressable_window * (min_clusters - step) / (min_clusters - 1)
        step_saving = min(to_monthly(step_window, ctx.lookback_days), addressable)
        phased = len(plan) > 1
        obs = observe_days(ctx)

        # Per day the warehouse was actually billed: a warehouse created mid-window
        # would otherwise look calmer than it is.
        days = max(int(row.get("observed_days") or 0), 1)
        starts = int(row.get("starts") or 0)
        starts_per_day = starts / days
        queries = int(row.get("queries") or 0)
        hours_of_day = int(row.get("hours_of_day_active") or 0)
        running_hours = running_min / 60.0
        name = row.get("warehouse_name") or str(row["warehouse_id"])
        wtype = str(row.get("warehouse_type") or "").upper()
        auto_stop = row.get("auto_stop_minutes")
        max_clusters = int(row.get("max_clusters") or min_clusters)

        # By dollars only: with min_clusters = 2 the excess share is ~50% by
        # construction, so a share-based tier would make every finding HIGH.
        if addressable >= 1000:
            priority = RulePriority.HIGH
        elif addressable >= 200:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        clients = sorted(
            (c for c in (row.get("top_clients") or []) if c),
            key=lambda c: -int(c.get("n") or 0),
        )
        top = clients[0] if clients else None
        top_share = (int(top.get("n") or 0) / queries) if (top and queries) else 0.0

        churny = starts_per_day >= float(t.get("churn_starts_per_day", 24))
        churn_clause = (
            f" It started {starts:,} times ({starts_per_day:,.0f} a day), and each start "
            f"brought up all {min_clusters} clusters, which then sat out the "
            f"{int(auto_stop)}-minute idle timer."
            if churny and auto_stop
            else (f" It started {starts:,} times ({starts_per_day:,.0f} a day), each "
                  f"bringing up all {min_clusters} clusters." if churny else "")
        )
        kind = "serverless " if wtype == "SERVERLESS" else ""
        why = (
            f"{kind.capitalize() if kind else ''}SQL warehouse '{name}' has min_clusters = "
            f"{min_clusters}, so it runs at least {min_clusters} clusters whenever it is "
            f"up. Over the last {ctx.lookback_days} days it was up ~{running_hours:,.0f} "
            f"hours, and in {one_share * 100:.0f}% of those minutes its queries fitted on "
            f"ONE cluster ({queries:,} queries, at most ~"
            f"{int(t.get('max_queries_per_cluster', 10))} concurrent per cluster)."
            f"{churn_clause} {excess_share * 100:.0f}% of its cluster-minutes were held "
            f"only by the floor — ~${addressable:,.0f}/mo of its ~${monthly_cost:,.0f}/mo."
            f" Autoscaling adds clusters when queries queue, so the floor is not what "
            f"serves its occasional bursts."
        )
        if phased:
            why += phase_target_clause(target=1, target_savings=addressable)
        if queued_pct >= 0.01:
            why += (
                f" {queued_pct * 100:.1f}% of queries already wait for capacity (p95 "
                f"{p95_wait:.1f}s); expect a little more at the start of a burst while the "
                f"second cluster starts."
            )

        how = (
            f"Set Minimum clusters from {min_clusters} to {step} (SQL Warehouses > '{name}' "
            f"> Edit > Scaling > Min), leaving Maximum at {max_clusters} so autoscaling "
            f"still adds clusters on demand."
            + phase_sentence(plan=plan, observe_days=obs, watch=_WATCH)
        )
        # Second remedy: the wake-up PATTERN. min_clusters = 1 fixes the multiplier;
        # a service querying every few minutes around the clock keeps waking it.
        if churny and top is not None:
            who = str(top.get("client") or "unknown")
            user = top.get("user")
            culprit = f"'{who}'" + (f" as {user}" if user else "")
            svc = " (a service identity)" if _is_service(user) else ""
            around = " around the clock" if hours_of_day >= 20 else ""
            how += (
                f" Separately, the {starts_per_day:,.0f} starts a day are driven by "
                f"queries arriving every few minutes{around}; {top_share * 100:.0f}% came "
                f"from {culprit}{svc}. Batching that client's queries, or moving it to a "
                f"small dedicated warehouse, stops the wake-ups themselves — "
                f"min_clusters = 1 only fixes how many clusters each wake-up starts."
            )

        viz = viz_daily_stack(
            title="Cluster-hours per day: needed vs held only by the min_clusters floor",
            unit="cluster-h",
            series=[
                ("w", "Clusters doing work (or added by autoscaling)", "info"),
                ("x", f"Held only by min_clusters = {min_clusters}", "warn"),
            ],
            rows=row.get("days"),
            lookback_days=ctx.lookback_days,
            note=(f"{excess_share * 100:.0f}% of cluster-hours were floor-only; "
                  f"one cluster covered the load in {one_share * 100:.0f}% of running minutes."),
            msg="min_clusters_hours",
            params={"min_clusters": min_clusters, "excess_pct": round(excess_share * 100),
                    "one_pct": round(one_share * 100)},
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked. Phased:
            # point = high = the full saving (to min 1), low = the first step's.
            monthly_savings_usd=round(addressable, 2),
            savings_status="estimated",
            monthly_savings_low_usd=round(step_saving, 2),
            monthly_savings_high_usd=round(addressable, 2),
            effort=EffortBand.TRIVIAL,  # a scaling field
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(
                    n=ctx.lookback_days, qpc=int(t.get("max_queries_per_cluster", 10)))
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_type=row.get("warehouse_type"),
                warehouse_size=row.get("warehouse_size"),
                auto_stop_minutes=auto_stop,
                min_clusters=min_clusters,
                max_clusters=max_clusters,
                # What to set NOW (the first step), so action.ts and `how` agree.
                proposed_min_clusters=step,
                step_min_clusters=step,
                step_excess_cluster_hours=round(
                    (step_window / rate if rate else 0.0) / 60.0, 1),
                step_monthly_usd=round(step_saving, 2),
                phasing=phasing_evidence(
                    "Minimum clusters", min_clusters, plan, observe_days=obs,
                    watch=_WATCH, step_savings=round(step_saving, 2),
                    target_savings=round(addressable, 2),
                ),
                **({"savings_range_basis": (
                    f"Low = the first step to min_clusters = {step} (~${step_saving:,.0f}/mo "
                    f"on its own); high = min_clusters = 1, what the data supports "
                    f"(~${addressable:,.0f}/mo). The figure shown is the full saving; reach "
                    f"it in steps, watching queueing between them."
                )} if phased else {}),
                **({"data_warning": data_warning} if data_warning else {}),
                actual_cluster_hours=round(actual / 60.0, 1),
                floor_cluster_hours=round(float(row.get("floor_cluster_min") or 0.0) / 60.0, 1),
                excess_cluster_hours=round(excess / 60.0, 1),
                excess_share=round(excess_share, 4),
                running_hours=round(running_hours, 1),
                one_cluster_share=round(one_share, 4),
                peak_clusters_needed=int(row.get("peak_needed") or 0),
                starts=starts,
                starts_per_day=round(starts_per_day, 1),
                queries=queries,
                queued_pct=queued_pct,
                p95_queue_wait_s=p95_wait,
                p50_query_duration_s=row.get("p50_duration_s"),
                hours_of_day_active=hours_of_day,
                top_clients=clients,
                usd_per_cluster_hour=round(rate * 60.0, 2),
                dbu_per_cluster_hour=round(dbu_per_cluster_hour, 1),
                cost_window_usd=round(window_cost, 2),
                addressable_monthly_usd=round(addressable, 2),
                dbus_30d=row.get("dbus_30d"),
            ),
        )

