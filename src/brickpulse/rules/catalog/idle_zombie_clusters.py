"""Rule: terminate idle / zombie clusters.

An all-purpose (UI/API) cluster that still exists but sits essentially idle
across its sampled node-timeline windows is a "zombie": it is running (and
billing) but doing almost no useful work. Nearly all of its spend is waste.

Savings model:

* **Forward-spend guard.** The actionable saving is sized on a short RECENT
  window (``recency_days``), not the whole lookback. The old code annualized the
  whole-lookback cost (``idle_zombie_clusters.py:93``), so a cluster that ran hot
  early in the window but has since STOPPED was still sold as ongoing monthly
  savings — yet terminating an already-stopped cluster reclaims nothing forward.
* **Tiering.** A cluster STILL running idle now (recent spend > 0) is **estimated**
  — CPU idle is only a *proxy* for true inactivity (a held notebook/stream can keep
  a low-CPU cluster alive), so it is never booked. A STOPPED cluster (no recent
  spend) drops to **advisory** with **$0** forward savings; its historical idle
  waste is shown only as max-addressable context, since the honest action there is
  hygiene (delete it so it isn't accidentally restarted), not a dollar saving.
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
from brickpulse.rules.evidence import (
    CONFIDENCE_MEDIUM,
    cluster_activity_stats,
    cluster_daily_evidence,
    cluster_kind_label,
    cluster_resource_metrics,
    cluster_resources_viz,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register


@register
class IdleZombieClustersRule(Rule):
    id = "idle_zombie_clusters"
    # Savings model: a cluster STILL running idle stays estimated (CPU
    # idle is a proxy, never booked); score() drops a STOPPED cluster to advisory
    # ($0 forward) rather than annualizing its stale whole-lookback cost.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Terminate idle / zombie clusters"
    resource_type = "cluster"
    # "Saved so far" (finops_rules.change_tracking): deletion (generic), or ANY move
    # toward a shorter auto-termination counts in full (the zombie now stops). Its
    # evidence doesn't carry the timeout, so the baseline is the tracker's observation.
    tracked_setting = TrackedSetting(
        resource="cluster", column="auto_termination_minutes",
        direction="decrease", off_values=(0, None), valuation="any_toward",
        label="Auto-termination (minutes)",
    )
    category = "compute"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "idle_zombie_clusters.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        t = ctx.thresholds
        recency_days = int(t.get("recency_days", 7))
        avg_cpu = float(row.get("avg_cpu") or 0.0)
        idle_windows = int(row.get("idle_windows") or 0)
        total_windows = int(row.get("total_windows") or 0)
        job_runs = int(row.get("job_runs_30d") or 0)

        # Too little history to judge: a cluster that has only been running for a
        # day or two looks exactly like a chronic zombie, and "terminate this" on
        # that evidence is guesswork. Checked first — before any pricing.
        if insufficient_history(row, ctx):
            return None

        # Investigation-guided guard: with no utilization data, or a cluster that
        # is not actually idle, there is nothing to recommend.
        if total_windows <= 0:
            return None
        if avg_cpu >= t["idle_cpu_pct_threshold"]:
            return None
        if idle_windows < t["min_idle_windows"]:
            return None
        # A cluster kept up by a schedule is NOT a zombie to terminate — doing so
        # would break the job. Low CPU there means the job is trivial / on the
        # wrong compute (interactive_to_job) or the timeout is too long
        # (cluster_autotermination), not that the cluster is abandoned. Defer to
        # those rules. max_job_runs defaults to 0: a true zombie runs no jobs.
        if job_runs > int(t.get("max_job_runs", 0)):
            return None

        # Defer to auto-termination when a tighter timeout is the honest fix.
        # An idle interactive cluster with NO auto-stop, or a LOOSE one (> the
        # acceptable timeout), is exactly what cluster_autotermination reclaims —
        # and "add/tighten auto-stop" is safer than "terminate" on a shared box a
        # person may reconnect to. These two rules used to double-fire on the same
        # cluster with opposite advice (measured: 10 of 12 zombie findings also
        # fired auto-termination). A cluster is a genuine zombie-to-terminate only
        # when it ALREADY has a tight timeout yet is STILL running idle around the
        # clock — a timeout can't fix that, so terminating/investigating is right.
        if t.get("defer_to_autostop_when_reclaimable", True):
            autostop = row.get("auto_termination_minutes")
            max_ok = int(t.get("max_acceptable_autostop_minutes", 60))
            loose_or_missing = (
                autostop is None or int(autostop) <= 0 or int(autostop) > max_ok
            )
            if loose_or_missing:
                return None  # cluster_autotermination owns this one

        # Whole-lookback idle spend. this is no longer the booked figure —
        # it is kept only as the max-addressable CONTEXT (what the cluster wasted
        # while it ran). The ACTIONABLE saving is sized on the recent window below.
        lookback_monthly = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if lookback_monthly <= 0:
            return None  # no billed cost attributed => nothing to save
        idle_fraction = idle_windows / total_windows
        historical_waste = lookback_monthly * idle_fraction

        # forward-spend guard: size the actionable saving on the RECENT
        # window. A cluster that ran hot early in the lookback but has since STOPPED
        # has ~0 recent spend, so terminating it reclaims nothing going forward —
        # that is hygiene ($0), not the whole-lookback annualization the old code
        # booked. recent_* come from the recency-windowed aggregates in the SQL.
        recent_windows = int(row.get("recent_windows") or 0)
        recent_idle_windows = int(row.get("recent_idle_windows") or 0)
        recent_active_days = int(row.get("recent_active_days") or 0)
        forward_monthly = to_monthly(row.get("recent_cost_usd") or 0.0, recency_days)
        recent_idle_fraction = (
            recent_idle_windows / recent_windows if recent_windows > 0 else 0.0
        )

        still_running = recent_windows > 0 and forward_monthly > 0
        if still_running:
            # Live idle zombie: still accruing spend now. The reclaimable idle share
            # is a forward saving, but CPU idle is only a PROXY for true inactivity,
            # so it stays ESTIMATED (low=0) — the engine never books a termination on
            # a proxy signal. high = the reclaimable idle spend.
            monthly_cost = forward_monthly
            savings = forward_monthly * recent_idle_fraction
            # A cluster idle across the whole lookback (so it cleared the candidate
            # gates) but BUSY again in the recent window has ~no reclaimable idle now:
            # recent_idle_fraction -> 0 => savings -> 0. It is no longer a zombie, so
            # say nothing rather than emit a $0 "Estimated" finding. (The stopped
            # branch below intentionally books $0 — that path is advisory hygiene, not
            # a live reclaim — so this guard is scoped to the running path only.)
            if savings <= 0:
                return None
            low, high, status = 0.0, savings, "estimated"
        else:
            # Stopped / no recent spend: terminating an already-stopped cluster saves
            # nothing forward. Surface it as ADVISORY hygiene with $0 booked; the
            # historical idle waste is the max-addressable context only.
            monthly_cost = lookback_monthly
            savings = 0.0
            low, high, status = 0.0, historical_waste, "advisory"

        cluster_name = row.get("cluster_name") or str(row["cluster_id"])
        active_days, _last_iso, days_ago, peak_pct = cluster_activity_stats(
            row.get("cpu_ts"), row.get("cpu_peak")
        )
        recency = ""
        if days_ago is not None:
            ago = "today" if days_ago == 0 else f"{days_ago} day{'s' if days_ago != 1 else ''} ago"
            recency = (
                f" It ran on only {active_days} of the last {ctx.lookback_days} days "
                f"and last ran {ago}"
                + (f" (peaking at {peak_pct:.0f}% CPU when used)" if peak_pct else "")
                + "."
            )
        kind = cluster_kind_label(row.get("cluster_source"))
        autostop = row.get("auto_termination_minutes")
        idle_pct = idle_windows / max(total_windows, 1) * 100
        # We only reach here when the cluster ALREADY has a tight auto-stop (else
        # it deferred to cluster_autotermination above), so the honest story is
        # "a timeout is set and it's STILL up around the clock" — terminate.
        timeout_note = (
            f" It already has a {int(autostop)}-minute auto-stop, yet stayed up "
            f"around the clock anyway. Something keeps resetting the timer, so a "
            f"tighter timeout won't help."
            if autostop not in (None, 0)
            else ""
        )
        if still_running:
            why = (
                f"{kind} '{cluster_name}' averaged {avg_cpu:.1f}% CPU and sat idle in "
                f"{idle_windows} of {total_windows} sampled windows ({idle_pct:.0f}%) over "
                f"the last {ctx.lookback_days} days. It is still running now, with "
                f"~${monthly_cost:,.0f}/mo of current spend and "
                f"(~${savings:,.0f}/mo reclaimable, sized on the last {recency_days} "
                f"days). CPU idleness is a proxy for inactivity, so verify held "
                f"notebooks and streams before terminating."
                f"{timeout_note}{recency}"
            )
            how = (
                "Terminate the cluster (Compute > select the cluster > Terminate), or "
                "find what keeps it alive, such as a held notebook, stream session, or "
                "external keep-alive. Its existing auto-stop is not catching it."
            )
        else:
            # Stopped: $0 forward, hygiene only. Be explicit that terminating it now
            # saves nothing, and frame the historical figure as context, not savings.
            why = (
                f"{kind} '{cluster_name}' averaged {avg_cpu:.1f}% CPU and sat idle in "
                f"{idle_windows} of {total_windows} sampled windows ({idle_pct:.0f}%) while "
                f"it ran, but has not run in the last {recency_days} days. It is not "
                f"billing now, so terminating it saves nothing going forward "
                f"($0/mo). It wasted ~${historical_waste:,.0f}/mo while it did run; "
                f"the value now is hygiene. Delete it so it is not left to be "
                f"restarted and drift idle again.{timeout_note}{recency}"
            )
            how = (
                "Delete the stopped cluster (Compute > select the cluster > Delete) so "
                "it can't be accidentally restarted and left idle. No forward spend is "
                "reclaimed today. This is cleanup, not a booked saving."
            )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=cluster_name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Estimated while the cluster is still running idle (CPU idle is a proxy,
            # never booked); advisory $0 once it has stopped (hygiene only).
            savings_status=status,
            effort=EffortBand.LOW,
            priority=RulePriority.HIGH,
            why=why,
            how=how,
            evidence=cluster_daily_evidence(
                cpu_hourly=row.get("cpu_hourly"),
                cpu_ts=row.get("cpu_ts"),
                idle_frac=row.get("idle_frac"),
                cpu_peak=row.get("cpu_peak"),
                lookback_days=ctx.lookback_days,
                # CPU idle is a proxy for the session/command activity that keeps a
                # cluster alive; system tables don't expose that for interactive
                # clusters, so this is medium — not high — confidence.
                confidence=CONFIDENCE_MEDIUM,
                data_basis=(
                    f"system.compute.node_timeline per-minute CPU, {ctx.lookback_days}-day "
                    f"lookback. CPU idle is a proxy for true inactivity — a held notebook/stream "
                    f"session can keep a low-CPU cluster alive, so verify before terminating. "
                    + (
                        f"The reclaimable figure is sized on the last {recency_days} days of "
                        f"billed spend, not the whole lookback, so a since-stopped cluster is "
                        f"not sold as ongoing savings."
                        if still_running
                        else (
                            f"This cluster has NOT run in the last {recency_days} days, so its "
                            f"forward saving is $0 — the historical figure is max-addressable "
                            f"context, not a booked saving."
                        )
                    )
                    + history_note(row, ctx)
                ),
                # Second chart: what the cluster's memory / swap / I/O wait /
                # network were doing. A "zombie" at 3% CPU but 80% memory with
                # steady network is holding something — worth knowing before you
                # terminate it.
                viz_extra=[
                    cluster_resources_viz(
                        row=row, lookback_days=ctx.lookback_days, cluster_kind=kind
                    )
                ],
                cluster_kind=kind,
                node_type=row.get("node_type_id"),
                worker_count=row.get("worker_count"),
                **cluster_resource_metrics(row),
                **history_evidence(row, ctx),
                avg_cpu_pct=round(avg_cpu, 1),
                idle_windows=idle_windows,
                total_windows=total_windows,
                cost_30d_usd=row.get("cost_30d_usd"),
                # forward-spend guard: what the RECENT window shows, so the
                # detail view can see why a stopped cluster books $0.
                recent_windows=recent_windows,
                recent_idle_windows=recent_idle_windows,
                recent_active_days=recent_active_days,
                recent_cost_usd=row.get("recent_cost_usd"),
                forward_monthly_usd=round(forward_monthly, 2),
                historical_waste_usd=round(historical_waste, 2),
                still_running=still_running,
            ),
        )
