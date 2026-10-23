"""Rule: AI/BI dashboards refreshed on a schedule that nobody opens (Tier 2, C4).

A dashboard with a refresh schedule (or any automated
caller) keeps running its queries on a SQL warehouse whether or not anyone reads
the result. When nobody has opened it for weeks, that warehouse time buys nothing.

How views are read (verified live, see the SQL header): ``access.audit``
``service_name='dashboards'`` keys every event on ``request_params['dashboard_id']``.
Opening a dashboard logs ``get*Dashboard*``; its queries log ``executeQuery``, and a
scheduled/background refresh carries ``user_agent`` ``lakeview-processing_*``. A
dashboard is "unviewed" when no open AND no non-automated ``executeQuery`` happened
for ``unviewed_days``.

**Advisory, $0 booked.** The figure is, per billed warehouse-HOUR the dashboard
queried in, its share of that hour's query task time times the hour's billed cost,
over the unviewed window — hours it never touched are never charged to it. Pausing
one dashboard only saves what the warehouse then stops running, so it is ADDRESSABLE spend, not a
certain saving. Views that the audit log can't see (an export, an API consumer
reading cached results, email snapshots) are why confidence is medium and the
advice is "confirm, then pause". Needs ``system.access.audit``: when it is not
readable the preflight reports the rule as unavailable (its SQL references it), and
a dashboard the audit log never saw at all is not judged.
"""

from __future__ import annotations

from datetime import date, datetime

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
    "system.query.history (queries with query_source.dashboard_id, task time per "
    "warehouse) + system.access.audit service 'dashboards' (opens via get*Dashboard*, "
    "and executeQuery from any caller other than the 'lakeview-processing' scheduled "
    "refresh count as views; {v}-day lookback) + billing.usage per warehouse x "
    "list_prices over the last {n} days. Cost = for each billed warehouse-hour the "
    "dashboard queried in, its share of that hour's query task time (query count when "
    "every query was a cache hit) x the hour's billed cost. Pausing it saves less "
    "when other work keeps the warehouse up in those hours. Views the audit log "
    "can't see (exports, email snapshots, API reads of cached results) are not "
    "counted."
)


def _days_ago(value, today: date) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return max(0, (today - value).days)
    return None


@register
class UnviewedScheduledDashboardsRule(Rule):
    id = "unviewed_scheduled_dashboards"
    # Advisory: the task-share allocation is approximate and views outside the audit
    # log (exports, snapshots, API reads) can't be ruled out.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Scheduled dashboards nobody opens"
    resource_type = "dashboard"
    category = "sql"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    cloud_savings = "none"
    sql_file = "unviewed_scheduled_dashboards.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        # SIGNAL-PRESENCE GATE: judge only dashboards the audit log demonstrably
        # covers. Query activity with no audit event at all means we can't see its
        # views, not that it has none.
        if int(row.get("audit_events") or 0) <= 0:
            return None
        # Belt and braces: the SQL already drops dashboards viewed inside the window.
        window = int(row.get("unviewed_window_days") or t.get("unviewed_days", 14))
        today = date.today()
        last_view_days = _days_ago(row.get("last_view_time"), today)
        if int(row.get("recent_view_events") or 0) > 0:
            return None
        if last_view_days is not None and last_view_days < window:
            return None
        # A dashboard trashed during the window is already being cleaned up.
        if int(row.get("was_deleted") or 0) > 0:
            return None

        queries = int(row.get("queries") or 0)
        active_days = int(row.get("observed_days") or 0)
        if queries < int(t.get("min_queries", 20)):
            return None
        if active_days < int(t.get("min_active_days", 5)):
            return None

        cost_window = float(row.get("unviewed_cost_window") or 0.0)
        addressable = to_monthly(cost_window, window)
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        dash_id = str(row["dashboard_id"])
        wh = row.get("warehouse_id")
        who = row.get("executed_by")
        task_hours = float(row.get("task_hours") or 0.0)
        hours_cost = float(row.get("active_hours_cost_window") or 0.0)
        # Its share of what the warehouse billed in the hours it queried.
        share = min(1.0, cost_window / hours_cost) if hours_cost > 0 else 0.0
        active_hours = int(row.get("active_hours") or 0)
        sole_hours = int(row.get("sole_user_hours") or 0)
        refreshes_per_day = active_hours / max(active_days, 1)
        gap_s = active_hours * 3600 / queries  # mean spacing while it is refreshing
        lookback = ctx.lookback_days

        if addressable >= 500:
            priority = RulePriority.HIGH
        elif addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        if last_view_days is None:
            seen = f"Nobody has opened it in the last {lookback} days"
        else:
            seen = f"It was last opened {last_view_days} days ago"
        who_clause = f" as {who}" if who else ""
        if task_hours >= 0.05:
            work = f"{task_hours:,.1f} h of query task time"
        else:
            work = "almost all of them answered from the result cache"
        why = (
            f"This dashboard ran {queries:,} queries{who_clause} on warehouse {wh} over "
            f"the last {window} days, in about {refreshes_per_day:.0f} hour(s) a day on "
            f"{active_days} days ({work}). "
            f"{seen}: every one of those queries came from a scheduled or background "
            f"refresh, not a person. In the {active_hours:,} warehouse-hours it queried "
            f"in, it did {share * 100:.0f}% of the work"
            + (f" and was the only thing running in {sole_hours:,} of them"
               if sole_hours else "")
            + f" — ~${addressable:,.0f}/mo of the warehouse's cost."
            + (f" That is a query every ~{gap_s:.0f} s in the hours it runs — far more "
               "often than anyone reading it would need." if gap_s < 60 else "")
            + (" Refreshing around the clock, it may also be what keeps the warehouse "
               "from auto-stopping." if refreshes_per_day >= 20 else "")
        )
        how = (
            "Check who still needs it: the dashboard's schedules and subscribers "
            "(Schedule button on the published dashboard), and any export or email "
            "snapshot that reads it outside the app. If nobody does, pause or delete "
            "the schedule (or trash the dashboard). If a few people read it rarely, "
            "refresh it less often — daily instead of hourly — and let viewers trigger "
            "a refresh on open. No saving is booked: the figure is a task-time share of "
            "the warehouse, and it only drops if the warehouse then runs less."
        )

        viz = viz_daily_stack(
            title="Scheduled refreshes vs views per day",
            unit="events",
            series=[
                ("r", "Scheduled / background refresh queries", "warn"),
                ("v", "Views (opened, or a filter changed)", "info"),
            ],
            rows=row.get("days"),
            lookback_days=lookback,
            marker=(row.get("last_view_time"), "last viewed"),
            note=(f"No views in the last {window} days, while the refreshes kept "
                  f"running (from the audit log)."),
            msg="dashboard_refresh_views",
            params={"window": window},
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="dashboard",
            resource_id=dash_id,
            resource_name=dash_id,
            # The dollars are a share of the WAREHOUSE's spend, so they share its pool:
            # the warehouse's own findings and this one never count the same spend twice.
            affected_spend_scope=f"warehouse|{wh}" if wh else None,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW,  # pause a schedule, after asking the owner
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                viz=viz,
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=window, v=lookback)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                dashboard_id=dash_id,
                warehouse_id=wh,
                warehouses=row.get("warehouses"),
                executed_by=who,
                queries=queries,
                task_hours=round(task_hours, 2),
                active_days=active_days,
                refresh_hours_per_day=round(refreshes_per_day, 1),
                mean_query_spacing_s=round(gap_s, 1),
                unviewed_window_days=window,
                last_view_days_ago=last_view_days,
                view_events_lookback=int(row.get("view_events") or 0),
                automated_refresh_events=int(row.get("automated_events") or 0),
                active_hours=active_hours,
                sole_user_hours=sole_hours,
                active_hours_share_pct=round(share * 100, 1),
                active_hours_cost_window_usd=row.get("active_hours_cost_window"),
                unviewed_cost_window_usd=round(cost_window, 2),
                addressable_monthly_usd=round(addressable, 2),
            ),
        )
