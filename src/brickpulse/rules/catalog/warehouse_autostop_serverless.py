"""Rule: shorten auto-stop on serverless SQL warehouses.

The classic ``warehouse_autostop`` rule deliberately excludes serverless
warehouses (``warehouse_type <> 'SERVERLESS'``) on the grounds that serverless
"has no warm-pool". But a serverless warehouse still stays *running* — and
billing per-second — through its auto-stop idle tail after each query. With the
default 10-minute auto-stop and sporadic queries, that idle tail is real spend a
shorter setting would reclaim.

**Why this rule does not just recommend the minimum.** An earlier version
proposed a 1-minute auto-stop. That is a bad recommendation even though it
maximizes the modelled saving: the Databricks UI itself enforces a 5-minute floor
for serverless auto-stop (1 min is API-only), and a warehouse that stops between
every pair of queries makes its users wait on a restart over and over. The
marginal dollars between 10 and 5 minutes are small; the usability cost of
churning is not.

So this rule *analyses the warehouse's actual usage pattern* — the distribution
of idle gaps between its queries — and picks the shortest value in the
usability-safe 5-10 minute window that does not introduce more than a few extra
restarts per day (``savings.choose_serverless_autostop``). A warehouse whose
queries arrive in tight clusters keeps a longer setting; one that is genuinely
idle for hours between bursts gets the short one. If no candidate is both
shorter than today's setting and comfortable, the rule returns ``None`` rather
than nagging.

The evidence carries the whole decision (gap histogram, every candidate value
with its reclaim and restart cost, and the hourly usage rhythm) so the user can
see *why* — see ``evidence.viz_autostop_tradeoff``.

Effort is TRIVIAL: a config toggle with no workload change.
"""

from __future__ import annotations

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import (
    coverage_evidence,
    coverage_note,
    underpriced,
)
from brickpulse.rules.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    evidence_block,
    gap_buckets_for_viz,
    gap_histogram_from_row,
    viz_autostop_tradeoff,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import (
    MAX_AUTOSTOP_MINUTES_SERVERLESS,
    MAX_EXTRA_STOPS_PER_DAY,
    MAX_EXTRA_STOPS_PER_DAY_CHURNY,
    MIN_AUTOSTOP_MINUTES_SERVERLESS,
    SAVINGS_CURVE_MINUTES,
    STARTUP_PENALTY_MIN_SERVERLESS,
    autostop_savings_tiers,
    choose_serverless_autostop,
    serverless_autostop_curve,
    serverless_autostop_savings_at,
)

_BASIS = (
    "system.query.history per-query idle gaps + billing.usage x list_prices, "
    "{n}-day lookback. The recommended value is chosen from THIS warehouse's gap "
    "distribution: the shortest setting between {lo} and {hi} min that reclaims "
    "idle without adding more than {stops:g} extra restart(s)/day. Restarts are "
    "charged at {pen:g} min each, so churn is netted out of the saving. We do not "
    "recommend below {lo} min — that is the Databricks UI floor for serverless, "
    "and stopping between every query degrades the experience for its users."
)


@register
class WarehouseAutoStopServerlessRule(Rule):
    id = "warehouse_autostop_serverless"
    # estimated (out of booked headline) until the savings rebuild.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Shorten auto-stop on serverless SQL warehouses"
    resource_type = "warehouse"
    # "Saved so far" (finops_rules.change_tracking): auto-stop moved toward the chosen
    # value. Partial moves are valued on evidence.savings_curve, derived from the gap
    # histogram in score() (PR F1b); a finding without one is valued linearly.
    tracked_setting = TrackedSetting(
        resource="warehouse", column="auto_stop_minutes",
        baseline_key="auto_stop_minutes", proposed_key="proposed_autostop_minutes",
        direction="decrease", off_values=(0, None), label="Auto-stop (minutes)",
    )
    category = "sql"
    sql_file = "warehouse_autostop_serverless.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. The whole recommendation is derived from this
        # warehouse's gap DISTRIBUTION, and a distribution built from one day of
        # queries is not one — picking an auto-stop from it would be guessing.
        # See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None  # no billed cost attributed => nothing to save

        warm_min = row.get("warm_minutes")
        hist = gap_histogram_from_row(row)
        # Without the gap distribution we cannot judge whether shortening is safe,
        # and we refuse to guess: no fallback to a blanket minimum.
        if not hist or not warm_min:
            return None

        lo = int(t.get("min_autostop_minutes", MIN_AUTOSTOP_MINUTES_SERVERLESS))
        hi = int(t.get("max_autostop_minutes", MAX_AUTOSTOP_MINUTES_SERVERLESS))
        max_stops = float(t.get("max_extra_stops_per_day", MAX_EXTRA_STOPS_PER_DAY))
        churny_stops = float(
            t.get("churny_stops_per_day", MAX_EXTRA_STOPS_PER_DAY_CHURNY)
        )
        penalty = float(t.get("startup_penalty_minutes", STARTUP_PENALTY_MIN_SERVERLESS))
        current = row.get("auto_stop_minutes")

        proposed, analysis = choose_serverless_autostop(
            gap_histogram=hist,
            current_autostop_min=float(current or 0) or float("inf"),
            lookback_days=ctx.lookback_days,
            min_minutes=lo,
            max_minutes=hi,
            startup_penalty_min=penalty,
            max_extra_stops_per_day=max_stops,
            churny_stops_per_day=churny_stops,
        )
        # No setting in the safe window both shortens and stays comfortable =>
        # this warehouse is already tuned for its usage pattern. Say nothing.
        if proposed is None:
            return None

        chosen = analysis["chosen"]
        savings, reclaimable = serverless_autostop_savings_at(
            net_reclaimed_min=float(chosen["net_reclaimed_min"]),
            warm_minutes_current=float(warm_min),
            monthly_cost=monthly_cost,
        )
        if savings <= 0:
            return None

        # Split the saving into "enable auto-stop" (the big, low-churn win for a
        # warehouse that has none) vs. "tighten into the 5-10 min window" (a smaller
        # further gain that costs restarts). For a warehouse with NO auto-stop the
        # bulk of the money is the enable tier: a long baseline stop already reclaims
        # the multi-hour weekend/overnight gaps that dominate the bill, and the short
        # setting only adds a small tail. Leading with the blended short-setting
        # number over-attributes the saving to a churny change that is mostly
        # unnecessary. See savings.autostop_savings_tiers.
        tiers = autostop_savings_tiers(
            gap_histogram=hist,
            current_autostop_min=float(current or 0) or float("inf"),
            chosen_minutes=proposed,
            warm_minutes_current=float(warm_min),
            monthly_cost=monthly_cost,
            lookback_days=ctx.lookback_days,
        )
        enable_tier = tiers.get("enable")
        tighten_tier = tiers["tighten"]

        setting = f"{current}-min" if current else "no"
        name = row.get("warehouse_name") or str(row["warehouse_id"])
        median_gap = float(row.get("median_gap_min") or 0.0)
        gap_count = int(row.get("gap_count") or 0)
        high_churn = bool(analysis.get("high_churn"))
        stops_per_day = chosen["extra_stops_per_day"]

        # Savings band (see docs/SAVINGS_CONFIDENCE_MODEL.md). The point estimate
        # `savings` is the full proposed short setting; the honest floor differs by
        # case, using the rule's OWN enable-vs-tighten decomposition rather than a
        # blanket percentage:
        #   * an enable tier exists (warehouse never stops) -> the low-churn "just
        #     turn it on at a safe baseline" reclaim is bankable with confidence, so
        #     low = that enable saving; the churnier tightening is the upside (high).
        #   * no enable tier but high-churn (already stops, but tightening restarts it
        #     on a cadence someone must sign off on) -> conservatively the reclaim may
        #     not be taken at all: low = $0, high = the full reclaim if it is.
        #   * comfortable tighten on an already-stopping warehouse -> measured and
        #     low-churn, so little dollar uncertainty: band stays collapsed.
        enable_savings = (
            float(enable_tier["monthly_savings_usd"]) if enable_tier else 0.0
        )
        range_basis: str | None = None
        if enable_savings > 0:
            low_savings, high_savings = round(enable_savings, 2), savings
            range_basis = (
                f"Low = the reclaim from simply turning auto-stop on at a safe "
                f"{tiers['baseline_minutes']}-minute setting (near-zero restart "
                f"cost); high = the full {proposed}-minute setting, including the "
                f"churnier tightening that adds ~{stops_per_day:.1f} restart(s)/day."
            )
        elif high_churn:
            low_savings, high_savings = 0.0, savings
            range_basis = (
                f"Low = $0 if the ~{stops_per_day:.0f} extra restart(s)/day a "
                f"{proposed}-min auto-stop adds aren't acceptable for this "
                f"warehouse's users; high = the full idle-tail reclaim if they are."
            )
        else:
            low_savings, high_savings = savings, savings

        # When auto-stop is OFF (or longer than the baseline), lead the explanation
        # with the enable tier — that is where ~most of the money is, and it carries
        # essentially no restart risk. The tighten tier then reads as an optional
        # refinement rather than the headline.
        if enable_tier and enable_tier["monthly_savings_usd"] > 0:
            base_m = tiers["baseline_minutes"]
            why = (
                f"Serverless SQL warehouse '{name}' has {setting} auto-stop — it never "
                f"stops — and spent ~${monthly_cost:,.0f}/mo. Across {gap_count:,} idle "
                f"gaps between its queries (median {median_gap:,.0f} min) it stayed "
                f"running, billing per-second, through every weekend and overnight lull. "
                f"MOST of the saving is simply turning auto-stop ON: even a conservative "
                f"{base_m}-minute stop reclaims ~{enable_tier['reclaimed_hours']:,.0f}h of "
                f"that idle — worth ~${enable_tier['monthly_savings_usd']:,.0f}/mo — with "
                f"almost no extra restarts, because the gaps it closes are hours long. "
                f"Tightening further to {proposed} min adds only "
                f"~${tighten_tier['monthly_savings_usd']:,.0f}/mo more "
                f"(~{stops_per_day:.1f} extra restarts/day). "
            )
        else:
            why = (
                f"Serverless SQL warehouse '{name}' has {setting} auto-stop and spent "
                f"~${monthly_cost:,.0f}/mo. Across {gap_count:,} idle gaps between its "
                f"queries (median {median_gap:,.0f} min), it stayed running — billing "
                f"per-second — for ~{reclaimable:,.0f}h of idle tail that a "
                f"{proposed}-minute auto-stop would reclaim, worth ~${savings:,.0f}/mo. "
            )
        if high_churn:
            # The saving is real and large, but so is the restart cost. Say both
            # plainly and hand the judgement to the person who knows the workload.
            why += (
                f"TRADE-OFF: something queries this warehouse on a regular cadence, "
                f"so a {proposed}-minute auto-stop would restart it ~"
                f"{stops_per_day:.0f} extra times per day. That is well above the "
                f"~{max_stops:.0f}/day we treat as unobtrusive, so this is only worth "
                f"doing if nothing latency-sensitive (a dashboard refresh, a polling "
                f"job, a BI extract) depends on it responding immediately."
            )
        else:
            why += (
                f"{proposed} min is the shortest setting this warehouse's usage "
                f"pattern tolerates: it adds only ~{stops_per_day:.1f} extra "
                f"restart(s) per day. Going shorter would stop the warehouse between "
                f"queries its users fire minutes apart."
            )

        if enable_tier and enable_tier["monthly_savings_usd"] > 0:
            base_m = tiers["baseline_minutes"]
            how = (
                f"Turn on auto-stop (SQL Warehouses > select the warehouse > Edit > "
                f"Auto stop). If restart latency worries you, start at a safe "
                f"{base_m} minutes to bank the bulk of the saving today with almost no "
                f"user-visible restarts, then tighten toward {proposed} min once you've "
                f"confirmed nothing latency-sensitive depends on it. A config toggle "
                f"with no workload change."
            )
        else:
            how = (
                f"Set auto-stop to {proposed} minutes (SQL Warehouses > select the "
                f"warehouse > Edit > Auto stop). A config toggle with no workload change."
            )
        if high_churn:
            how += (
                " Before you do: check WHAT is querying it on that cadence (Query "
                "History, grouped by user/application). If it is a scheduled "
                "refresh that can tolerate a few seconds of start-up, take the "
                "saving; if users are waiting on it interactively, leave the "
                "auto-stop where it is and treat this finding as informational."
            )
        else:
            how += (
                f" Keep it at {proposed} rather than the {lo}-minute floor — the "
                f"chart shows how many additional cold starts each shorter value "
                f"would cost this warehouse's users for very little extra saving."
            )

        # evidence.savings_curve (plan F.2, PR F1b): net reclaim at each candidate
        # setting between the proposal and today's, from the same histogram and
        # restart penalty that priced the estimate. "Saved so far" values a partial
        # move on it; the top point is the estimate and nothing exceeds it.
        curve = serverless_autostop_curve(
            gap_histogram=hist,
            current_autostop_min=current,
            proposed_minutes=proposed,
            estimate=savings,
            warm_minutes_current=float(warm_min),
            monthly_cost=monthly_cost,
            startup_penalty_min=penalty,
            candidates=t.get("savings_curve_minutes", SAVINGS_CURVE_MINUTES),
        )

        viz = viz_autostop_tradeoff(
            gap_buckets=gap_buckets_for_viz(row),
            candidates=analysis.get("candidates", []),
            current_minutes=current,
            chosen_minutes=proposed,
            median_gap_min=median_gap,
            p90_gap_min=float(row.get("p90_gap_min") or 0.0),
            queries_by_hour=row.get("queries_by_hour"),
            queries_by_hour_minute=row.get("queries_by_hour_minute"),
            queries_by_dow_hour=row.get("queries_by_dow_hour"),
            tiers=tiers,
            lookback_days=ctx.lookback_days,
            max_extra_stops_per_day=max_stops,
            high_churn=high_churn,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=low_savings,
            monthly_savings_high_usd=high_savings,
            # A high-churn change is no longer a free toggle: it needs someone to
            # check what is polling the warehouse first.
            effort=EffortBand.LOW if high_churn else EffortBand.TRIVIAL,
            priority=RulePriority.MEDIUM if high_churn else RulePriority.HIGH,
            why=why,
            how=how,
            evidence=evidence_block(
                # Same measurement either way; what differs is whether the ACTION is
                # safe to take unattended, so a high-churn finding is not "high
                # confidence, just do it".
                confidence=CONFIDENCE_MEDIUM if high_churn else CONFIDENCE_HIGH,
                data_basis=_BASIS.format(
                    n=ctx.lookback_days, lo=lo, hi=hi, stops=max_stops, pen=penalty
                ) + (
                    f" This warehouse is queried on a regular cadence, so no value in "
                    f"the window stays under {max_stops:.0f} restarts/day; the "
                    f"recommendation is surfaced with its restart cost stated rather "
                    f"than hidden, because the idle spend is material."
                    if high_churn else ""
                ) + history_note(row, ctx) + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                high_churn=high_churn,
                viz=viz,
                warehouse_size=row.get("warehouse_size"),
                dbus_30d=row.get("dbus_30d"),
                auto_stop_minutes=current,
                proposed_autostop_minutes=proposed,
                cost_30d_usd=row.get("cost_30d_usd"),
                reclaimable_hours=reclaimable,
                extra_restarts_per_day=chosen["extra_stops_per_day"],
                median_idle_gap_min=round(median_gap, 1),
                p90_idle_gap_min=round(float(row.get("p90_gap_min") or 0.0), 1),
                idle_gaps_analyzed=gap_count,
                avoidable_usd=savings,
                **({"savings_curve": curve} if curve else {}),
                # What the low/high ends of the band mean for THIS warehouse, shown
                # verbatim on the Data Quality panel (only when the band is widened).
                **({"savings_range_basis": range_basis} if range_basis else {}),
            ),
        )
