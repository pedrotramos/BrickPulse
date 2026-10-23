"""Rule: hung / runaway job runs on jobs without a (useful) timeout (Tier 1, B2).

EXPERIMENTAL (Labs tab). A recurring job whose runs normally take minutes but
occasionally hang for hours keeps paying for compute until someone notices. A
run-level ``timeout_seconds`` stops it automatically; 84% of jobs in the verified
account have none.

**Detection.** Per job (>= ``min_runs`` runs in the window), a run is an OUTLIER when
its wall-time exceeds ``outlier_multiple`` x the job's p50, ``min_outlier_seconds``,
AND the job's p90. The job qualifies when:

* it has no run bound — no job-level timeout and not every task has one — or its
  bound is loose (> ``tighten_multiple`` x the p95 of its normal runs), in which case
  the recommendation is to TIGHTEN it;
* it is not streaming (job trigger CONTINUOUS, majority-continuous runs, or a
  streaming-backlog health rule) — those run forever by design;
* **bimodal guard:** p90/p50 <= ``max_p90_p50_ratio``. A wider spread means the job
  has two normal duration modes (a month-end run) and "5x the median" is not a hang.
  EXCEPTION, from inspecting the top jobs live: the worst offenders (a per-minute job
  with ~10% of runs lasting hours) all trip the plain guard, and their long runs end
  CANCELLED/ERROR — a hang, not a second workload mode. So a wide-spread job still
  qualifies when >= ``min_unsuccessful_outlier_share`` of its outliers ended badly,
  and only those unsuccessful outliers are priced.

**Pricing (advisory, $0 booked).** Per outlier run the SQL takes the SMALLER of its
billed cost above the job's median run cost and its billed cost x (wall-time past the
recommended timeout / wall-time). The first term matters: live, many long serverless
runs billed the SAME as a normal run (waiting, not computing), so a pure duration
share would credit dollars a timeout never saves. Whether killing the run really saves
it depends on the run being genuinely stuck (a slow run that would have succeeded is
re-run), so the figure is ADDRESSABLE spend, not a booked saving.
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
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.lakeflow.job_run_timeline (run wall-time = span of the run's hourly slices; "
    "outlier = > {mult:g}x the job's p50, > {min_h:g}h and > its p90) + lakeflow.jobs / "
    "job_tasks (current timeout_seconds, health_rules) + system.billing.usage x "
    "list_prices per run (usage_metadata.job_run_id), {n}-day lookback. The excess per "
    "outlier run is the smaller of its cost above the job's median run cost and its "
    "cost past the recommended timeout, so long runs that bill no more than a normal run "
    "credit nothing. Runs on all-purpose clusters carry no per-run billing and are not "
    "priced. ADDRESSABLE spend — a timeout only saves it if the run was truly stuck — "
    "so nothing is booked. Experimental (Labs) — validate before trusting."
)

_UNSUCCESSFUL = "CANCELLED/ERROR/TIMED_OUT"


def _hours(seconds: float) -> str:
    h = seconds / 3600.0
    return f"{h:,.1f}h" if h < 48 else f"{h / 24:,.1f} days"


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60.0)):,} min"


def _run_bound(row: dict) -> float | None:
    """The longest a run can currently last, or None when nothing bounds it.

    A job-level ``timeout_seconds`` bounds the whole run. Failing that, a run is
    bounded only when EVERY task carries its own timeout (their sum is the bound for
    tasks in series — an over-estimate for parallel tasks, which errs toward
    recommending a tighter job-level timeout, the safe direction). Health rules only
    alert, so they never count as a bound."""
    job_timeout = float(row.get("job_timeout_s") or 0)
    if job_timeout > 0:
        return job_timeout
    tasks = int(row.get("task_count") or 0)
    if tasks > 0 and int(row.get("tasks_with_timeout") or 0) >= tasks:
        return float(row.get("task_timeout_sum_s") or 0) or None
    return None


@register
class JobRunawayNoTimeoutRule(Rule):
    id = "job_runaway_no_timeout"
    experimental = True
    # Advisory: the outlier runs are measured, but whether killing them saves the spend
    # depends on the run being genuinely stuck rather than slow-but-productive.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Set a timeout on jobs with hung / runaway runs"
    resource_type = "job"
    category = "jobs"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "job_runaway_no_timeout.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        total_runs = int(row.get("total_runs") or 0)
        # Streaming jobs run forever by design; a "runaway" there is normal operation.
        if (row.get("job_trigger_type") or "").upper() == "CONTINUOUS":
            return None
        if row.get("has_streaming_health_rule"):
            return None
        continuous = int(row.get("continuous_runs") or 0)
        if total_runs and continuous / total_runs > float(t.get("max_continuous_run_share", 0.5)):
            return None

        normal_p95 = float(row.get("normal_p95_s") or 0.0)
        rec_timeout = float(row.get("rec_timeout_s") or 0.0)
        if rec_timeout <= 0:
            return None

        # An existing bound that is reasonably tight is working — nothing to add. A
        # loose one (far above the job's normal range) is a "tighten" recommendation.
        bound = _run_bound(row)
        tighten_multiple = float(t.get("tighten_multiple", 10))
        if bound is not None:
            if bound <= tighten_multiple * max(normal_p95, 1.0) or rec_timeout >= bound:
                return None
            mode = "tighten"
        else:
            mode = "missing"

        # Bimodal guard (see module docstring): a wide normal spread is a second duration
        # mode, unless the long runs mostly ended badly — which is a hang.
        outliers = int(row.get("outlier_runs") or 0)
        unsuccessful = int(row.get("outlier_unsuccessful_runs") or 0)
        p50 = float(row.get("p50_s") or 0.0)
        p90 = float(row.get("p90_s") or 0.0)
        spread = (p90 / p50) if p50 > 0 else float("inf")
        unsuccessful_share = (unsuccessful / outliers) if outliers else 0.0
        hang_exemption = False
        if spread > float(t.get("max_p90_p50_ratio", 3.0)):
            if unsuccessful_share < float(t.get("min_unsuccessful_outlier_share", 0.5)):
                return None
            hang_exemption = True
            excess_window = float(row.get("excess_cost_unsuccessful_30d") or 0.0)
            priced_runs = unsuccessful
        else:
            excess_window = float(row.get("excess_cost_30d") or 0.0)
            priced_runs = outliers
        if priced_runs < int(t.get("min_outlier_runs", 2)):
            return None

        addressable = to_monthly(excess_window, ctx.lookback_days)
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        addressable = min(addressable, monthly_cost)
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        name = row.get("job_name") or str(row["job_id"])
        max_outlier = float(row.get("max_outlier_s") or 0.0)
        excess_wall = float(row.get("excess_wall_s") or 0.0)

        if addressable >= 500:
            priority = RulePriority.HIGH
        elif addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        which = (
            f"{unsuccessful:,} of those ended {_UNSUCCESSFUL} — stuck runs that were "
            f"eventually killed or crashed, not a slower second workload"
            if hang_exemption
            else f"{unsuccessful:,} of them ended {_UNSUCCESSFUL}"
        )
        if mode == "missing":
            bound_clause = "The job has no run timeout, so nothing stopped them."
        else:
            bound_clause = (
                f"Its current timeout ({_hours(bound)}) is more than "
                f"{tighten_multiple:g}x its normal run length, so it barely limits them."
            )
        why = (
            f"Job '{name}' usually finishes in ~{_minutes(p50)} (p95 of normal runs "
            f"~{_minutes(normal_p95)}), but {outliers:,} of its {total_runs:,} runs over "
            f"the last {ctx.lookback_days} days ran far longer — up to "
            f"{_hours(max_outlier)}; {which}. {bound_clause} The compute billed past a "
            f"~{_minutes(rec_timeout)} timeout is worth ~${addressable:,.0f}/mo."
        )
        if mode == "missing":
            action = f"set a run timeout of ~{_minutes(rec_timeout)}"
        else:
            action = f"tighten the run timeout to ~{_minutes(rec_timeout)}"
        how = (
            f"In Workflows > the job > Job details, {action} (about "
            f"{float(t.get('timeout_p95_multiple', 3)):g}x its normal p95, so healthy runs "
            f"are never cut), and add a RUN_DURATION_SECONDS health rule to be alerted "
            f"before it fires. Then open one of the long runs to see where it hangs (a "
            f"stuck external call, a lock, an upstream wait). No saving is booked: a "
            f"timeout only saves the spend if those runs were truly stuck — a slow run "
            f"that would have succeeded just gets re-run."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW,  # one job setting
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(
                    mult=float(t.get("outlier_multiple", 5)),
                    min_h=float(t.get("min_outlier_seconds", 3600)) / 3600.0,
                    n=ctx.lookback_days,
                )
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                timeout_mode=mode,
                total_runs=total_runs,
                outlier_runs=outliers,
                outlier_unsuccessful_runs=unsuccessful,
                outlier_billed_runs=row.get("outlier_billed_runs"),
                hang_exemption=hang_exemption,
                p50_seconds=p50,
                p90_seconds=p90,
                normal_p95_seconds=normal_p95,
                p90_p50_ratio=None if spread == float("inf") else round(spread, 2),
                recommended_timeout_seconds=round(rec_timeout),
                current_run_bound_seconds=bound,
                job_timeout_seconds=row.get("job_timeout_s"),
                tasks_with_timeout=row.get("tasks_with_timeout"),
                task_count=row.get("task_count"),
                has_duration_health_rule=bool(row.get("has_duration_health_rule")),
                max_outlier_seconds=max_outlier,
                excess_wall_hours=round(excess_wall / 3600.0, 1),
                median_run_usd=row.get("median_run_usd"),
                outlier_cost_30d_usd=row.get("outlier_cost_30d"),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
                top_outlier_runs=row.get("top_outlier_runs"),
            ),
        )
