"""Rule: fix or retire chronically-failing jobs (Gap G1 — reliability waste).

A job that fails a large fraction of its runs burns compute on nothing: every
failed run still spun up compute and billed DBUs before it raised. This rule
measures that waste directly — the REAL billed cost attributed to failed runs
via ``billing.usage.usage_metadata.job_run_id`` — and treats as recoverable
only the share attributable to *excess* failures above a healthy baseline, so a
job failing at noise level is credited ~$0 while a chronically-broken pipeline
is credited nearly all of its wasted spend.

This was a documented blind spot (Gap G1): the app had no reliability rule. The
savings are not a fraction-of-cost guess — they are measured failed-run dollars,
scaled by the observed excess-failure fraction.

The savings model splits the
finding into two actions with different booking tiers:

* **Repair** — when the job has comparable SUCCESSFUL runs to price the fixed
  state, the credit (excess-failure spend net of completion cost) is a real
  estimate, but a code fix is not a certain saving, so it is **estimated** (low 0).
* **Retire** — when the job never once succeeded in the window, there is no
  successful run to estimate a repair credit from, and retiring a job needs an
  owner/obsolescence decision the engine cannot observe. The excess-failure spend
  is surfaced as **max addressable** but books nothing — **advisory** — until an
  owner confirms the job can be retired.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_job_runs
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import failing_job_savings

_BASIS = (
    "system.lakeflow.job_run_timeline (terminal result_state per run) + "
    "billing.usage attributed per run via usage_metadata.job_run_id x list_prices, "
    "{n}-day lookback. Failed-run cost is measured, not estimated. Savings credit "
    "only the failure rate above the {b:.0%} healthy baseline, and net out the "
    "compute a fixed run still consumes once it completes successfully (estimated "
    "from this job's own successful runs) — a REPAIR estimate. A job that never "
    "succeeds has no successful run to price, so it is a RETIRE candidate: its "
    "excess-failure spend is shown as max-addressable but books nothing (advisory) "
    "until an owner confirms it can be retired. Streaming jobs (majority-CONTINUOUS "
    "trigger) are excluded, since they restart on error by design."
)


@register
class FailingJobsRule(Rule):
    id = "failing_jobs"
    # Savings model: REPAIR findings (job has comparable successful runs)
    # stay estimated; RETIRE findings (job never succeeded) drop to advisory per-finding
    # in score() — retirement needs an owner/obsolescence decision, so it books $0 and
    # shows the excess-failure spend as max-addressable only. Class default = estimated.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Fix or retire a chronically-failing job"
    resource_type = "job"
    category = "jobs"
    sql_file = "failing_jobs.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. A cluster of failures inside a single day is
        # usually ONE incident — a bad deploy, an upstream outage — not the chronic
        # pattern this rule is about, and "fix or retire this job" is the wrong
        # response to an incident. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds

        total_runs = int(row.get("total_runs") or 0)
        failed_runs = int(row.get("failed_runs") or 0)
        fail_rate = float(row.get("fail_rate") or 0.0)
        baseline = float(t.get("baseline_fail_rate", 0.05))

        # Only flag jobs failing materially above the healthy baseline.
        if fail_rate < float(t.get("min_fail_rate", 0.10)):
            return None
        if fail_rate <= baseline:
            return None

        # Real billed dollars burned on failed runs, normalized to a month.
        failed_cost_monthly = to_monthly(row.get("failed_cost_30d") or 0.0, ctx.lookback_days)
        total_cost_monthly = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)

        # Mean per-run cost of a failed vs a successful run of THIS job. Computed
        # from raw window totals (both over the same lookback), so their ratio is
        # window-agnostic and safe to combine with the monthly failed_cost above.
        # A fixed failure becomes a successful run that still bills compute, so
        # savings() nets the successful-run cost out of the credit. succeeded_runs
        # is 0 for a job that never once succeeded — savings() then falls back to
        # the retire basis (that job is a genuine retire candidate).
        succeeded_runs = int(row.get("succeeded_runs") or 0)
        failed_cost_window = float(row.get("failed_cost_30d") or 0.0)
        succeeded_cost_window = float(row.get("succeeded_cost_30d") or 0.0)
        avg_failed_run_cost = failed_cost_window / failed_runs if failed_runs > 0 else 0.0
        avg_success_run_cost = (
            succeeded_cost_window / succeeded_runs if succeeded_runs > 0 else None
        )
        savings = failing_job_savings(
            failed_run_cost=failed_cost_monthly,
            fail_rate=fail_rate,
            baseline_fail_rate=baseline,
            avg_failed_run_cost=avg_failed_run_cost,
            avg_success_run_cost=avg_success_run_cost,
        )
        if savings <= 0:
            return None

        # separate the two actions and their booking tiers.
        #   * REPAIR — the job has comparable SUCCESSFUL runs, so the fixed state can
        #     be priced (avg_success_run_cost is not None) and the credit is a real
        #     estimate (excess-failure spend net of completion cost). A code fix is
        #     not bookable with certainty, so it is ESTIMATED (low 0).
        #   * RETIRE — the job never once succeeded in the window. There is no
        #     successful run to price a repair credit, AND retiring a job requires an
        #     owner/obsolescence decision the engine cannot observe. So the
        #     excess-failure spend is shown as MAX ADDRESSABLE but books nothing:
        #     ADVISORY. (An obsolescence signal / owner sign-off is what would promote
        #     it — not anything in the system tables.)
        is_retire = avg_success_run_cost is None
        if is_retire:
            low, high, status, action = 0.0, savings, "advisory", "retire"
        else:
            low, high, status, action = 0.0, savings, "estimated", "repair"

        name = row.get("job_name") or str(row["job_id"])
        cancelled_runs = int(row.get("cancelled_runs") or 0)
        skipped_runs = int(row.get("skipped_runs") or 0)
        code = row.get("termination_code")
        # CLUSTER_ERROR and RUN_EXECUTION_ERROR need different fixes (infrastructure
        # vs code), so naming the dominant one saves the reader a round trip.
        code_hint = {
            "CLUSTER_ERROR": (
                " Most runs ended with CLUSTER_ERROR, which points at compute "
                "configuration or capacity rather than the job's code."
            ),
            "RUN_EXECUTION_ERROR": (
                " Most runs ended with RUN_EXECUTION_ERROR, i.e. the job's own code "
                "raised."
            ),
            "INVALID_CLUSTER_REQUEST": (
                " Most runs ended with INVALID_CLUSTER_REQUEST — the cluster spec is "
                "being rejected, so no work is even starting."
            ),
            "MAX_CONCURRENT_RUNS_EXCEEDED": (
                " Runs are being skipped for exceeding the concurrency limit, so the "
                "schedule is firing faster than the job completes."
            ),
        }.get(str(code or ""), "")
        # The credit nets out completion cost unless the job never succeeded (then
        # it is a retire candidate — addressable but not booked; needs an owner call).
        if is_retire:
            reclaim_clause = (
                f"This job never completed successfully in the window, so there is no "
                f"successful run to estimate a repair saving from, and retiring it is a "
                f"call only its owner can make. Up to ~${savings:,.0f}/mo of "
                f"excess-failure spend is addressable if the job is obsolete — shown "
                f"for review, not booked, until someone confirms it can be retired."
            )
        else:
            reclaim_clause = (
                f"Fixing the failures could reclaim ~${savings:,.0f}/mo — not the full "
                f"failed spend, because a fixed run still runs to completion and "
                f"bills compute (~${avg_success_run_cost:,.2f}/run here); this is the "
                f"share above baseline, net of that completion cost. Estimated (a code "
                f"fix isn't a certain saving), so it's shown but not booked."
            )
        why = (
            f"Job '{name}' failed {failed_runs} of {total_runs} runs "
            f"({fail_rate * 100:.0f}%) over the last {ctx.lookback_days} days — far "
            f"above a healthy ~{baseline * 100:.0f}% baseline. Failed runs still "
            f"billed compute: ~${failed_cost_monthly:,.0f}/mo was spent on runs that "
            f"produced nothing. {reclaim_clause}{code_hint}"
        )
        if cancelled_runs:
            # Cancellations are reported but never priced into savings: stopping a
            # job someone deliberately cancels is not an action this rule proposes.
            cancelled_cost = to_monthly(
                row.get("cancelled_cost_30d") or 0.0, ctx.lookback_days
            )
            why += (
                f" Separately, {cancelled_runs} run(s) were cancelled"
                + (
                    f" (~${cancelled_cost:,.0f}/mo of compute consumed before they "
                    f"stopped)"
                    if cancelled_cost >= 1
                    else ""
                )
                + " — not counted as failures, but worth a look if that is unexpected."
            )
        if skipped_runs:
            why += (
                f" {skipped_runs} run(s) were skipped, usually a concurrency limit "
                f"rather than a fault."
            )
        if is_retire:
            how = (
                "Confirm with the owner whether this job is still needed — it has never "
                "succeeded in the window. If it's obsolete, pause or delete it to "
                "reclaim the excess-failure spend. If it's still needed, treat it as a "
                "repair: investigate a failed run's output/logs (Workflows > the job) "
                "and fix the root cause, then add a retry/alert policy."
            )
        else:
            how = (
                "Investigate the failure cause (Workflows > the job > a failed run > "
                "output/logs). Fix the root cause, or if the job is obsolete, pause or "
                "delete it. Add a retry/alert policy so failures aren't silently retried."
            )

        # Per-day succeeded/failed buckets for the chart (defensive: SQL supplies
        # an array of {d, s, f} structs; tolerate list/dict rows or absence).
        days = _normalize_days(row.get("days"))
        viz = viz_job_runs(
            days=days,
            lookback_days=ctx.lookback_days,
            fail_rate=fail_rate,
            baseline_fail_rate=baseline,
            failed_cost=round(failed_cost_monthly, 2),
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(failed_cost_monthly, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Repair is estimated (a code fix isn't a certain saving); retire is
            # advisory (books $0 until an owner confirms the job can be retired).
            savings_status=status,
            effort=EffortBand.MEDIUM,  # a code/config fix, not a toggle
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days, b=baseline)
                + history_note(row, ctx),
                viz=viz,
                **history_evidence(row, ctx),
                # Which action this finding proposes: 'repair' (estimated) vs 'retire'
                # (advisory, needs owner sign-off) — the split.
                action=action,
                total_runs=total_runs,
                failed_runs=failed_runs,
                fail_rate_pct=round(fail_rate * 100, 1),
                # Reliability signals beyond the failure rate. Reported so the raw
                # table distinguishes a job that BREAKS from one that is constantly
                # stopped or skipped — different problems, different fixes.
                cancelled_runs=cancelled_runs,
                skipped_runs=skipped_runs,
                succeeded_runs=succeeded_runs,
                continuous_runs=int(row.get("continuous_runs") or 0),
                termination_code=code,
                cancelled_cost_30d=row.get("cancelled_cost_30d"),
                failed_cost_30d=row.get("failed_cost_30d"),
                succeeded_cost_30d=row.get("succeeded_cost_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
                # Per-run means that drive the completion-cost netting, so the raw
                # table shows WHY the credit is below the full failed spend.
                avg_failed_run_cost=round(avg_failed_run_cost, 2),
                avg_success_run_cost=(
                    round(avg_success_run_cost, 2)
                    if avg_success_run_cost is not None
                    else None
                ),
            ),
        )


def _normalize_days(days) -> list[dict]:
    """Coerce the SQL ``days`` array (structs with fields d/s/f) into plain
    dicts the viz builder expects. Tolerates None, list-of-dicts, or the
    Row/struct shape returned by the SQL connector."""
    out: list[dict] = []
    for item in days or []:
        if isinstance(item, dict):
            d, s, f = item.get("d"), item.get("s"), item.get("f")
        else:  # struct/Row with attributes or positional tuple
            d = getattr(item, "d", None)
            s = getattr(item, "s", None)
            f = getattr(item, "f", None)
            if d is None and isinstance(item, (list, tuple)) and len(item) == 3:
                d, s, f = item
        if d is None:
            continue
        out.append({"d": str(d), "s": int(s or 0), "f": int(f or 0)})
    return out
