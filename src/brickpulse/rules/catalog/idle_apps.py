"""Rule: stop or delete an idle Databricks App.

"Idle Apps (no access last 1 day)" is a common FinOps check. A Databricks App that is deployed and running holds
serverless compute and bills continuously (``billing_origin_product = 'APPS'``); an
app nobody opens is pure waste — the direct analogue of an idle serving endpoint.

Important caveats reflected in the confidence tier and the copy:

1. **The audit access filter is best-effort and unvalidated here** (no access to
   ``system.access.audit`` in the dev environment). A missed access event would read
   as "idle" and flag a BUSY app — the same false-positive class idle_serving_
   endpoints guards against. So the finding is MEDIUM (not HIGH) confidence and its
   action is "verify, then stop", not "delete now". Validate the audit filter on a
   workspace that hosts Apps before trusting this in production.
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
from brickpulse.rules.savings import idle_app_savings

_BASIS = (
    "system.billing.usage (billing_origin_product = 'APPS', usage_metadata.app_id) "
    "x list_prices for cost, system.access.audit (databricksApps access events) for "
    "usage, {n}-day lookback. Access is a per-event log; but the app-access audit "
    "filter is unvalidated in this build, so a missed event could read as idle — "
    "hence medium confidence and a verify-then-act recommendation."
)


@register
class IdleAppsRule(Rule):
    id = "idle_apps"
    version = "0.1.0"
    title = "Stop or delete an idle Databricks App"
    resource_type = "app"
    category = "apps"
    sql_file = "idle_apps.sql.j2"
    # DISABLED from recs + totals. The databricksApps audit stream is lifecycle-only
    # with no per-app request/traffic log, so idleness cannot be substantiated — even
    # the signal-presence gate cannot manufacture traffic evidence.
    # default_enabled=False here AND enabled:false in thresholds.yaml so no deploy
    # re-enables it by config default. Re-enable only once a real access signal exists.
    default_enabled = False

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # SIGNAL-PRESENCE GATE. access_events == 0 is only meaningful if this
        # workspace's audit log actually captures app access. When a workspace has
        # ZERO databricksApps access events in the whole window, we have no usage
        # signal at all — "no events" then means "we can't see access", not "nobody
        # opened it". Flagging every billed app as idle in that case is a pure
        # false positive (seen live: a workspace with 0 audit events flagged its
        # entire ~16k-app estate as waste). So emit nothing rather than guess.
        if int(row.get("app_access_events_total") or 0) <= 0:
            return None
        # Too little history to judge: an app deployed two days ago has billed for
        # two days and served nobody because it is NEW.
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds

        days_billed = int(row.get("days_billed") or 0)
        if days_billed < int(t.get("min_days_billed", 7)):
            return None  # not billing consistently => not an always-on cost

        access_events = int(row.get("access_events") or 0)
        if access_events > int(t.get("max_access_events", 0)):
            return None  # someone opened it => not idle

        monthly_cost = to_monthly(row.get("cost_window_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(t.get("min_monthly_cost_usd", 25)):
            return None  # below the floor, not worth an admin's attention

        savings = idle_app_savings(monthly_cost=monthly_cost, is_idle=True)
        if savings <= 0:
            return None

        name = row.get("app_name") or str(row.get("app_id"))
        billed_desc = (
            f"billed on every one of the last {ctx.lookback_days} days"
            if days_billed >= ctx.lookback_days
            else f"billed on {days_billed} of the last {ctx.lookback_days} days"
        )
        why = (
            f"Databricks App '{name}' {billed_desc} — about ${monthly_cost:,.0f}/mo — "
            f"but recorded no access events in that window. A deployed, running app "
            f"holds serverless compute and bills continuously whether or not anyone "
            f"opens it, so an unused one is pure waste."
        )
        how = (
            f"Verify nothing depends on '{name}' (check scheduled links, embeds, and "
            f"users who may be between sessions), then stop the app to release its "
            f"compute, or delete it if it is no longer needed (Compute > Apps > "
            f"{name}). Stopping keeps the definition and can be restarted on demand."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="app",
            resource_id=str(row.get("app_id") or name),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            effort=EffortBand.LOW,  # stop is a click, but confirm no dependents first
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                app_id=row.get("app_id"),
                access_events=access_events,
                distinct_users=int(row.get("distinct_users") or 0),
                last_access_time=row.get("last_access_time"),
                days_billed=days_billed,
                dbus_window=row.get("dbus_window"),
                cost_window_usd=row.get("cost_window_usd"),
            ),
        )
