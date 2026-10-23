"""Rule: remove or pause unused scheduled jobs.

Jobs that still exist / are scheduled but have not run recently are definition
and orchestration cruft: they may hold reserved resources or signal abandoned
pipelines. This is a hygiene recommendation, so we emit the finding even when
there is no residual cost (savings = 0), suppressing only jobs that ran recently.

NOTE — no ``insufficient_history`` guard here, deliberately. Every other rule
infers a *rate* from observed activity and needs several days for that rate to be
representative (see :mod:`finops_rules.history`). This rule keys on the OPPOSITE:
the absence of runs across a 90-day window, gated by ``min_days_since_last_run``
(default 30). Its threshold IS a history requirement — a job cannot read as stale
without 30 days of quiet — and the ``require_activity_or_cost`` guard already
drops never-run leftovers. Adding an observed-activity minimum would be
backwards: it would demand recent activity from a rule that fires on its absence.
"""

from __future__ import annotations

from datetime import date, datetime

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_STALE_DAYS = 999  # treated as "never run" when there is no last-run timestamp


def _days_since(last_run_time) -> int:
    """Days since the given last-run timestamp.

    Handles a datetime, a date, an ISO-ish string (uses the leading date
    portion), or None. Returns a large value when it can't be determined so the
    job reads as very stale."""
    if last_run_time is None:
        return _STALE_DAYS

    ref: date | None = None
    if isinstance(last_run_time, datetime):
        ref = last_run_time.date()
    elif isinstance(last_run_time, date):
        ref = last_run_time
    elif isinstance(last_run_time, str):
        text = last_run_time.strip()
        if not text:
            return _STALE_DAYS
        # Take the date portion of things like "2026-05-01 12:00:00" / "2026-05-01T..".
        date_part = text.replace("T", " ").split(" ", 1)[0]
        try:
            ref = date.fromisoformat(date_part)
        except ValueError:
            return _STALE_DAYS
    else:
        return _STALE_DAYS

    return (date.today() - ref).days


@register
class UnusedScheduledJobsRule(Rule):
    id = "unused_scheduled_jobs"
    # it books stale projected cost as savings; advisory until rebuilt.
    default_savings_status = "advisory"
    version = "1.0.0"
    title = "Remove or pause unused scheduled jobs"
    resource_type = "job"
    # "Saved so far" (finops_rules.change_tracking): the job was paused (deletion is
    # generic). The "from" value is the tracker's own observation of ``paused``.
    # NB: this rule is advisory today, so nothing it flags is tracked until it books.
    tracked_setting = TrackedSetting(
        resource="job", column="paused", proposed_value=True, direction="equals",
        valuation="binary", reached_kind="paused", label="Schedule paused",
    )
    category = "jobs"
    sql_file = "unused_scheduled_jobs.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        t = ctx.thresholds

        days_since = _days_since(row.get("last_run_time"))
        if days_since < t["min_days_since_last_run"]:
            return None  # ran recently => still in use

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        # Hygiene rule: emit even with zero residual cost. Any residual cost is
        # fully avoidable if the job is confirmed unused.
        savings = monthly_cost if monthly_cost > 0 else 0.0

        # Noise guard (config-tunable, default on): a job with NO recorded run in
        # the activity window AND no residual cost is just a leftover/never-used
        # definition, not an actionable cost finding. Emitting one per such job
        # floods the UI (a large workspace can have tens of thousands). We still
        # surface jobs that were active then went stale, or that still cost money.
        # Set require_activity_or_cost: false to get the full hygiene list.
        has_activity = row.get("last_run_time") is not None or int(row.get("runs_lookback") or 0) > 0
        if t.get("require_activity_or_cost", True) and not has_activity and savings <= 0:
            return None

        last_run_desc = "never (no run recorded)" if row.get("last_run_time") is None \
            else f"{days_since} days ago"
        why = (
            f"Job '{row.get('job_name') or row['job_id']}' still exists/scheduled but "
            f"last ran {last_run_desc}. The 90-day run history contains "
            f"{int(row.get('runs_lookback') or 0)} run(s), and billing shows "
            f"~${monthly_cost:,.0f}/mo of residual cost. The system tables cannot tell "
            f"whether the job is intentionally dormant, so confirm with its owner."
        )
        how = (
            "Confirm the job is unused, then pause or delete it "
            "(Workflows > select the job > pause or delete)."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=row.get("job_name") or str(row["job_id"]),
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            effort=EffortBand.LOW,
            priority=RulePriority.LOW,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=(
                    "system.lakeflow.job_run_timeline, 90-day closed run-history window, "
                    f"plus billing.usage x list_prices over {ctx.lookback_days} days. "
                    "Absence of runs does not prove the job is obsolete."
                ),
                last_run_time=str(row.get("last_run_time")),
                days_since_last_run=days_since,
                runs_lookback=row.get("runs_lookback"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )
