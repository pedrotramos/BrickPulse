"""Rule: task retries hidden inside eventually-successful job runs (Tier 1, B3).

A job task that fails and is retried by the job's retry
policy, then succeeds, billed compute for every failed attempt — but the RUN ends
SUCCEEDED, so ``failing_jobs`` (which prices failed runs) never sees it. A flaky task
that routinely needs two or three attempts is paying for them every day.

This is a SIBLING of ``failing_jobs``, disjoint by construction: only job runs that
end SUCCEEDED are counted here, and a run that finally failed is priced in full there.

How retries are read (verified live, see the SQL header): each attempt is its own
task ``run_id`` under the same ``(job_run_id, task_key)``, run sequentially; for_each
iterations share the key too but carry ``parent_run_id <> job_run_id`` and are
excluded. Billing exists per JOB run only, so the failed attempts' dollars are the
run's billed cost apportioned by their share of the run's task wall-time.

**Estimated saving.** The failed-attempt spend is measured and is what the bill drops
by once the retries stop, but the apportionment is by wall-time (parallel tasks on one
cluster make it approximate) and a fix to a flaky dependency is not certain, so it is
``estimated`` (shown and ranked), not booked. Only the retry rate ABOVE a healthy
baseline is credited (mirrors ``failing_jobs``), so noise-level retries surface nothing.
Promoted out of Labs 2026-09-23.
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
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block, viz_daily_stack
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.lakeflow.job_task_run_timeline (each retry attempt is its own task run_id "
    "under the same job_run_id + task_key; for_each iterations excluded via "
    "parent_run_id) + job_run_timeline (only runs that ended SUCCEEDED — failed runs "
    "belong to failing_jobs) + billing.usage per job run via usage_metadata.job_run_id "
    "x list_prices, {n}-day lookback. Billing has no per-attempt grain, so the failed "
    "attempts' cost is the run's billed cost x their share of the run's task "
    "wall-time — approximate when tasks run in parallel on one cluster. Only the retry "
    "rate above the {b:.0%} healthy baseline is credited."
)

_CODE_HINT = {
    "RUN_EXECUTION_ERROR": " The failed attempts mostly ended with RUN_EXECUTION_ERROR — "
    "the task's own code raised, then passed on a later attempt (a race, a transient "
    "source, or a non-idempotent step).",
    "INVALID_CLUSTER_REQUEST": " The failed attempts mostly ended with "
    "INVALID_CLUSTER_REQUEST — compute failed to start (capacity or a rejected spec), "
    "not the code.",
    "INTERNAL_ERROR": " The failed attempts mostly ended with INTERNAL_ERROR — often a "
    "platform or capacity hiccup; check whether it clusters at particular times.",
    "DRIVER_ERROR": " The failed attempts mostly ended with DRIVER_ERROR — usually "
    "driver memory pressure; a larger driver may remove the retries.",
}


@register
class JobTaskRetryWasteRule(Rule):
    id = "job_task_retry_waste"
    # Estimated: failed-attempt spend is measured; the apportionment is approximate.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "Task retries hidden inside successful job runs"
    resource_type = "job"
    category = "jobs"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    cloud_savings = "none"
    sql_file = "job_task_retry_waste.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        succeeded_runs = int(row.get("succeeded_runs") or 0)
        retried_runs = int(row.get("retried_runs") or 0)
        if succeeded_runs <= 0 or retried_runs < int(t.get("min_retried_runs", 5)):
            return None
        # A retry storm inside one or two days is an incident (an upstream outage),
        # not the chronic flakiness this rule is about.
        if int(row.get("retry_days") or 0) < int(t.get("min_retry_days", 3)):
            return None

        retry_rate = retried_runs / succeeded_runs
        baseline = float(t.get("baseline_retry_rate", 0.05))
        if retry_rate < float(t.get("min_retry_rate", 0.10)) or retry_rate <= baseline:
            return None

        # Measured failed-attempt spend, credited only for the retries ABOVE the
        # healthy baseline (the failing_jobs excess-over-baseline shape). No completion
        # netting is needed: the final successful attempt runs either way, and only
        # the failed attempts were priced.
        excess = min(1.0, (retry_rate - baseline) / retry_rate)
        retry_cost_monthly = to_monthly(row.get("retry_cost_30d") or 0.0, ctx.lookback_days)
        addressable = retry_cost_monthly * excess
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        failed_attempts = int(row.get("failed_attempts") or 0)
        name = row.get("job_name") or str(row["job_id"])
        task = row.get("top_task_key")
        code = row.get("top_failure_code")

        if retry_rate >= 0.5 or addressable >= 500:
            priority = RulePriority.HIGH
        elif retry_rate >= 0.25 or addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        task_clause = f" Most retries are in task '{task}'." if task else ""
        why = (
            f"Job '{name}' succeeded {succeeded_runs:,} times over the last "
            f"{ctx.lookback_days} days, but {retried_runs:,} of those runs "
            f"({retry_rate * 100:.0f}%) only got there after a task failed and was "
            f"retried — {failed_attempts:,} failed attempt(s) in total.{task_clause} "
            f"Every failed attempt billed compute: ~${retry_cost_monthly:,.0f}/mo, of "
            f"which ~${addressable:,.0f}/mo is above a healthy ~{baseline * 100:.0f}% "
            f"retry rate. These runs report SUCCEEDED, so the cost doesn't show up as "
            f"failures anywhere.{_CODE_HINT.get(str(code or ''), '')}"
        )
        how = (
            "Open a retried run (Workflows > the job > a run with multiple attempts on "
            + (f"'{task}'" if task else "the flaky task")
            + ") and read the failed attempt's error. Fix the flaky cause — a transient "
            "source or API (add backoff inside the task), a race with an upstream job "
            "(add a dependency or file-arrival trigger), or memory pressure (size the "
            "driver/workers). Keep the retry policy as a safety net, but it should rarely "
            "fire. The saving is an estimate: the dollars are measured, but a fix to a "
            "flaky dependency is not a certain one."
        )

        viz = viz_daily_stack(
            title="Successful runs per day: clean vs needed a task retry",
            unit="runs",
            series=[
                ("c", "Succeeded first time", "info"),
                ("r", "Succeeded only after a failed attempt", "warn"),
            ],
            rows=row.get("days"),
            lookback_days=ctx.lookback_days,
            note=(f"{failed_attempts:,} failed attempt(s) billed compute; a healthy "
                  f"retry rate is ~{baseline * 100:.0f}%."),
            msg="job_retry_runs",
            params={"failed_attempts": failed_attempts, "baseline_pct": round(baseline * 100)},
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked.
            monthly_savings_usd=round(addressable, 2),
            savings_status="estimated",
            monthly_savings_low_usd=round(addressable, 2),
            effort=EffortBand.MEDIUM,  # a code/dependency fix, not a toggle
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                viz=viz,
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days, b=baseline)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                succeeded_runs=succeeded_runs,
                retried_runs=retried_runs,
                retry_days=row.get("retry_days"),
                failed_attempts=failed_attempts,
                retry_rate_pct=round(retry_rate * 100, 1),
                baseline_retry_rate_pct=round(baseline * 100, 1),
                top_task_key=task,
                top_failure_code=code,
                retry_cost_30d=row.get("retry_cost_30d"),
                retry_cost_monthly_usd=round(retry_cost_monthly, 2),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
