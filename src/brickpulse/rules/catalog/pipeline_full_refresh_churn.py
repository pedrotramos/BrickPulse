"""Rule: pipelines doing frequent full refreshes (Tier 1, B4).

EXPERIMENTAL (Labs tab). A Lakeflow / DLT full refresh recomputes every table (or
every *selected* table) from scratch, while an incremental update processes only new
data. A pipeline that keeps full-refreshing — a ``full_refresh`` flag left on in the
job or orchestrator, or a checkpoint / schema problem forcing a reset each time — pays
the from-scratch price over and over for work an incremental update would do.

How a full refresh shows up (verified live in ``system.lakeflow.pipeline_update_timeline``):
``update_type = 'FULL_REFRESH'`` for the whole pipeline, or ``update_type = 'REFRESH'``
with a non-empty ``full_refresh_selection`` for a selective one. Each update is priced
from ``billing.usage.usage_metadata.dlt_update_id``.

**The counterfactual is the pipeline's own median incremental update.** The excess is
the sum over full-refresh updates of ``cost - median completed incremental cost`` (never
negative). A pipeline with no completed incremental updates has nothing to compare
against, so the saving can't be priced and it is suppressed.

This COMPLEMENTS ``pipeline_failed_updates`` (compute burned on FAILED updates) and
``continuous_pipeline`` (an always-on pipeline idling): neither sees a full refresh that
succeeds. Lakebase synced-table pipelines (``DATABASE_TABLE_SYNC``) full-refresh on
every snapshot sync by design and are skipped. Continuous pipelines are skipped too —
their "update" is one long-running stream, so a per-update baseline means nothing.

**Advisory, $0 booked.** Some full refreshes are deliberate (a backfill, a logic change
that must rewrite history), and whether the next update could have been incremental is
not in system tables. The figure is ADDRESSABLE spend. Experimental (Labs, see
``Rule.experimental``) — out of the headline entirely.
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
    "system.lakeflow.pipeline_update_timeline (update_type = 'FULL_REFRESH', or a "
    "REFRESH with a non-empty full_refresh_selection = a full refresh; a REFRESH with no "
    "selection = incremental) + system.billing.usage x list_prices priced per update via "
    "usage_metadata.dlt_update_id, {n}-day lookback. Excess = each full refresh's cost "
    "minus this pipeline's median COMPLETED incremental update cost. Full refreshes are "
    "directly counted; whether each could have been incremental is not recorded, so the "
    "figure is ADDRESSABLE spend and nothing is booked. Experimental (Labs) — validate "
    "before trusting."
)

# Trigger types that mean "something automated asked for a full refresh" — the fix is
# in the job / orchestrator config, not a person's habit.
_AUTOMATED = ("fr_job_task", "fr_api_call")

# Pipeline types whose full refreshes are the product working as designed, not churn:
# a Lakebase synced table in SNAPSHOT mode full-refreshes on every sync (verified live:
# 35 of 41 updates for each synced table), and the lever there is the sync mode, not
# a flag in a scheduler.
_BY_DESIGN_TYPES = frozenset({"DATABASE_TABLE_SYNC"})


@register
class PipelineFullRefreshChurnRule(Rule):
    id = "pipeline_full_refresh_churn"
    experimental = True
    # Advisory: the full refreshes are measured, but whether each one was avoidable
    # (vs a deliberate backfill) is not in system tables. Candidate for estimated on
    # promotion out of Labs.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Pipeline keeps doing full refreshes"
    resource_type = "pipeline"
    category = "pipelines"
    contract_pricing = "none"  # $0 advisory: no booked dollar figure to re-price
    cloud_savings = "none"
    sql_file = "pipeline_full_refresh_churn.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        full_refreshes = int(row.get("full_refreshes") or 0)
        if full_refreshes < int(t.get("min_full_refreshes", 2)):
            return None  # one reset is normal operation, not churn
        if row.get("pipeline_type") in _BY_DESIGN_TYPES:
            return None  # full refresh is how a snapshot sync works
        if row.get("is_continuous"):
            return None  # one long-running update: no per-update baseline
        incremental = int(row.get("incremental_priced") or 0)
        median_inc = row.get("median_incremental_cost_usd")
        if median_inc is None or incremental < int(t.get("min_incremental_updates", 3)):
            return None  # nothing incremental to compare against: can't be priced

        excess_window = max(0.0, float(row.get("excess_cost_usd") or 0.0))
        fr_cost_window = max(0.0, float(row.get("full_refresh_cost_usd") or 0.0))
        excess_window = min(excess_window, fr_cost_window)
        addressable = to_monthly(excess_window, ctx.lookback_days)
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        name = row.get("pipeline_name") or str(row["pipeline_id"])
        median_inc = float(median_inc)
        median_fr = float(row.get("median_full_refresh_cost_usd") or 0.0)
        automated = sum(int(row.get(k) or 0) for k in _AUTOMATED)
        manual = int(row.get("fr_user_action") or 0)
        retries = int(row.get("fr_retry") or 0)
        selective = int(row.get("selective_full_refreshes") or 0)
        fr_days = int(row.get("fr_days") or 0)
        job_id = row.get("fr_job_id")

        if full_refreshes >= 10 or addressable >= 500:
            priority = RulePriority.HIGH
        elif full_refreshes >= 4 or addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        multiple = (median_fr / median_inc) if median_inc > 0 else None
        multiple_clause = (
            f" A typical full refresh costs ~${median_fr:,.2f} against ~${median_inc:,.2f} "
            f"for an incremental update ({multiple:,.0f}x)."
            if multiple and multiple >= 1.5
            else ""
        )
        if automated >= max(1, full_refreshes // 2):
            source_clause = (
                f" {automated} of them were requested by a job or API caller"
                + (f" (job {job_id})" if job_id else "")
                + ", so a full-refresh flag is probably set in the scheduler."
            )
        elif manual:
            source_clause = f" {manual} were started by hand from the UI."
        else:
            source_clause = ""
        why = (
            f"Pipeline '{name}' ran {full_refreshes} full refresh(es) on {fr_days} day(s) "
            f"in the last {ctx.lookback_days} days"
            + (f" ({selective} selective)" if selective else "")
            + f", recomputing its tables from scratch each time.{multiple_clause}"
            f"{source_clause} Doing that work incrementally would have cost "
            f"~${addressable:,.0f}/mo less."
        )
        how_parts = [
            "Check the job task or orchestrator that triggers this pipeline and turn off "
            "'Full refresh' (full_refresh: true / full_refresh_selection) unless a backfill "
            "genuinely needs it.",
            "If the full refreshes are forced — the incremental update failing on a schema "
            "change, a lost or incompatible checkpoint, a source that was rewritten — fix "
            "that root cause instead.",
        ]
        if retries:
            how_parts.append(
                f"{retries} full refresh(es) were automatic retries of a failed one: look at "
                "the first failure's error, not the retries."
            )
        if row.get("is_development"):
            how_parts.append(
                "The pipeline is in development mode; full refreshes during development "
                "are common — confirm this isn't just active iteration."
            )
        how_parts.append(
            "No saving is booked: a deliberate backfill or logic change that must rewrite "
            "history is a legitimate full refresh."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="pipeline",
            resource_id=str(row["pipeline_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW if automated else EffortBand.MEDIUM,
            priority=priority,
            why=why,
            how=" ".join(how_parts),
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                updates=row.get("updates"),
                full_refreshes=full_refreshes,
                selective_full_refreshes=selective,
                full_refresh_days=fr_days,
                full_refresh_triggers={
                    "job_task": int(row.get("fr_job_task") or 0),
                    "api_call": int(row.get("fr_api_call") or 0),
                    "user_action": manual,
                    "retry_on_failure": retries,
                },
                full_refresh_job_id=job_id,
                full_refreshes_priced=row.get("fr_priced"),
                incremental_updates=row.get("incremental_updates"),
                incremental_updates_priced=incremental,
                median_incremental_cost_usd=round(median_inc, 4),
                median_full_refresh_cost_usd=round(median_fr, 4),
                full_refresh_cost_usd=row.get("full_refresh_cost_usd"),
                excess_cost_usd=row.get("excess_cost_usd"),
                addressable_monthly_usd=round(addressable, 2),
                pipeline_type=row.get("pipeline_type"),
                is_development=row.get("is_development"),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
