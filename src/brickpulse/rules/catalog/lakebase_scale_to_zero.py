"""Rule: enable scale-to-zero on a Lakebase endpoint that idles on its always-on floor.

Lakebase Autoscaling bills the CU-hours a compute actually runs. The autoscaling
max is a ceiling, not a cost; the cost levers are the floor (min CU) and whether the
compute may suspend at all. An endpoint with scale-to-zero off bills its floor every
10-minute slot, whether or not a client is connected. This rule finds those
endpoints and prices the floor hours in which no client was connected.

Both halves are MEASURED, which is why this is a high-confidence rule:

* **The floor is billed as its own compute type.** ``billing.usage`` tags it
  ``product_features.lakebase.compute_type = 'COMPUTE_NODE_ALWAYS_ON_MIN'``.
  Verified live (2026-11-04): endpoints billing it filled ~990 of 1,008 ten-minute
  slots in 7 days; endpoints billing only ``COMPUTE_NODE`` filled a median of 13.
  So the finding states a fact about the configuration (scale-to-zero is off, or
  its timeout is longer than the window), not a guess.
* **Idleness is a client-backend count.** ``system.lakebase.database_gauges``
  samples ``numbackends`` every 30 s and reads 0 on an idle database.
  ``sessions_established`` is deliberately ignored: the platform opens its own
  sessions every ~2 hours, so it never reads 0.

Guards, in the order ``score()`` applies them:

1. Missing telemetry is UNKNOWN, never idle. The ``system.lakebase`` tables are
   Beta and opt-in (Previews > "Lakebase Observability in System Tables"); the
   source preflight marks the rule unavailable when they can't be read, and an
   endpoint whose billed hours are mostly unobserved is suppressed.
2. Read replicas are skipped: scale-to-zero support for them is not verified.
3. Scale-to-zero is allowed only when the autoscaling max is <= 32 CU, and
   max - min <= 16 CU. The floor is read from the compute's memory (~2 GiB per CU,
   measured), so a floor above 32 CU is suppressed (impossible) and a floor between
   16 and 32 CU carries a warning (possible only if the max is <= 32).

Pricing is region-correct by construction: every 10-minute slot is priced by the
shared list-price join on its own SKU (the region is in the SKU name), so a Brazil
endpoint shows about twice the saving of an identical one in US East. No $/DBU
constant appears anywhere in the rule.

The saving is **estimated** (shown and ranked, not booked): the idle hours and their
cost are measured, but suspending trades a cold start on the next connection. Labs
until the rule has been reviewed on more accounts.
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
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.billing.usage (billing_origin_product = 'LAKEBASE', compute_type = "
    "COMPUTE_NODE_ALWAYS_ON_MIN: the always-on floor, priced per 10-minute slot x "
    "list_prices on its own regional SKU) + system.lakebase.database_gauges "
    "(numbackends, client backends sampled every 30 s), {n}-day window (the "
    "telemetry tables keep 7 days). An hour counts as idle only when telemetry "
    "covered it and no client backend appeared; unobserved hours are unknown, not "
    "idle. Only idle runs of {run}+ hours count, each net of a {timeout}-minute "
    "suspend timeout. Seven days span one weekly cycle; the monthly figure assumes "
    "the pattern repeats."
)

_RANGE_WARNING = (
    "The floor is between {g:g} and {mx:g} CU. Lakebase allows scale-to-zero only "
    "when the autoscaling max is at most {mx:g} CU, so check the max before enabling "
    "it, and lower it first if needed."
)


#: Default branch names: a project's root branch is retired by deleting the project.
_DEFAULT_BRANCHES = {"production", "main"}


def _normalize_days(days) -> list[dict]:
    """Coerce the SQL's per-day series (Spark Rows, dicts or JSON text) to dicts."""
    if isinstance(days, str):
        import json
        try:
            days = json.loads(days)
        except (ValueError, TypeError):
            return []
    out = []
    for entry in days or []:
        if entry is None:
            continue
        row = entry.asDict() if hasattr(entry, "asDict") else dict(entry)
        if row.get("d") is None:
            continue
        out.append({
            "d": str(row["d"]),
            "billed_hours": int(row.get("billed_hours") or 0),
            "client_hours": int(row.get("client_hours") or 0),
        })
    return sorted(out, key=lambda x: x["d"])


@register
class LakebaseScaleToZeroRule(Rule):
    id = "lakebase_scale_to_zero"
    version = "0.1.0"
    title = "Enable scale-to-zero on an idle Lakebase endpoint"
    resource_type = "lakebase_endpoint"
    category = "lakebase"
    sql_file = "lakebase_scale_to_zero.sql.j2"
    # Labs: net-new coverage, kept out of the headline and NBA until reviewed.
    experimental = True
    # Idle hours and their cost are measured; the cold-start trade-off is not.
    default_savings_status = "estimated"
    # The saving is a share of the endpoint's own spend, so it scales by its
    # effective contract discount.
    contract_pricing = "ratio"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        billed_hours = int(row.get("billed_hours") or 0)
        telemetry_hours = int(row.get("telemetry_hours") or 0)
        if billed_hours <= 0:
            return None
        # Unobserved hours are unknown. Too many of them and the endpoint's idleness
        # can't be judged at all.
        coverage = telemetry_hours / billed_hours
        if coverage < float(t.get("min_telemetry_coverage", 0.8)):
            return None
        if str(row.get("compute_mode") or "").lower() == "replica":
            return None

        floor_cu = row.get("floor_cu")
        floor_cu = float(floor_cu) if floor_cu is not None else None
        max_cu = float(t.get("max_scale_to_zero_cu", 32))
        guaranteed_cu = float(t.get("guaranteed_scale_to_zero_cu", 16))
        if floor_cu is not None and floor_cu > max_cu:
            return None  # max >= min > 32 CU: scale-to-zero is not allowed

        # Monthly figures use the row's own window (7 days at most), never the
        # engine-wide lookback: the telemetry cannot cover a longer span.
        window = int(row.get("window_days") or ctx.lookback_days)
        monthly_cost = to_monthly(float(row.get("cost_window_usd") or 0.0), window)
        reclaim = to_monthly(float(row.get("reclaimable_cost_usd") or 0.0), window)
        reclaim = min(reclaim, monthly_cost)
        if reclaim < float(t.get("min_monthly_savings_usd", 10)):
            return None

        idle_hours = int(row.get("idle_hours") or 0)
        client_hours = int(row.get("client_hours") or 0)
        reclaim_hours = int(row.get("reclaimable_idle_hours") or 0)
        longest = int(row.get("longest_idle_run_hours") or 0)
        run_h = int(t.get("min_idle_run_hours", 2))
        timeout = int(t.get("suspend_timeout_minutes", 5))
        fully_idle = client_hours == 0

        project = row.get("project_name") or row.get("project_id")
        branch = row.get("branch_name") or row.get("branch_id")
        endpoint_id = str(row["endpoint_id"])
        label = row.get("endpoint_name") or endpoint_id
        name = f"{project} / {branch} / {label}" if project and branch else label
        cu_desc = f"{floor_cu:g} CU floor" if floor_cu is not None else "floor"

        if fully_idle or reclaim >= float(t.get("high_priority_monthly_usd", 200)):
            priority = RulePriority.HIGH
        elif monthly_cost > 0 and reclaim / monthly_cost >= 0.5:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        if fully_idle:
            why = (
                f"Lakebase endpoint '{name}' billed its always-on {cu_desc} for all "
                f"{billed_hours:,} hours of the last {window} days (~${monthly_cost:,.0f}/mo), "
                f"and no client connected in any of the {telemetry_hours:,} hours the "
                f"telemetry covered. Scale-to-zero is off, so it pays for a database "
                f"nobody is using."
            )
            if str(row.get("branch_name") or "").lower() in _DEFAULT_BRANCHES:
                retire = (
                    f"Confirm nothing depends on project '{project or 'this project'}'. "
                    f"If it is no longer needed, delete the project."
                )
            else:
                retire = (
                    f"Confirm nothing depends on branch '{branch or 'this branch'}'. If "
                    f"it is no longer needed, delete the branch or its compute."
                )
            how = (
                f"{retire} Otherwise enable scale-to-zero on the endpoint with a "
                f"{timeout}-minute suspend timeout: it then bills only while a client is "
                f"connected, and the first connection after a suspend waits for a cold "
                f"start."
            )
        else:
            why = (
                f"Lakebase endpoint '{name}' billed its always-on {cu_desc} for "
                f"{billed_hours:,} hours of the last {window} days (~${monthly_cost:,.0f}/mo), "
                f"but a client was connected in only {client_hours:,} of them. "
                f"{reclaim_hours:,} billed hours fell in stretches of {run_h}+ hours with no "
                f"client at all (longest: {longest:,}h), worth ~${reclaim:,.0f}/mo after a "
                f"{timeout}-minute suspend timeout."
            )
            how = (
                f"Enable scale-to-zero on the endpoint with a {timeout}-minute suspend "
                f"timeout. It keeps the current autoscaling range and stops billing once "
                f"no client is connected. The first connection after a suspend waits for "
                f"a cold start, so keep scale-to-zero off where callers can't tolerate "
                f"one, or use a longer timeout to suspend only overnight."
            )

        warning = None
        if floor_cu is not None and guaranteed_cu < floor_cu <= max_cu:
            warning = _RANGE_WARNING.format(g=guaranteed_cu, mx=max_cu)
            how += " " + warning

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="lakebase_endpoint",
            resource_id=endpoint_id,
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(reclaim, 2),
            savings_status="estimated",
            monthly_savings_low_usd=round(reclaim, 2),
            monthly_savings_high_usd=round(reclaim, 2),
            effort=EffortBand.LOW,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=window, run=run_h, timeout=timeout)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                **({"data_warning": warning} if warning else {}),
                tier="fully_idle" if fully_idle else "idle_stretches",
                project_id=row.get("project_id"),
                branch_id=row.get("branch_id"),
                project_name=row.get("project_name"),
                branch_name=row.get("branch_name"),
                endpoint_name=row.get("endpoint_name"),
                compute_mode=row.get("compute_mode"),
                floor_cu=floor_cu,
                peak_cu=row.get("peak_cu"),
                floor_dbu_per_hour=row.get("floor_dbu_per_hour"),
                window_days=window,
                billed_hours=billed_hours,
                telemetry_hours=telemetry_hours,
                telemetry_coverage=round(coverage, 4),
                idle_hours=idle_hours,
                client_hours=client_hours,
                reclaimable_idle_hours=reclaim_hours,
                longest_idle_run_hours=longest,
                suspend_timeout_minutes=timeout,
                cost_window_usd=row.get("cost_window_usd"),
                reclaimable_cost_window_usd=row.get("reclaimable_cost_usd"),
                monthly_reclaimable_usd=round(reclaim, 2),
                daily_activity=_normalize_days(row.get("daily_activity")),
            ),
        )
