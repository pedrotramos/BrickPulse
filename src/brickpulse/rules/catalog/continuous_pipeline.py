"""Rule: move an idle continuous pipeline to a triggered schedule.

Closes the largest completely-unruled slice of spend. Verified on a live account:
359 DLT pipelines billing ~$16.4k/mo with no rule looking at any of them — and
within that, 3 continuous pipelines carried 72% of DLT spend. One billed 729 of
729 possible hours off 108 updates.

A continuous pipeline keeps its compute up permanently, so it bills around the
clock whether or not data arrives. A triggered pipeline bills only while an update
runs. The waste is therefore the hours that were billed with no update in flight,
which the SQL COUNTS from two system tables rather than inferring from a proxy —
so this rule sits in the high-confidence tier alongside
:mod:`~finops_rules.catalog.idle_serving_endpoints`.

The trade-off is latency, and it belongs to the reader: triggered means data lands
when the next trigger fires rather than seconds after arrival. The finding states
the measured update cadence so an admin can judge that, instead of being told to
flip a switch.
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
    calendar_grid,
    hourly_states_to_grid,
    viz_hour_heatmap,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import continuous_pipeline_savings

_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_BASIS = (
    "system.billing.usage (DLT hours billed) vs "
    "system.lakeflow.pipeline_update_timeline (hours an update was actually in "
    "flight), {n}-day lookback. Idle hours are COUNTED from both sources, not "
    "inferred — the highest confidence tier. Whole hours only, so a partly-active "
    "hour counts as fully active and the saving rounds down. 'Idle' means no update "
    "was in flight, not that no input data arrived: telling those apart would need "
    "per-pipeline Pipeline Events, which are not exposed as a system table, so the "
    "saving stays estimated and genuinely-streaming pipelines are excluded by the "
    "idle-share gate instead."
)


@register
class ContinuousPipelineRule(Rule):
    id = "continuous_pipeline"
    # Savings model: stays ESTIMATED — not bookable, and out of the booked
    # headline. The idle hours are MEASURED (COUNTED from billing.usage vs
    # pipeline_update_timeline, not inferred from a proxy), but the *saving* is a
    # counterfactual: that a triggered schedule would not bill those idle hours, net
    # of a modelled per-run start-up overhead. A measured quantity feeding a modelled
    # counterfactual is an estimate, so estimated is the honest tier.
    #   MEASUREMENT CEILING: "idle" here means no update was in flight, NOT that no
    #   input data arrived. Separating a genuinely no-input hour from useful-but-quiet
    #   processing would need per-pipeline input-row metrics, which live only in each
    #   pipeline's own Pipeline Events event_log — there is no account-wide system
    #   table for them (confirmed absent from system.lakeflow). So this rule does not
    #   attempt that distinction; it suppresses genuinely-streaming pipelines instead,
    #   via the min_idle_hours_frac gate, and states the measured cadence for the
    #   reader to judge the latency trade-off.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Switch idle continuous pipelines to triggered"
    resource_type = "pipeline"
    category = "pipelines"
    sql_file = "continuous_pipeline.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A pipeline seen for a day or two has no established rhythm: the idle share
        # of one afternoon says nothing about the month. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        billed_hours = float(row.get("billed_hours") or 0.0)
        active_hours = float(row.get("active_hours") or 0.0)
        if billed_hours <= 0:
            return None

        idle_share = max(0.0, billed_hours - active_hours) / billed_hours
        # A pipeline whose updates cover most of its billed window is genuinely
        # streaming: continuous is the correct setting, and switching would break
        # the latency requirement it exists to meet. Nothing to recommend.
        if idle_share < float(t.get("min_idle_hours_frac", 0.5)):
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(t.get("min_monthly_cost_usd", 100)):
            return None

        # Serverless DLT starts in seconds, so the per-run start-up penalty is much
        # smaller than for classic compute. Pick the FLOOR overhead by
        # compute type; is_serverless is the measured flag the SQL already emits.
        is_serverless = bool(row.get("is_serverless"))
        floor_overhead = float(
            t.get("startup_overhead_frac_serverless", 0.05) if is_serverless
            else t.get("startup_overhead_frac", 0.15)
        )
        # Cadence-aware overhead: a frequently-triggered
        # pipeline pays more in per-run start-up than a flat fraction assumes. Estimate the
        # start-up cost from the measured update cadence (updates x minutes-per-start) as a
        # share of the idle time a triggered schedule would reclaim, and take the MAX with
        # the floor — so the overhead can only RISE (savings only fall) for a churny
        # pipeline, never drop below the conservative floor.
        idle_hours_gross = max(0.0, billed_hours - active_hours)
        updates = int(row.get("updates_30d") or 0)
        startup_min = float(
            t.get("startup_minutes_serverless", 0.5) if is_serverless
            else t.get("startup_minutes_classic", 7.0)
        )
        cadence_overhead = (
            (updates * startup_min) / (idle_hours_gross * 60.0)
            if idle_hours_gross > 0 else 0.0
        )
        startup_overhead = min(1.0, max(floor_overhead, cadence_overhead))
        _grid = calendar_grid(
            [int(x) for x in (row.get("hourly_states") or [])], row.get("grid_start")
        )
        savings, idle_hours = continuous_pipeline_savings(
            billed_hours=billed_hours,
            active_hours=active_hours,
            monthly_cost=monthly_cost,
            startup_overhead_frac=startup_overhead,
        )
        if savings < float(t.get("min_savings_usd", 25)):
            return None

        name = row.get("pipeline_name") or str(row["pipeline_id"])
        updates = int(row.get("updates_30d") or 0)
        active_days = int(row.get("active_days") or 0)
        avg_minutes = row.get("avg_update_minutes")
        # Cadence in plain terms, so the latency trade-off is legible.
        per_day = updates / max(1, active_days)
        cadence = (
            f"{per_day:.1f} update(s) per active day"
            if per_day >= 1
            else f"{updates} update(s) across {active_days} active day(s)"
        )

        why = (
            f"Pipeline '{name}' runs in CONTINUOUS mode, so its compute stays up and "
            f"billing around the clock: it billed {billed_hours:,.0f} hours in the last "
            f"{ctx.lookback_days} days but an update was only in flight for "
            f"{active_hours:,.0f} of them — {idle_hours:,.0f} idle hours "
            f"({idle_share * 100:.0f}% of the time it was paid for). It spends "
            f"~${monthly_cost:,.0f}/mo, of which ~${savings:,.0f}/mo is the idle "
            f"portion a triggered schedule would not pay for. Measured cadence: "
            f"{cadence}."
        )
        how = (
            "If the data does not need sub-minute freshness, switch the pipeline to "
            "triggered and give it a schedule matching how often data actually "
            "arrives (Pipelines > select the pipeline > Settings > uncheck "
            "Continuous, then set a schedule). Continuous is the right choice when "
            "latency genuinely matters — check the freshness requirement with the "
            "pipeline's owner before changing it."
        )
        if row.get("run_as"):
            how += f" This pipeline runs as {row['run_as']}."

        # Priority on the size of the recoverable spend: effort is identical across
        # findings (a settings toggle plus a schedule), so money is what should order
        # them.
        if savings >= float(t.get("high_priority_savings_usd", 500)):
            priority = RulePriority.HIGH
        elif savings >= float(t.get("medium_priority_savings_usd", 100)):
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="pipeline",
            resource_id=str(row["pipeline_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            # A settings toggle, but it changes data freshness — so it needs a
            # conversation with the owner, unlike a pure auto-stop change.
            effort=EffortBand.LOW,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=(
                    _BASIS.format(n=ctx.lookback_days)
                    + history_note(row, ctx)
                    + coverage_note(row)
                ),
                # Day x hour grid of what this pipeline was doing while billing.
                # Reuses the SAME `hour_heatmap` chart and state encoding as the
                # warehouse auto-stop rule (0=not billed, 1=update in flight,
                # 2=billed but idle), so the reader sees the 86%-idle claim rather
                # than being asked to take two numbers on trust. Omitted when the
                # series is absent — a blank grid would read as "no data" and is
                # worse than the text alone.
                viz=(
                    viz_hour_heatmap(
                        grid=_grid[0],
                        day_labels=_DAY_LABELS,
                        reclaimable_hours=idle_hours,
                        context="pipeline",
                        day_dates=_grid[1],
                    )
                    if row.get("hourly_states")
                    else None
                ),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                billed_hours=round(billed_hours, 1),
                active_hours=round(active_hours, 1),
                idle_hours=idle_hours,
                idle_pct=round(idle_share * 100, 1),
                updates_30d=updates,
                active_days=active_days,
                avg_update_minutes=avg_minutes,
                failed_updates=int(row.get("failed_updates") or 0),
                is_serverless=row.get("is_serverless"),
                startup_overhead_frac=startup_overhead,
                # Auditability for finding #9: the floor and the cadence-derived
                # estimate whose MAX became the effective overhead above.
                startup_overhead_floor=floor_overhead,
                cadence_startup_overhead=round(cadence_overhead, 4),
                edition=row.get("edition"),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
