"""Rule: SQL warehouse kept warm by trivial / metadata queries (Tier 1, B1).

A BI tool, dashboard auto-refresh or poller that sends a
statement every few minutes resets a warehouse's auto-stop timer, so the warehouse
never stops. ``warehouse_autostop`` / ``warehouse_autostop_serverless`` cannot see
this: they count those pings as busy time, and a shorter auto-stop does not help
because the timer never expires. The fix is on the CLIENT (the refresh schedule, the
connection-pool health check, the poller) — which is why the finding names it.

A "keep-alive hour" is an hour the warehouse was billed in which every statement was
trivial (SHOW / DESCRIBE / USE / SET / OTHER / EXPLAIN, a SELECT that ran no tasks —
a result-cache hit, a metadata probe, ``SELECT 1``) and no real statement was running.

**Estimated saving.** The figure is the billed cost of the keep-alive hours, less one
auto-stop tail per contiguous run of them (that tail is billed anyway after the last real
work) — what the bill drops by once the pings stop. Some trivial-only hours are
legitimate — a person viewing a cached dashboard, a caching layer pre-warming on
purpose — so it is ``estimated`` (shown and ranked), not booked. Promoted out of Labs
2026-09-23.
"""

from __future__ import annotations

from datetime import date, timedelta

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import (
    CONFIDENCE_MEDIUM,
    evidence_block,
    hourly_states_to_grid,
    viz_hour_heatmap,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.query.history (statement_type, total_task_duration_ms, from_result_cache, "
    "client_application, executed_by) classifies each billed warehouse-hour from "
    "system.billing.usage x list_prices, {n}-day lookback. A keep-alive hour = billed, "
    "with only trivial statements (metadata/session statements or a SELECT that ran no "
    "tasks) and no real statement running. The figure is the billed cost of those hours "
    "less one auto-stop tail per contiguous run of them — an ESTIMATED saving: some "
    "trivial-only hours are legitimate (a person reading a cached dashboard), so it is "
    "shown but not booked."
)

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _culprit_hint(client: str) -> str:
    """What the named client most likely is, so ``how`` points at the right knob."""
    c = (client or "").lower()
    if "dashboard" in c:
        return ("a dashboard with a short auto-refresh or refresh schedule — lengthen "
                "or remove the refresh, or schedule it only in working hours")
    if "job" in c:
        return ("a scheduled job or SQL task issuing a probe statement — check its "
                "schedule and whether it needs to run around the clock")
    if "genie" in c or "alert" in c:
        return "a scheduled Genie/alert evaluation — check its schedule"
    return ("a BI tool, connection-pool health check or poller — raise its refresh / "
            "keep-alive interval above the warehouse's auto-stop, or point it at a "
            "different endpoint")


@register
class WarehouseKeepaliveQueriesRule(Rule):
    id = "warehouse_keepalive_queries"
    # Estimated: the keep-alive hours are measured; some may be legitimate use.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "Warehouse kept warm by trivial or metadata queries"
    resource_type = "warehouse"
    category = "sql"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    cloud_savings = "none"
    sql_file = "warehouse_keepalive_queries.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        history_days = int(t.get("history_days", 14))
        billed = int(row.get("billed_hours") or 0)
        keepalive = int(row.get("keepalive_hours") or 0)
        if keepalive < int(t.get("min_keepalive_hours", 24)) or billed <= 0:
            return None
        # A rhythm, not one odd evening: keep-alive hours on several distinct days.
        if int(row.get("keepalive_days") or 0) < int(t.get("min_keepalive_days", 3)):
            return None
        share = keepalive / billed
        if share < float(t.get("min_keepalive_share", 0.10)):
            return None

        # A keep-alive holds the warehouse up for hours on end. Isolated single
        # trivial-only hours right after real work (someone re-reading cached results)
        # are the auto-stop tail, not a keep-alive: require sustained runs.
        islands = int(row.get("keepalive_islands") or 0)
        mean_run = keepalive / max(islands, 1)
        if mean_run < float(t.get("min_mean_run_hours", 2.0)):
            return None

        # Without the pings the warehouse still bills one auto-stop tail after its last
        # real work, so each contiguous run of keep-alive hours keeps up to one tail.
        window_cost = float(row.get("cost_window_usd") or 0.0)
        keepalive_cost = min(float(row.get("keepalive_cost_usd") or 0.0), window_cost)
        auto_stop = float(row.get("auto_stop_minutes") or 0.0)
        avg_hour_cost = keepalive_cost / keepalive
        tail_hours = min(auto_stop / 60.0, 1.0) * islands
        addressable_window = max(0.0, keepalive_cost - tail_hours * avg_hour_cost)
        addressable = to_monthly(addressable_window, history_days)
        if addressable < float(t.get("min_addressable_usd", 50)):
            return None
        monthly_cost = to_monthly(window_cost, history_days)

        clients = sorted(
            (c for c in (row.get("top_clients") or []) if c),
            key=lambda c: -int(c.get("n") or 0),
        )
        statements = int(row.get("keepalive_statements") or 0)
        cache_hits = int(row.get("keepalive_cache_hits") or 0)
        name = row.get("warehouse_name") or str(row["warehouse_id"])

        if addressable >= 1000 or share >= 0.5:
            priority = RulePriority.HIGH
        elif addressable >= 200:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        if clients:
            top = clients[0]
            who = str(top.get("client") or "unknown")
            user = top.get("user")
            culprit = f"'{who}'" + (f" as {user}" if user else "")
            top_share = (int(top.get("n") or 0) / statements) if statements else 0.0
            lead = "Most of those statements" if top_share >= 0.5 else "The largest share"
            culprit_clause = f" {lead} ({top_share * 100:.0f}%) came from {culprit}."
            hint = _culprit_hint(who)
        else:
            culprit_clause = ""
            hint = _culprit_hint("")
        cache_clause = (
            f" {cache_hits:,} of them were result-cache hits — answered without compute, "
            f"yet the warehouse stayed billed through them."
            if statements and cache_hits >= 0.5 * statements
            else ""
        )
        why = (
            f"SQL warehouse '{name}' has a {auto_stop:.0f}-minute auto-stop, but in "
            f"{keepalive:,} of its {billed:,} billed hours over the last {history_days} "
            f"days ({share * 100:.0f}%) it ran nothing except trivial statements "
            f"({statements:,} metadata/session statements or SELECTs that did no work)."
            f"{cache_clause}{culprit_clause} Those statements keep resetting the "
            f"auto-stop timer, holding the warehouse up for ~{mean_run:.0f} hours at a "
            f"stretch when it would otherwise stop — ~${addressable:,.0f}/mo of "
            f"its ~${monthly_cost:,.0f}/mo is spent keeping it warm for nothing. A shorter "
            f"auto-stop would not help: the timer never gets to expire."
        )
        how = (
            f"Find the client sending the statements (Query History > filter this "
            f"warehouse > Source / User) — most likely {hint}. Once the pings stop, the "
            f"existing auto-stop does the rest. The saving is an estimate: first confirm "
            f"the trivial-only hours are not people genuinely reading cached results."
        )

        states = list(row.get("hourly_states") or [])
        viz = None
        if states:
            grid = hourly_states_to_grid(states)
            start = date.today() - timedelta(days=history_days)
            days = [start + timedelta(days=i) for i in range(len(grid))]
            viz = viz_hour_heatmap(grid=grid, day_labels=[_WEEKDAYS[d.weekday()] for d in days],
                                   reclaimable_hours=keepalive, context="keepalive",
                                   day_dates=[d.isoformat() for d in days])

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked.
            monthly_savings_usd=round(addressable, 2),
            savings_status="estimated",
            monthly_savings_low_usd=round(addressable, 2),
            effort=EffortBand.LOW,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=history_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_type=row.get("warehouse_type"),
                warehouse_size=row.get("warehouse_size"),
                auto_stop_minutes=row.get("auto_stop_minutes"),
                billed_hours=billed,
                real_hours=int(row.get("real_hours") or 0),
                keepalive_hours=keepalive,
                keepalive_days=int(row.get("keepalive_days") or 0),
                keepalive_islands=islands,
                mean_run_hours=round(mean_run, 1),
                keepalive_share=round(share, 4),
                keepalive_statements=statements,
                keepalive_cache_hits=cache_hits,
                top_clients=clients,
                keepalive_cost_window_usd=round(keepalive_cost, 2),
                cost_window_usd=round(window_cost, 2),
                history_days=history_days,
                addressable_monthly_usd=round(addressable, 2),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
