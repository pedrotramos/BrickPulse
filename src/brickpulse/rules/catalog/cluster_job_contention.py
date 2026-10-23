"""Rule: concurrent jobs contending on a shared all-purpose cluster.

Several job runs executing AT THE SAME TIME on one all-purpose (interactive)
cluster compete for its fixed pool of cores and memory, so each runs slower than
it would in isolation. The fix is not a bigger shared cluster (that only raises
the all-purpose bill) — it is to ISOLATE the jobs onto their own job clusters,
which bill at the lower job-compute rate AND remove the contention.

This is deliberately an ADVISORY finding: ``monthly_savings_usd`` is 0. The
all-purpose -> job-compute price gap on these same clusters is already claimed by
``interactive_to_job``; booking it again here would double-count and break the
engine invariant that estimates never exceed true avoidable spend. The payoff of
acting on contention is performance/reliability, so the finding ranks on
``monthly_waste_usd`` — the all-purpose spend AT RISK during contended minutes
(spend buying self-competing throughput) — exactly as ``untagged_spend`` does.

The signal is MEASURED, not inferred: concurrency comes from
``job_task_run_timeline`` (distinct job runs live per minute) and saturation from
``node_timeline`` (cluster-wide CPU/memory that same minute). Because
``node_timeline`` is node-level, this proves "the box was saturated while N jobs
overlapped", never "job A starved job B" — a contention signal, not per-job
blame. The discriminator that separates real contention from a merely undersized
cluster is the SOLO-vs-CONCURRENT DELTA: saturation must both be high during
overlap AND rise materially above the solo level. A cluster equally busy whether
one job or several run is undersized (``rightsize``'s problem), not contended.
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
from brickpulse.rules.evidence import (
    CONFIDENCE_HIGH,
    evidence_block,
    viz_cluster_contention,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.lakeflow.job_task_run_timeline (distinct job runs live per minute) "
    "joined to system.compute.node_timeline (cluster-wide CPU/memory that minute), "
    "{n}-day lookback. Direct measurement — highest confidence tier. Contention is "
    "read as saturation that is high during overlap AND materially above the "
    "single-job (solo) level; node_timeline is node-level, so this measures the "
    "cluster being saturated while jobs overlapped, not one job starving another."
)


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


@register
class ClusterJobContentionRule(Rule):
    id = "cluster_job_contention"
    # advisory rule, $0 savings by design (interactive_to_job owns the $).
    default_savings_status = "advisory"
    version = "1.0.0"
    title = "Isolate concurrent jobs contending on a shared cluster"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "cluster_job_contention.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge the cluster's habitual pattern.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # spend-at-risk figure we rank on.
        if underpriced(row):
            return None

        t = ctx.thresholds

        peak = int(row.get("peak_concurrent_runs") or 0)
        if peak < int(t["min_concurrent_runs"]):
            return None  # never actually overlapped enough to contend

        contended_min = int(row.get("contended_minutes") or 0)
        measured_min = int(row.get("contended_minutes_measured") or 0)
        if contended_min < int(t["min_contended_minutes"]):
            return None  # overlap too rare to be a habitual pattern

        # Coverage gate: only measured contended minutes can prove saturation. A
        # cluster whose overlap we mostly could not measure (little/no
        # node_timeline) must not be flagged on faith.
        coverage = _num(row, "contended_measured_frac") or 0.0
        if measured_min < int(t["min_contended_minutes"]) or coverage < float(
            t["min_measured_frac"]
        ):
            return None

        # The discriminator: saturated DURING overlap AND materially more than solo.
        cpu_c_p95 = _num(row, "cpu_pct_concurrent_p95")
        cpu_c = _num(row, "cpu_pct_concurrent")
        cpu_solo = _num(row, "cpu_pct_solo")
        mem_c_p95 = _num(row, "mem_pct_concurrent_p95")
        mem_c = _num(row, "mem_pct_concurrent")
        mem_solo = _num(row, "mem_pct_solo")

        delta_min = float(t["min_saturation_delta_pct"])

        def _contends(peak_pct, avg_pct, solo_pct, sat_pct) -> bool:
            # Saturated at the p95 during overlap, and its average rose at least
            # delta_min points above the solo average. A missing solo baseline
            # (only ever ran concurrent) counts the whole concurrent level as rise.
            if peak_pct is None or avg_pct is None:
                return False
            if peak_pct < sat_pct:
                return False
            rise = avg_pct - (solo_pct if solo_pct is not None else 0.0)
            return rise >= delta_min

        cpu_contends = _contends(
            cpu_c_p95, cpu_c, cpu_solo, float(t["cpu_saturated_pct"])
        )
        mem_contends = _contends(
            mem_c_p95, mem_c, mem_solo, float(t["mem_saturated_pct"])
        )
        if not (cpu_contends or mem_contends):
            return None  # saturated-equally-solo (undersized) or not saturated

        # Spend at risk (ranking figure, NOT a saving): the all-purpose cost
        # attributed to contended minutes.
        monthly_at_risk = to_monthly(
            _num(row, "contended_cost_30d_usd") or 0.0, ctx.lookback_days
        )
        if monthly_at_risk < float(t["min_monthly_cost_usd"]):
            return None  # too little contended spend to be worth splitting jobs

        name = row.get("cluster_name") or str(row["cluster_id"])
        jobs = int(row.get("contended_job_count") or 0)
        avg_conc = _num(row, "avg_concurrent_runs") or 0.0

        # Name the bottleneck we actually measured, for honest copy.
        bound = "CPU and memory" if (cpu_contends and mem_contends) else (
            "CPU" if cpu_contends else "memory"
        )
        if cpu_contends:
            sat_phrase = f"CPU reached {cpu_c_p95:.0f}% during overlap vs {(cpu_solo or 0):.0f}% running solo"
        else:
            sat_phrase = f"memory reached {mem_c_p95:.0f}% during overlap vs {(mem_solo or 0):.0f}% running solo"

        why = (
            f"On all-purpose cluster '{name}', up to {peak} job runs executed at "
            f"once (averaging {avg_conc:.1f} concurrent across {contended_min} "
            f"overlapping minute(s)), and the cluster's {bound} saturated while they "
            f"competed: {sat_phrase}. {jobs} job(s) were involved. The overlapping "
            f"runs are sharing one fixed pool of {bound.replace(' and ', '/')}, so "
            f"each finishes slower than it would alone — ~${monthly_at_risk:,.0f}/mo "
            f"of all-purpose spend is buying this self-competing throughput."
        )
        how = (
            "Move the overlapping jobs onto their OWN job clusters (Workflows > each "
            "job > swap the shared all-purpose cluster for a dedicated job cluster or "
            "serverless job). Isolation removes the contention AND bills at the lower "
            "job-compute rate; serverless jobs additionally scale to zero between "
            "runs. Upsizing the shared cluster is the wrong lever — it raises the "
            "all-purpose bill without decoupling the jobs. (The all-purpose vs "
            "job-compute price saving itself is reported separately.)"
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_at_risk, 2),
            monthly_savings_usd=0.0,  # advisory: dollars are interactive_to_job's
            effort=EffortBand.HIGH,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz_cluster_contention(
                    cpu_solo=cpu_solo,
                    cpu_concurrent=cpu_c,
                    cpu_concurrent_p95=cpu_c_p95,
                    mem_solo=mem_solo,
                    mem_concurrent=mem_c,
                    mem_concurrent_p95=mem_c_p95,
                    peak_concurrent_runs=peak,
                    contended_minutes=contended_min,
                    contended_job_count=jobs,
                ),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                peak_concurrent_runs=peak,
                avg_concurrent_runs=row.get("avg_concurrent_runs"),
                contended_minutes=contended_min,
                contended_minutes_measured=measured_min,
                contended_measured_frac=row.get("contended_measured_frac"),
                job_active_minutes=row.get("job_active_minutes"),
                running_minutes=row.get("running_minutes"),
                contended_job_count=jobs,
                cpu_pct_solo=cpu_solo,
                cpu_pct_concurrent=cpu_c,
                cpu_pct_concurrent_p95=cpu_c_p95,
                mem_pct_solo=mem_solo,
                mem_pct_concurrent=mem_c,
                mem_pct_concurrent_p95=mem_c_p95,
                cost_30d_usd=row.get("cost_30d_usd"),
                contended_cost_30d_usd=row.get("contended_cost_30d_usd"),
            ),
        )
