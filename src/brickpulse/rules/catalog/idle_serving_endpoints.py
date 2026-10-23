"""Rule: shut down (or scale to zero) an idle model serving endpoint.

Model serving was a blind spot: every rule in this catalog covered clusters,
warehouses, or jobs, so an always-on endpoint serving nobody billed 24/7 with
nothing to flag it.

This rule has the STRONGEST evidence of any rule here. Every other idleness rule
infers disuse from a proxy (lineage reads, CPU, run history);
``system.serving.endpoint_usage`` is a per-REQUEST log, so "nobody calls this"
is measured, not inferred. That is why it is the only ``CONFIDENCE_HIGH``
idleness rule.

Two guards carry the correctness of this rule, both verified against a live
account (see the SQL header for the measurements):

1. **Only endpoints that bill while idle can have idle waste.** Pay-per-token
   Foundation Model and external-model endpoints cost nothing when idle, so
   "delete this idle endpoint" would save $0. The SQL keeps only provisioned
   throughput and endpoints with ``min_concurrency > 0`` (scale-to-zero
   disabled). Measured: ``min_concurrency > 0`` endpoints billed 15.0/15 days
   (avg 2806 DBU); scale-to-zero ones billed 5.8/15 days (avg 277 DBU).

2. **A missing entity join is "unknown", never "idle".** ``served_entities``
   covered only 92 of 591 billing endpoint_ids over a 14-day window, so a naive
   left join reads unmatched endpoints as zero-traffic. Scoring an unmatched row
   would recommend deleting BUSY endpoints — the same class of error as pricing
   a rule off the wrong SKU. ``score()`` therefore requires ``entity_matched``.

3. **Zero LOGGED requests is not proof of zero requests.** ``endpoint_usage`` only
   records endpoints with usage tracking enabled, and no system table exposes that
   setting. So a zero-traffic endpoint is split on whether it has EVER logged a
   request in the retained history (``requests_retained``): if it has, tracking is
   demonstrably on and the idleness is measured (HIGH, bookable); if it never has,
   "idle" and "untracked" are indistinguishable, so the finding drops to MEDIUM /
   estimated and carries a ``data_warning`` the UI shows above everything else.
   Measured live: 35 of 40 zero-traffic candidates had never logged a request.
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
from brickpulse.rules.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_serving_traffic,
)
from brickpulse.rules.coverage import coverage_supports_booking
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import idle_serving_endpoint_savings

_BASIS = (
    "system.serving.endpoint_usage (per-request log) + serving.served_entities "
    "x billing.usage (usage_metadata.endpoint_id) x list_prices, {n}-day "
    "lookback. Requests are COUNTED, not inferred, and only endpoints that bill "
    "while idle (provisioned throughput, or min_concurrency > 0) are considered."
)

# Shown verbatim as the finding's warning callout when zero traffic can't be told
# apart from usage tracking being off (see guard 3 in the module docstring).
_UNTRACKED_WARNING = (
    "No request has ever been logged for this endpoint — not in this window and not in "
    "the ~90 days of request history system tables keep. That is what an unused "
    "endpoint looks like, but it is also exactly what you see when usage tracking is "
    "turned off for it: system.serving.endpoint_usage only records endpoints with usage "
    "tracking enabled, and no system table says whether it is. Check the endpoint's "
    "Metrics tab (or ask its owner) before acting."
)


def _is_gpu_compute(compute_type) -> bool:
    """True when the endpoint's served-entity ``compute_type`` is a GPU tier.

    ``custom_model_config.compute_type`` is a string like ``CPU`` / ``GPU_SMALL`` /
    ``GPU_MEDIUM`` / ``GPU_LARGE`` / ``MULTIGPU_MEDIUM``, so any tier containing
    "GPU" is GPU-backed. GPU serving bills at a large multiple of CPU serving, so an
    idle GPU endpoint is among the most expensive waste in the catalog — worth
    surfacing more prominently and pointing at the extra CPU-downgrade lever."""
    return "gpu" in str(compute_type or "").lower()


def _normalize_days(days) -> list[dict]:
    """Collapse the SQL's per-day (billed, requests) rows into the chart series.

    The SQL unions two sources (a billing row and a request row per day), so the
    same date can appear twice; we merge on date and keep one entry per day."""
    merged: dict[str, dict] = {}
    for entry in days or []:
        if entry is None:
            continue
        # Spark Rows behave like mappings via asDict; dicts pass through.
        row = entry.asDict() if hasattr(entry, "asDict") else dict(entry)
        day = row.get("d")
        if day is None:
            continue
        day = str(day)
        slot = merged.setdefault(day, {"d": day, "w": 0, "r": 0})
        slot["w"] = max(slot["w"], int(row.get("w") or 0))
        slot["r"] += int(row.get("r") or 0)
    return [merged[d] for d in sorted(merged)]


@register
class IdleServingEndpointsRule(Rule):
    id = "idle_serving_endpoints"
    version = "1.0.0"
    title = "Shut down or scale to zero an idle serving endpoint"
    resource_type = "serving_endpoint"
    # "Saved so far" (finops_rules.change_tracking): deletion (generic), or scale-to-zero
    # turned on (min_concurrency 0, the same signal this rule's gate reads). Binary.
    tracked_setting = TrackedSetting(
        resource="serving_endpoint", column="min_concurrency",
        baseline_key="min_concurrency", proposed_value=0, direction="decrease",
        valuation="binary", label="Scale to zero",
    )
    category = "serving"
    sql_file = "idle_serving_endpoints.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Guard 2 (see module docstring): without a matched entity row we have no
        # request data for this endpoint, and absence of data is NOT absence of
        # traffic. Suppress rather than guess.
        if not int(row.get("entity_matched") or 0):
            return None

        # Too little history to judge: an endpoint deployed two days ago has
        # billed for two days and served nobody yet because it is NEW.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds

        days_billed = int(row.get("days_billed") or 0)
        if days_billed < int(t.get("min_days_billed", 7)):
            return None  # not billing consistently => not an always-on cost

        monthly_cost = to_monthly(row.get("cost_window_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(t.get("min_monthly_cost_usd", 25)):
            return None  # below the floor, not worth an admin's attention

        min_concurrency = int(row.get("min_concurrency") or 0)
        is_pt = bool(int(row.get("is_provisioned_throughput") or 0))
        # GPU serving compute costs a large multiple of CPU serving, so an idle GPU
        # endpoint is far more expensive waste than an idle CPU one. It doesn't change
        # WHETHER we flag (the idle-cost logic below is identical), only how loudly
        # (priority) and the extra lever we suggest (re-evaluate CPU compute).
        is_gpu = _is_gpu_compute(row.get("compute_type"))
        # Scale-to-zero is the cheap config fix; provisioned throughput has no
        # such toggle, so there the lever is removing the reserved capacity.
        scale_to_zero_available = min_concurrency > 0 and not is_pt

        requests = int(row.get("requests_window") or 0)
        # Two tiers:
        #  * zero traffic  -> full always-on cost is reclaimable, HIGH confidence
        #    (requests are counted, not inferred).
        #  * low traffic   -> too few requests to justify always-warm replicas;
        #    scale-to-zero reclaims most of the idle cost at a cold-start cost. The
        #    idle share is estimated, so MEDIUM confidence and a conservative credit.
        #    Only when scale-to-zero is available (provisioned throughput has no
        #    cheap lever, so it stays a zero-traffic-only, delete-or-nothing rule).
        req_per_day = requests / max(1, days_billed)
        low_traffic_cap = float(t.get("low_traffic_max_requests_per_day", 100))
        is_zero_traffic = requests <= int(t.get("max_requests", 0))
        # Guard 3: zero traffic is only MEASURED when tracking demonstrably records
        # this endpoint, i.e. it has logged at least one request at some point.
        # The retained history includes the window, so it can never be smaller.
        retained = max(int(row.get("requests_retained") or 0), requests)
        tracking_unverified = is_zero_traffic and retained <= 0
        is_low_traffic = (
            not is_zero_traffic
            and scale_to_zero_available
            and req_per_day <= low_traffic_cap
        )
        if not is_zero_traffic and not is_low_traffic:
            return None  # genuinely in use => not idle

        savings = idle_serving_endpoint_savings(
            monthly_cost=monthly_cost,
            requests=requests,
            scale_to_zero_available=scale_to_zero_available,
            treat_as_zero_traffic=is_zero_traffic,
            is_low_traffic=is_low_traffic,
            low_traffic_reclaim_frac=float(t.get("low_traffic_reclaim_frac", 0.7)),
        )
        if savings <= 0:
            return None

        name = row.get("endpoint_name") or str(row["endpoint_id"])
        entity = row.get("entity_name") or row.get("entity_type") or "model"

        if is_low_traffic:
            # Serving a little, but far too little to justify always-warm replicas.
            cause = (
                f"it averages only ~{req_per_day:.1f} request(s)/day yet keeps warm "
                f"replicas billed around the clock (scale-to-zero is disabled, "
                f"min_concurrency={min_concurrency})"
            )
            how = (
                f"Enable scale-to-zero on '{name}' by setting min_provisioned_"
                f"concurrency to 0 (Serving > {name} > Edit). At ~{req_per_day:.1f} "
                f"requests/day it will sit scaled to zero most of the time and cost "
                f"almost nothing, at the price of a cold start on the first request "
                f"after an idle period. Confirm that cold-start latency is acceptable "
                f"for this endpoint's callers before switching."
            )
        elif scale_to_zero_available:
            cause = (
                f"scale-to-zero is disabled (min_concurrency={min_concurrency}), so "
                f"it keeps warm replicas billed around the clock"
            )
            how = (
                f"Confirm nothing calls '{name}' (check apps, jobs, and agents that "
                f"may be between releases), then either enable scale-to-zero by "
                f"setting min_provisioned_concurrency to 0, or delete the endpoint "
                f"(Serving > {name} > Edit). Scale-to-zero keeps the endpoint "
                f"addressable and costs nothing while idle, at the price of a cold "
                f"start on the next request — the safer first step if you are unsure."
            )
        else:
            cause = "it reserves provisioned throughput capacity, which bills continuously"
            how = (
                f"Confirm nothing calls '{name}', then delete the endpoint or drop its "
                f"provisioned throughput (Serving > {name} > Edit). Provisioned "
                f"throughput has no scale-to-zero: the reserved capacity bills whether "
                f"or not requests arrive. If the endpoint must stay reachable, move it "
                f"to a pay-per-token or scale-to-zero configuration."
            )

        # `date_sub(current_date(), N)` is inclusive on both ends, so an N-day
        # lookback spans N+1 calendar dates and a permanently-on endpoint reports
        # days_billed = N+1. Saying "billed on 31 of the last 30 days" would read
        # as a bug and undermine the finding, so a fully-billed window is stated
        # as such rather than as a fraction.
        billed_desc = (
            f"billed on every one of the last {ctx.lookback_days} days"
            if days_billed >= ctx.lookback_days
            else f"billed on {days_billed} of the last {ctx.lookback_days} days"
        )
        idle_word = "barely used" if is_low_traffic else "idle"
        if tracking_unverified:
            # Lead with the doubt: the reader must see it before the dollar claim.
            why = (
                f"Serving endpoint '{name}' (serving {entity}) {billed_desc}, costing "
                f"about ${monthly_cost:,.0f}/mo, and has NO logged inference requests — "
                f"but it has never logged a request at all, so usage tracking may simply "
                f"be off for it. If it really is unused, it is idle because {cause} and "
                f"the full always-on cost is reclaimable; that is not yet verified."
            )
            how = (
                f"First verify it is really unused: open Serving > {name} > Metrics and "
                f"check for request traffic, or ask its owner — the request log in system "
                f"tables only covers endpoints with usage tracking enabled. Only then: "
                + how[0].lower() + how[1:]
            )
        else:
            last = row.get("last_request_retained")
            last_clause = (
                f" Its last logged request was on {str(last)[:10]}, so usage tracking "
                f"is recording it — the silence since is real."
                if is_zero_traffic and last
                else ""
            )
            why = (
                f"Serving endpoint '{name}' (serving {entity}) {billed_desc}, costing about "
                f"${monthly_cost:,.0f}/mo, but served {requests} inference requests in "
                f"that window.{last_clause} It is {idle_word} because {cause}. Request "
                f"counts come from the per-request serving log, so this is measured rather "
                f"than inferred; "
                + (
                    f"the ~${savings:,.0f}/mo credited is the share scale-to-zero would "
                    f"reclaim (the endpoint still serves its occasional traffic)."
                    if is_low_traffic
                    else "the full always-on cost is reclaimable."
                )
            )
        if is_gpu:
            # GPU serving is the most expensive idle waste here; call it out and add
            # the CPU-downgrade lever on top of the scale-to-zero / delete advice.
            why += (
                f" This endpoint runs on GPU serving compute ({row.get('compute_type')}), "
                f"billed at a large multiple of CPU serving — idle GPU serving is among "
                f"the costliest waste to leave running."
            )
            how += (
                " Because this is GPU compute, also re-evaluate whether the model needs "
                "a GPU at all: many models meet their latency target on CPU, and moving "
                "compute_type to a CPU tier cuts the cost sharply even when the endpoint "
                "does see traffic."
            )

        viz = viz_serving_traffic(
            days=_normalize_days(row.get("days_series")),
            lookback_days=ctx.lookback_days,
            days_billed=days_billed,
            total_requests=requests,
            last_request=str(row.get("last_request_time")) if row.get("last_request_time") else None,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="serving_endpoint",
            resource_id=str(row["endpoint_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            # the zero-traffic tier is MEASURED (requests
            # counted, full always-on cost reclaimable) -> BOOKABLE. The low-traffic
            # tier estimates the idle share scale-to-zero would reclaim -> ESTIMATED,
            # out of the booked headline until it is recomputed from arrival gaps.
            # even the zero-traffic tier books only when coverage is KNOWN
            # adequate — an endpoint whose serving spend we cannot fully price (this
            # rule aggregates its own dbus_window/unpriced_window) drops to estimated
            # rather than booking a partial dollar as if it were the whole cost.
            # Guard 3: an endpoint that has never logged a request might just be
            # untracked, so its zero traffic is not measured -> estimated, not booked.
            savings_status="bookable"
            if is_zero_traffic
            and not tracking_unverified
            and coverage_supports_booking(
                row, dbu_key="dbus_window", unpriced_key="unpriced_window"
            )
            else "estimated",
            # Enabling scale-to-zero is a single config toggle; removing
            # provisioned throughput needs a capacity decision first.
            effort=EffortBand.LOW if scale_to_zero_available else EffortBand.MEDIUM,
            # GPU serving idle is disproportionately expensive, so it ranks higher.
            priority=RulePriority.HIGH if is_gpu else RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                # Zero traffic is counted, not inferred (HIGH). The low-traffic tier
                # estimates the idle share scale-to-zero would reclaim, so it is a
                # notch less certain (MEDIUM).
                confidence=(
                    CONFIDENCE_HIGH
                    if is_zero_traffic and not tracking_unverified
                    else CONFIDENCE_MEDIUM
                ),
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + (
                    " This endpoint has never logged a request in the retained history, "
                    "so zero traffic cannot be told apart from usage tracking being off "
                    "— confidence lowered and nothing booked until verified."
                    if tracking_unverified
                    else ""
                )
                + history_note(row, ctx),
                viz=viz,
                **history_evidence(row, ctx),
                traffic_tier=(
                    "zero_unverified" if tracking_unverified
                    else "zero" if is_zero_traffic else "low"
                ),
                usage_tracking_verified=not tracking_unverified,
                **({"data_warning": _UNTRACKED_WARNING} if tracking_unverified else {}),
                requests_retained=retained,
                last_request_retained=row.get("last_request_retained"),
                requests_per_day=round(req_per_day, 2),
                entity_type=row.get("entity_type"),
                entity_name=row.get("entity_name"),
                created_by=row.get("created_by"),
                min_concurrency=min_concurrency or None,
                compute_type=row.get("compute_type"),
                is_gpu_compute=is_gpu,
                provisioned_throughput=is_pt,
                requests_window=requests,
                distinct_requesters=int(row.get("distinct_requesters") or 0),
                last_request_time=row.get("last_request_time"),
                days_billed=days_billed,
                dbus_window=row.get("dbus_window"),
                cost_window_usd=row.get("cost_window_usd"),
            ),
        )
