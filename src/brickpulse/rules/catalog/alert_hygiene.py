"""Rule: SQL alert hygiene — erroring, unheard, or keeping a warehouse warm (Tier 2, C3).

Three signals on a live, unpaused SQL alert, one finding per
alert (the most urgent signal leads the copy; all of them go in ``evidence.issues``):

(a) **Always erroring** — every evaluation since its last successful one failed, for at
    least ``min_error_streak_days`` distinct days. The alert protects nothing.
(b) **Nobody notified** — no subscriptions, not driven by a user job (a job with an
    alert task may notify on the result itself), and no evaluation in the window
    notified anyone. When it triggers, nobody hears.
(c) **Keeping a warehouse warm** — billed warehouse-hours in which the only statements
    running were alert evaluations. A 1–5-minute schedule on a warehouse whose
    auto-stop is longer than the interval keeps it up around the clock for a few
    seconds of query time. ``warehouse_autostop*`` counts those evaluations as busy
    time, and ``warehouse_keepalive_queries`` does not treat them as trivial (they are
    real SELECTs), so only this rule can see it.

**Estimated saving where there are dollars.** The figure is the alert's share of its
warehouse's alert-only billed hours (split by statement count per hour), less one
auto-stop tail per contiguous run of those hours — what the bill drops by if the alert
stops keeping the warehouse up — so it is the finding's ``estimated`` saving (shown and
ranked, not booked). (a)/(b) on a warehouse that is busy anyway carry no warm-hours cost
— the alert's own query time is negligible — so those findings stay ``advisory`` at $0:
the value is a broken or unheard monitor, not dollars. Promoted out of Labs 2026-09-23.
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
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_daily_stack,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.alert.alerts (latest row: subscriptions, schedule, compute) + "
    "system.alert.alert_evaluation_history (error_code, notified_subscriptions, "
    "task_key) over a {n}-day lookback. Warm hours: system.query.history "
    "(query_source.alert_id) classifies each billed hour of the alert's warehouse "
    "(system.billing.usage x list_prices); an hour counts when alert evaluations were "
    "the only statements running. The alert's share is its statement count in those "
    "hours, less one auto-stop tail per contiguous run — an ESTIMATED saving, not a "
    "booked one: an alert that matters may need a shorter schedule, not deletion. "
    "Job wiring is read from task_key ('evaluate_alert' = the alert's own schedule)."
)

ISSUE_ERRORING = "always_erroring"
ISSUE_UNNOTIFIED = "nobody_notified"
ISSUE_WARM = "keeps_warehouse_warm"


def _cron_hint(cron: str | None) -> str:
    return f" (schedule `{cron}`)" if cron else ""


@register
class AlertHygieneRule(Rule):
    id = "alert_hygiene"
    # Estimated where the alert holds its warehouse up; per-finding advisory at $0.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "SQL alert erroring, unheard, or keeping a warehouse warm"
    resource_type = "alert"
    category = "sql"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    cloud_savings = "none"
    sql_file = "alert_hygiene.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds
        history_days = int(t.get("history_days", 14))
        evaluations = int(row.get("evaluations") or 0)
        if evaluations <= 0:
            return None

        issues: list[str] = []

        streak = int(row.get("error_streak_days") or 0)
        if streak >= int(t.get("min_error_streak_days", 7)) and bool(
            row.get("last_evaluation_errored")
        ):
            issues.append(ISSUE_ERRORING)

        subs = int(row.get("subscription_count") or 0)
        user_job = int(row.get("user_job_evaluations") or 0)
        notifying = int(row.get("notifying_evaluations") or 0)
        if subs == 0 and user_job == 0 and notifying == 0:
            issues.append(ISSUE_UNNOTIFIED)

        # Warm-hours cost: the alert's share of the warehouse's alert-only hours, less
        # one auto-stop tail per contiguous run (billed anyway after the last real work).
        # Unpriced usage makes the figure unreliable, so it is dropped, not guessed.
        warm_hours = int(row.get("warm_hours") or 0)
        warm_days = int(row.get("warm_days") or 0)
        alert_warm = float(row.get("warm_cost_usd") or 0.0)
        wh_warm = float(row.get("wh_alert_only_cost_usd") or 0.0)
        wh_hours = int(row.get("wh_alert_only_hours") or 0)
        islands = int(row.get("wh_alert_only_islands") or 0)
        auto_stop = float(row.get("auto_stop_minutes") or 0.0)
        addressable = 0.0
        if alert_warm > 0 and wh_warm > 0 and wh_hours > 0 and not underpriced(row):
            fraction = min(alert_warm / wh_warm, 1.0)
            tail_hours = min(auto_stop / 60.0, 1.0) * islands
            wh_net = max(0.0, wh_warm - tail_hours * (wh_warm / wh_hours))
            addressable = to_monthly(wh_net * fraction, history_days)
        if (
            warm_hours >= int(t.get("min_warm_hours", 24))
            and warm_days >= int(t.get("min_warm_days", 3))
            and addressable >= float(t.get("min_warm_addressable_usd", 25))
        ):
            issues.append(ISSUE_WARM)

        if not issues:
            return None

        name = row.get("display_name") or str(row["alert_id"])
        wh_name = row.get("warehouse_name") or row.get("warehouse_id") or "its warehouse"
        cron = row.get("cron")
        triggered = int(row.get("triggered_evaluations") or 0)
        statements = int(row.get("alert_statements") or 0)
        wh_monthly = to_monthly(float(row.get("wh_cost_window_usd") or 0.0), history_days)
        warm = ISSUE_WARM in issues

        why_parts: list[str] = []
        how_parts: list[str] = []
        if ISSUE_ERRORING in issues:
            code = row.get("last_error_code") or "an error"
            msg = (row.get("last_error_message") or "").strip()
            msg_clause = f": \"{msg[:160]}\"" if msg else ""
            why_parts.append(
                f"Alert '{name}' has failed every evaluation for {streak} days "
                f"({int(row.get('errored_evaluations') or 0):,} of {evaluations:,} "
                f"evaluations in the last {history_days} days errored; latest "
                f"{code}{msg_clause}). It is not monitoring anything."
            )
            how_parts.append(
                "Open the alert, fix its query or its permissions (the error above says "
                "which), or delete it if nobody owns it any more."
            )
        if ISSUE_UNNOTIFIED in issues:
            lead = "It also has" if why_parts else f"Alert '{name}' has"
            trig = (
                f"; it triggered {triggered:,} times in {history_days} days and nobody heard"
                if triggered
                else ""
            )
            why_parts.append(
                f"{lead} no subscribers, is not run by a job, and none of its "
                f"{evaluations:,} evaluations notified anyone{trig}."
            )
            how_parts.append(
                "Add a subscriber or notification destination if the condition matters; "
                "otherwise delete or pause the alert."
            )
        if warm:
            lead = "Its evaluations" if why_parts else f"The evaluations of alert '{name}'"
            why_parts.append(
                f"{lead}{_cron_hint(cron)} were the only thing running on SQL "
                f"warehouse '{wh_name}' in {warm_hours:,} billed hours on {warm_days} days "
                f"({statements:,} alert statements in {history_days} days). With a "
                f"{auto_stop:.0f}-minute auto-stop, each evaluation restarts or holds the "
                f"warehouse, so ~${addressable:,.0f}/mo of its ~${wh_monthly:,.0f}/mo is "
                f"spent keeping it up for this alert."
            )
            how_parts.append(
                "Evaluate it less often than the warehouse's auto-stop (or at the "
                "cadence the condition really needs), lower the warehouse's auto-stop, "
                "or move the alert to a warehouse that is already running at those times."
            )
        if addressable > 0:
            how_parts.append("The saving is an estimate: confirm with the alert's owner first.")

        if addressable >= 1000:
            priority = RulePriority.HIGH
        elif addressable >= 200 or ISSUE_ERRORING in issues:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        # (a) is a direct fact from the evaluation log; (b) infers job wiring from
        # task_key, and (c) is an apportioned estimate.
        confidence = (
            CONFIDENCE_HIGH if issues == [ISSUE_ERRORING] else CONFIDENCE_MEDIUM
        )

        eval_viz = viz_daily_stack(
            title="Evaluations per day",
            unit="evaluations",
            series=[
                ("ok", "OK (condition not met)", "info"),
                ("t", "Triggered", "violet"),
                ("e", "Errored", "danger"),
            ],
            rows=row.get("eval_by_day"),
            lookback_days=history_days,
            note=(f"{notifying:,} of {evaluations:,} evaluations notified someone "
                  f"({subs} subscriber(s))."),
            msg="alert_evaluations",
            params={"notifying": notifying, "evaluations": evaluations, "subscribers": subs},
        )
        warm_viz = viz_daily_stack(
            title=f"Hours per day this alert alone kept '{wh_name}' running",
            unit="h",
            series=[("h", "Billed hours with only this alert's queries", "warn")],
            rows=row.get("warm_by_day"),
            lookback_days=history_days,
            note=(f"{warm_hours:,} billed hours on {warm_days} days; the warehouse "
                  f"auto-stops after {auto_stop:.0f} min."),
            msg="alert_warm_hours",
            params={"warehouse": wh_name, "hours": warm_hours, "days": warm_days,
                    "auto_stop": auto_stop},
        ) if warm else None

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="alert",
            resource_id=str(row["alert_id"]),
            resource_name=name,
            # Every dollar here is the alert's warehouse's compute, so it shares that
            # warehouse's spend pool (deduped with the warehouse's own findings).
            affected_spend_scope=(
                f"warehouse|{row['warehouse_id']}" if row.get("warehouse_id") else None),
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked.
            monthly_savings_usd=round(addressable, 2),
            savings_status="estimated" if addressable > 0 else "advisory",
            monthly_savings_low_usd=round(addressable, 2),
            effort=EffortBand.LOW,
            priority=priority,
            why=" ".join(why_parts),
            how=" ".join(how_parts),
            evidence=evidence_block(
                confidence=confidence,
                data_basis=_BASIS.format(n=history_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=warm_viz or eval_viz,
                viz_extra=[eval_viz] if warm_viz and eval_viz else None,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                issues=issues,
                owned_by=row.get("owned_by"),
                run_as=row.get("run_as"),
                schedule=cron,
                subscription_count=subs,
                evaluations=evaluations,
                errored_evaluations=int(row.get("errored_evaluations") or 0),
                triggered_evaluations=triggered,
                notifying_evaluations=notifying,
                user_job_evaluations=user_job,
                error_streak_days=streak,
                last_error_code=row.get("last_error_code"),
                last_ok_time=row.get("last_ok_time"),
                last_evaluation=row.get("last_evaluation"),
                warehouse_id=row.get("warehouse_id"),
                warehouse_name=row.get("warehouse_name"),
                warehouse_type=row.get("warehouse_type"),
                auto_stop_minutes=row.get("auto_stop_minutes"),
                alert_statements=statements,
                warm_hours=warm_hours,
                warm_days=warm_days,
                warehouse_alert_only_hours=wh_hours,
                warehouse_cost_window_usd=round(float(row.get("wh_cost_window_usd") or 0.0), 2),
                history_days=history_days,
                addressable_monthly_usd=round(addressable, 2),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
