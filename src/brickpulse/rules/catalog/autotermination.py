"""Reference rule: enable / shorten cluster auto-termination.

This is the canonical example every other rule is cloned from. Read it top to
bottom alongside ``sql/autotermination.sql.j2`` and ``docs/ADDING_A_RULE.md``.
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
    coverage_supports_booking,
    underpriced,
)
from brickpulse.rules.evidence import (
    CONFIDENCE_MEDIUM,
    cluster_daily_evidence,
    cluster_kind_label,
    cluster_resource_metrics,
    cluster_resources_viz,
    viz_cluster_week_heatmap,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import cluster_autoterm_savings


@register
class AutoTerminationRule(Rule):
    id = "cluster_autotermination"
    # kept BOOKABLE (with the CPU/activity caveat noted in the SQL header) —
    # the gap method measures reclaimable idle seconds directly. 
    default_savings_status = "bookable"
    version = "1.0.0"
    title = "Enable / shorten cluster auto-termination"
    resource_type = "cluster"
    # "Saved so far" (finops_rules.change_tracking): auto-termination moved toward the
    # proposal. Linear; "off" counts as the longest idle gap the evidence measured.
    tracked_setting = TrackedSetting(
        resource="cluster", column="auto_termination_minutes",
        baseline_key="auto_termination_minutes",
        proposed_key="proposed_auto_termination_minutes",
        direction="decrease", off_values=(0, None), label="Auto-termination (minutes)",
    )
    category = "compute"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "autotermination.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. One day's idle gaps are not a habit, and the
        # monthly figure this rule quotes is an extrapolation from the window — so
        # this is checked before any pricing. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        proposed = int(t["max_acceptable_minutes"])
        running_min = float(row.get("running_minutes") or 0.0)
        idle_min = float(row.get("idle_minutes") or 0.0)
        reclaimable_gross = float(row.get("reclaimable_gross_min") or 0.0)
        num_gaps = int(row.get("num_reclaim_gaps") or 0)
        longest_gap = int(row.get("longest_idle_gap_min") or 0)
        job_runs = int(row.get("job_runs_30d") or 0)

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0 or running_min <= 0:
            return None  # no billed cost / no runtime => nothing to save

        # Gap-based: auto-termination reclaims only idle time in CONTIGUOUS gaps
        # longer than the proposed timeout — not scattered idle between bursts.
        savings, reclaimable_hours = cluster_autoterm_savings(
            reclaimable_gross_min=reclaimable_gross,
            num_reclaim_gaps=num_gaps,
            running_minutes=running_min,
            monthly_cost=monthly_cost,
        )
        if savings <= 0 or reclaimable_hours <= 0:
            return None  # no long-enough idle gaps => tightening wouldn't help

        current = row.get("auto_termination_minutes")
        setting = "no" if not current else f"{current}-min"
        run_h, idle_h = running_min / 60.0, idle_min / 60.0
        kind = cluster_kind_label(row.get("cluster_source"))
        why = (
            f"{kind} '{row['cluster_name']}' has {setting} auto-termination. Over the last "
            f"{ctx.lookback_days} days it ran {run_h:,.0f}h and was idle (CPU below 10%) for "
            f"{idle_h:,.0f}h of that. About {reclaimable_hours:,.0f}h of the idle time sat in "
            f"stretches longer than {proposed} min (longest {longest_gap / 60.0:,.0f}h) — time a "
            f"tighter auto-termination could reclaim, worth ~${savings:,.0f}/mo of the "
            f"~${monthly_cost:,.0f}/mo spend."
        )
        how = (
            f"Set auto-termination to {proposed} minutes (Compute > select the cluster > Edit > "
            f"Auto Termination). First confirm no attached notebook, stream, or JDBC session is "
            f"deliberately holding it — those keep it alive regardless of the timeout."
        )

        # Confidence is MEDIUM, not high: CPU idle is a proxy for the command/
        # session inactivity auto-termination actually keys on, and that signal is
        # not exposed for interactive clusters (query.history covers only DBSQL).
        basis = (
            f"system.compute.node_timeline per-minute CPU, {ctx.lookback_days}-day lookback; "
            f"reclaimable = idle time in contiguous gaps longer than {proposed} min, net of "
            f"restart cost. Auto-termination keys on command/session activity, which system "
            f"tables don't expose for interactive clusters — so this CPU-idle figure is an "
            f"upper bound." + (f" {job_runs} job run(s) seen on this cluster." if job_runs else "")
            + history_note(row, ctx) + coverage_note(row)
        )

        # book the dollars only when coverage is KNOWN adequate. underpriced()
        # above already dropped findings whose coverage is known-low; this additionally
        # downgrades a finding whose coverage is UNKNOWN (an old snapshot without the
        # columns) to estimated — visible, but contributing $0 to the booked headline
        # rather than a confident dollar we cannot actually price.
        status = "bookable" if coverage_supports_booking(row) else "estimated"

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=row.get("cluster_name") or str(row["cluster_id"]),
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            savings_status=status,
            effort=EffortBand.TRIVIAL,
            priority=RulePriority.HIGH,
            why=why,
            how=how,
            evidence=cluster_daily_evidence(
                cpu_hourly=row.get("cpu_hourly"),
                cpu_ts=row.get("cpu_ts"),
                idle_frac=row.get("idle_frac"),
                cpu_peak=row.get("cpu_peak"),
                lookback_days=ctx.lookback_days,
                confidence=CONFIDENCE_MEDIUM,
                data_basis=basis,
                # Extra charts. First: WHEN was it used, as a weekday x hour heatmap
                # — the same "where are the idle gaps?" panel the warehouse auto-stop
                # rule shows (omitted when the row predates the matrix). Second: CPU
                # is not enough to explain a cluster; memory, swap, I/O wait and
                # network show whether idle time is real slack or a blocked workload.
                viz_extra=[
                    viz_cluster_week_heatmap(
                        active_by_dow_hour=row.get("active_by_dow_hour"),
                        up_by_dow_hour=row.get("up_by_dow_hour"),
                        active_by_hour_minute=row.get("active_by_hour_minute"),
                        lookback_days=ctx.lookback_days,
                    ),
                    cluster_resources_viz(
                        row=row, lookback_days=ctx.lookback_days, cluster_kind=kind
                    ),
                ],
                cluster_kind=kind,
                node_type=row.get("node_type_id"),
                worker_count=row.get("worker_count"),
                **cluster_resource_metrics(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                auto_termination_minutes=current,
                # The setting `how` tells the user to apply, as data, so the UI can
                # show "off → 60 min" without parsing prose.
                proposed_auto_termination_minutes=proposed,
                running_hours=round(run_h, 1),
                idle_hours=round(idle_h, 1),
                reclaimable_hours=reclaimable_hours,
                longest_idle_gap_hours=round(longest_gap / 60.0, 1),
                job_runs_30d=job_runs,
                dbus_30d=row.get("dbus_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )
