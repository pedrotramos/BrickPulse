"""Rule: right-size an over-provisioned SQL warehouse (Gap G3).

``rightsize_underutilized`` right-sizes CLUSTERS from per-minute CPU. Warehouses
expose no CPU, so warehouse *size* over-provisioning had no rule — a documented
blind spot (Gap G3). This fills it.

TWO evidence tiers, chosen per warehouse by what the data supports:

* **Measured (primary).** ``system.query.history.total_task_duration_ms`` is the
  summed wall time of every Spark task a query ran across all slots. A minute's
  worth of that divided by 60 is the number of task slots kept busy that minute —
  a DIRECT horsepower measurement, bounded by the warehouse's real slot count.
  When the peak of that is far under the size's slot capacity, the warehouse is
  provably oversized and the finding is rated MEDIUM confidence, stating the
  measured utilisation explicitly. We ALWAYS recommend a single step down and
  price the saving on that step: a bigger size also speeds each individual query
  (an effect slot occupancy cannot see) and a 30-day window can miss a periodic
  heavy batch, so a multi-size jump on a throughput-only signal would be
  overreach. When the measured headroom implies the warehouse could ultimately go
  smaller, that deeper "floor" and its total saving are shown as CONTEXT for the
  operator to reach iteratively — change one notch, watch the KPIs, repeat — never
  folded into the recommended action.

* **Inferred (fallback).** When no task data is present, we fall back to the
  original proxy: a warehouse whose queries never queue for capacity, never spill
  to disk, and scan only small volumes has clear headroom. That is the absence of
  stress rather than a positive measurement, so it stays LOW confidence and
  proposes a single conservative step.

A size step down the ladder halves the DBU/hour rate (a property of the ladder),
but the same work has fewer task slots on the smaller size, so a compute-bound
query runs longer and claws back part of the rate cut. The rule prices that
elasticity explicitly: candidate = current x size_ratio x runtime_ratio (see
:func:`finops_rules.savings.warehouse_downsize_savings`). The runtime_ratio is
MEASURED from the slot signal — peak demand over the target size's slots — not a
guessed fraction; when the peak still fits the smaller size it is 1.0 and the full
rate cut is realised, which is the only case the rule books today. What differs
between tiers is *how many* steps are judged safe and how confident we are. A
larger size also speeds each individual query — an effect slot occupancy does not
capture — so even the measured tier stays deliberately conservative and tells the
operator to watch the KPIs after the change.

**Phased (2026-10-06, docs/ADDING_A_RULE.md "Rules that reduce capacity").** The
recommendation is still one size step, but the finding now LEADS with the full
saving: with measured slots and deeper headroom, ``monthly_savings_usd`` =
``monthly_savings_high_usd`` = the floor size's saving, ``monthly_savings_low_usd`` = the
one step's, and ``evidence.phasing`` carries the ladder plan (``phasing.ladder_plan``,
one rung per step). With measured slots and no deeper headroom the plan is the single
step. The inferred tier (no slot data) carries no plan: it measures no target, and its
low end is $0, not a step.
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
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_wh_rightsize,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.phasing import ladder_plan, observe_days, phasing_evidence
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import (
    WAREHOUSE_SIZE_LADDER,
    max_safe_steps_down_by_slots,
    warehouse_downsize_savings,
    warehouse_size_slots,
)

# Appended to both bases. The dollar figure prices
# the exact per-cluster DBU-rate halving of one ladder step. That is correct at list
# price, but a warehouse with multi-cluster auto-scaling may scale OUT to more clusters
# on the smaller size to absorb bursts the bigger size soaked up in one — in which case
# the realized cut is smaller than the modelled halving. The rule already excludes
# warehouses that queue today, so this is a post-change risk to watch, not a present
# one; we disclose it rather than guess a discount.
_WATCH = "queued-for-capacity %, spill % and p95 query runtime"

_MULTICLUSTER_CAVEAT = (
    " The saving prices the exact per-cluster rate halving; if the warehouse uses "
    "multi-cluster auto-scaling it may scale out to more clusters on the smaller size "
    "under burst, realizing less than that — watch the running cluster count after the "
    "change, not only query latency."
)

# Two data bases, because the rule now has two evidence tiers. When measured Spark
# slot occupancy is available it is the primary signal (a direct horsepower
# measurement) and confidence rises; when it is not, the rule falls back to the
# original proxy-only, low-confidence behaviour so nothing regresses.
_BASIS_SLOTS = (
    "system.query.history measured Spark SLOT occupancy (total_task_duration_ms per "
    "minute / 60 = slots busy) + queue wait, spill, scanned bytes + billing.usage x "
    "list_prices, {n}-day lookback. Peak slot demand far under the size's capacity "
    "gates the finding; we recommend ONE step down (the exact rate halving) and, when "
    "the headroom implies more, show the deeper floor as context to reach one step at "
    "a time. Medium confidence — slot occupancy is measured, but a larger size also "
    "speeds each query, an effect this signal doesn't capture, so we stay conservative."
    + _MULTICLUSTER_CAVEAT
)
_BASIS = (
    "system.query.history (queue wait, spill, scanned bytes, concurrency) + "
    "billing.usage x list_prices, {n}-day lookback. Warehouses expose no CPU and no "
    "task data was available here, so size is inferred from query behaviour — low "
    "confidence; the saving prices a single ladder step (exact rate halving), deeper "
    "downsizing left to confirm."
    + _MULTICLUSTER_CAVEAT
)


@register
class WarehouseRightsizeRule(Rule):
    id = "warehouse_rightsize"
    # Savings model: stays ESTIMATED. The saving now prices runtime elasticity
    # explicitly (candidate = current x size_ratio x runtime_ratio, see
    # savings.warehouse_downsize_savings): a smaller size runs compute-bound work
    # longer, clawing back part of the rate cut. When the MEASURED slot peak still
    # fits the target size the runtime_ratio is 1.0 (full cut); a larger size also
    # speeds each query — an effect slot occupancy can't see — so it is never booked.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Right-size an over-provisioned SQL warehouse"
    resource_type = "warehouse"
    # "Saved so far" (finops_rules.change_tracking): size moved down the ladder. When
    # evidence.phasing exists its data target is the full-credit proposal and its
    # three points value a partial move; else each step halves the rate (ladder).
    tracked_setting = TrackedSetting(
        resource="warehouse", column="warehouse_size",
        baseline_key="warehouse_size", proposed_key="proposed_size",
        direction="decrease", valuation="ladder", ladder=tuple(WAREHOUSE_SIZE_LADDER),
        uses_phasing=True, label="Size",
    )
    category = "sql"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "warehouse_rightsize.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. min_queries can be met inside one busy
        # afternoon, and this rule already infers rather than measures headroom —
        # downsizing on a one-day sample is how you cause a regression.
        # See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(t.get("min_monthly_cost_usd", 100)):
            return None  # too little spend to be worth the downsize risk

        size = row.get("warehouse_size")

        # Measured Spark slot occupancy, when the query history carried task data.
        # This is the direct horsepower signal; when present it GATES the
        # recommendation (a size with no slot headroom is not over-provisioned) and
        # raises confidence. When absent (None), we keep the original proxy-only,
        # low-confidence behaviour — no regression.
        peak_slots = row.get("peak_slots_used")
        size_slots = warehouse_size_slots(size or "")
        have_slots = peak_slots is not None and size_slots is not None

        # How many steps the measured headroom COULD support — used only as context
        # (the "potential floor"), never as the recommended move. We always
        # recommend ONE step: a bigger size also speeds each individual query, an
        # effect slot occupancy cannot see, and a 30-day window can miss a periodic
        # heavy batch — so the safe practice is change one notch, observe the KPIs,
        # then repeat. Jumping several sizes at once on a throughput-only signal is
        # exactly the overreach an operator should not do blind.
        if have_slots:
            potential_steps = max_safe_steps_down_by_slots(
                current_size=size or "",
                peak_slots_used=float(peak_slots),
                headroom_factor=float(t.get("slot_headroom_factor", 2.0)),
                max_steps=int(t.get("max_steps_down", 8)),
            )
            if potential_steps < 1:
                # Peak slot demand does not leave a safe margin even for one step —
                # the measured signal says this size is NOT over-provisioned, so we
                # trust it and say nothing rather than fall back to the weaker proxy.
                return None
        else:
            potential_steps = int(t.get("steps_down", 1))

        # The RECOMMENDED move is always a single step, priced on that step.
        steps = 1
        # Runtime elasticity: a smaller size has fewer task slots, so
        # compute-bound work runs longer and claws back part of the rate cut. With
        # the measured slot signal, runtime_ratio is the peak demand over the target
        # size's slots (floored at 1); the slot-headroom gate above guarantees the
        # peak still fits the smaller size, so this is 1.0 today (the full rate cut is
        # realised) — but it discounts automatically if the peak ever approaches the
        # target. No slot signal => 1.0, the stated best-case upper bound (low conf).
        step_ratio = _runtime_ratio(peak_slots, size_slots, steps) if have_slots else 1.0
        savings, proposed = warehouse_downsize_savings(
            monthly_cost=monthly_cost,
            current_size=size or "",
            steps_down=steps,
            runtime_ratio=step_ratio,
        )
        if savings <= 0 or not proposed:
            return None

        # The size the measured headroom suggests is ultimately reachable, and the
        # extra saving beyond the first step — shown as context so the full
        # opportunity stays visible without recommending a blind multi-size jump.
        # Priced with the elasticity of the DEEPER move, so a floor that would start
        # to bottleneck the peak is discounted rather than over-promised.
        floor_ratio = (
            _runtime_ratio(peak_slots, size_slots, potential_steps) if have_slots else 1.0
        )
        floor_savings, floor_size = warehouse_downsize_savings(
            monthly_cost=monthly_cost,
            current_size=size or "",
            steps_down=potential_steps,
            runtime_ratio=floor_ratio,
        )
        has_further = have_slots and potential_steps > 1 and floor_size != proposed

        # Savings band (see docs/SAVINGS_CONFIDENCE_MODEL.md). The point estimate
        # `savings` is the single measured step. The band uses the rule's OWN
        # decomposition, not a blanket percentage:
        #   * no slot signal (low confidence) -> the step is inferred from query
        #     behaviour, so it's the BEST case, not a floor: low = $0 (it may not
        #     hold), high = the full one-step cut.
        #   * measured slots WITH deeper reachable headroom -> the single step is the
        #     conservative floor and the deeper size the measured headroom could
        #     ultimately reach is the upside: low = step saving, high = floor saving.
        #   * measured slots, no deeper headroom -> a proven single step with little
        #     dollar uncertainty: band stays collapsed.
        range_basis: str | None = None
        if not have_slots:
            low_savings, high_savings = 0.0, savings
            range_basis = (
                f"Low = $0 — with no Spark task data the target size is inferred from "
                f"query behaviour, so treat it as directional; high = the full "
                f"one-step rate cut if {_fmt(proposed)} keeps up with the workload."
            )
        elif has_further:
            low_savings, high_savings = savings, max(savings, floor_savings)
            range_basis = (
                f"Low = the one measured step to {_fmt(proposed)} (peak slot demand "
                f"fits it with room to spare); high = {_fmt(floor_size)}, the deeper "
                f"size the measured headroom could ultimately reach — one step at a "
                f"time, confirming each before the next. The figure shown is the full "
                f"saving at {_fmt(floor_size)}."
            )
        else:
            low_savings, high_savings = savings, savings
        # Lead with the full saving (phased downsizing): the point is what the data
        # supports, the first step is the low end. Unchanged without deeper headroom,
        # and in the inferred tier (its point stays the one-step best case).
        point_savings = high_savings if has_further else savings

        # The step plan, only where a target is MEASURED (slot data). The inferred tier
        # has no measured target and a $0 low end, so it carries no plan.
        phasing = None
        if have_slots:
            plan = (
                ladder_plan(size or "", floor_size, WAREHOUSE_SIZE_LADDER)
                if has_further else [proposed]
            ) or [proposed]
            phasing = phasing_evidence(
                "Cluster size",
                (size or "").upper().replace("XSMALL", "X_SMALL"),
                plan,
                observe_days=observe_days(ctx),
                watch=_WATCH,
                step_savings=low_savings,
                target_savings=point_savings,
            )

        queued_pct = float(row.get("queued_pct") or 0.0) * 100
        spilled_pct = float(row.get("spilled_pct") or 0.0) * 100
        p95_runtime = float(row.get("p95_runtime_s") or 0.0)
        peak_conc = int(row.get("peak_concurrency") or 0)
        p95_read_gb = float(row.get("p95_read_gb") or 0.0)
        name = row.get("warehouse_name") or str(row["warehouse_id"])

        # The measured slot sentence, stated explicitly when we have the signal — so
        # the reader knows the recommendation rests on a horsepower MEASUREMENT, not
        # only on the absence of stress symptoms.
        slot_pct = (
            (float(peak_slots) / size_slots * 100.0) if have_slots else 0.0
        )
        # A "further headroom" clause, shown when the measured peak suggests the
        # warehouse could ultimately go smaller than one step — surfaced as context
        # for iteration, NEVER folded into the recommended action or its dollar figure.
        further_clause = ""
        if has_further:
            further_clause = (
                f" That utilisation suggests it could ultimately reach as low as "
                f"{_fmt(floor_size)} (~${floor_savings:,.0f}/mo total) — but do it "
                f"one size at a time, confirming each step before the next, not in a "
                f"single jump."
            )
        if have_slots:
            slot_clause = (
                f"Measured Spark slot utilisation is the primary signal here: at its "
                f"busiest minute this warehouse used only ~{float(peak_slots):.1f} of "
                f"~{size_slots} task slots ({slot_pct:.1f}% of the size's horsepower). "
            )
            conf_clause = (
                "It's a medium-confidence call — slot use is measured, but a larger "
                "size also speeds each individual query, so step down one notch and "
                "watch the KPIs before going further."
            )
        else:
            slot_clause = ""
            conf_clause = (
                "It's a low-confidence call (no Spark task data was available for this "
                "warehouse, so size is inferred from query behaviour) — treat it as: try "
                "one notch down if you can accept slightly slower queries."
            )

        why = (
            f"SQL warehouse '{name}' runs at {_fmt(size)} but its {int(row.get('queries') or 0):,} "
            f"queries over the last {ctx.lookback_days} days sit well inside the size's "
            f"headroom: {queued_pct:.1f}% queued for capacity, {spilled_pct:.1f}% spilled "
            f"to disk, p95 scanned only {p95_read_gb:.1f} GB. {slot_clause}"
            f"None of those is the bottleneck a bigger size buys off, so the same work fits "
            f"one size down at {_fmt(proposed)}. It spent ~${monthly_cost:,.0f}/mo; one step "
            f"down halves the DBU/hr rate for ~${savings:,.0f}/mo — queries run a little "
            f"slower on right-sized compute.{further_clause} {conf_clause} Watch the KPIs below."
        )
        how = (
            f"Change the warehouse size from {_fmt(size)} to {_fmt(proposed)} — one step down "
            f"(SQL Warehouses > the warehouse > Edit > Cluster size). Then watch these "
            f"KPIs for a few days: queued-for-capacity % (stays near {queued_pct:.1f}%), "
            f"spill % (stays at {spilled_pct:.1f}%), p95 query runtime "
            f"(baseline {p95_runtime:.1f}s — expect a small rise), and — if the warehouse "
            f"auto-scales — the running cluster count (a smaller size may scale out to "
            f"more clusters under burst, offsetting part of the saving). If queueing or "
            f"spill appears, step back up; if all hold, "
            + (
                f"repeat toward {_fmt(floor_size)} one size at a time."
                if has_further
                else "consider a further step down."
            )
        )

        viz = viz_wh_rightsize(
            current_size=(size or "").upper().replace("XSMALL", "X_SMALL"),
            proposed_size=proposed,
            queued_pct=queued_pct,
            max_queued_pct=float(t.get("max_queued_pct", 0.02)) * 100,
            spilled_pct=spilled_pct,
            max_spilled_pct=float(t.get("max_spilled_pct", 0.01)) * 100,
            p95_read_gb=p95_read_gb,
            max_p95_read_gb=float(t.get("max_p95_read_gb", 10)),
            p95_runtime_s=p95_runtime,
            peak_concurrency=peak_conc,
            peak_slots_used=float(peak_slots) if have_slots else None,
            size_slots=size_slots if have_slots else None,
            floor_size=floor_size if has_further else None,
            floor_savings_usd=floor_savings if has_further else None,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=point_savings,
            monthly_savings_low_usd=low_savings,
            monthly_savings_high_usd=high_savings,
            effort=EffortBand.LOW,  # a size dropdown, but warrants a watch period
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                # Measured slot occupancy raises confidence from the proxy-only floor.
                confidence=CONFIDENCE_MEDIUM if have_slots else CONFIDENCE_LOW,
                data_basis=(
                    (_BASIS_SLOTS if have_slots else _BASIS).format(n=ctx.lookback_days)
                    + history_note(row, ctx) + coverage_note(row)
                ),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_size=size,
                # Carried so the detail page can judge serverless fit even when
                # this is the only warehouse rule that fired (serverless.fit).
                warehouse_type=row.get("warehouse_type"),
                proposed_size=proposed,
                queries=row.get("queries"),
                queued_pct=row.get("queued_pct"),
                spilled_pct=row.get("spilled_pct"),
                p95_read_gb=row.get("p95_read_gb"),
                p95_runtime_s=row.get("p95_runtime_s"),
                peak_concurrency=peak_conc,
                # Measured slot signal (present only when task data existed).
                peak_slots_used=float(peak_slots) if have_slots else None,
                size_slots=size_slots if have_slots else None,
                slot_util_pct=round(slot_pct, 1) if have_slots else None,
                # Always one step is RECOMMENDED; the deeper floor the measured
                # headroom suggests is carried as context for iteration, not action.
                steps_down=steps,
                potential_steps_down=potential_steps if have_slots else None,
                floor_size=floor_size if has_further else None,
                floor_savings_usd=floor_savings if has_further else None,
                # The one-step saving, kept by name now that the point is the floor's.
                step_savings_usd=savings,
                **({"phasing": phasing} if phasing else {}),
                dbus_30d=row.get("dbus_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
                # What the low/high ends of the band mean for THIS warehouse, shown
                # verbatim on the Data Quality panel (only when the band is widened).
                **({"savings_range_basis": range_basis} if range_basis else {}),
            ),
        )


def _runtime_ratio(peak_slots, size_slots: int | None, steps_down: int) -> float:
    """Measured runtime elasticity for a ``steps_down`` move: peak slot demand over
    the TARGET size's slots, floored at 1.0.

    A step down the ladder halves the slots, so the target has
    ``size_slots / 2**steps_down`` of them. If the peak still fits (ratio < 1) the
    work does not slow down and the full rate cut is realised (returns 1.0); if the
    peak would spill past the smaller size (ratio > 1) the work runs proportionally
    longer, and warehouse_downsize_savings discounts the saving by that factor."""
    if peak_slots is None or not size_slots:
        return 1.0
    target_slots = float(size_slots) / (2 ** steps_down)
    if target_slots <= 0:
        return 1.0
    return max(1.0, float(peak_slots) / target_slots)


def _fmt(size) -> str:
    """Pretty-print a ladder size (2X_SMALL -> '2X-Small')."""
    if not size:
        return "unknown"
    s = str(size).upper().replace("XSMALL", "X_SMALL")
    return s.replace("_", "-").title().replace("X-", "X-")
