"""Rule: move scheduled work off all-purpose clusters.

Automated jobs running on all-purpose (interactive) compute pay roughly ~2x the
job-compute DBU rate. Moving these runs to job compute / serverless jobs saves
the per-DBU price gap.

The cluster is judged AS A WHOLE, not job-by-job. The saving is priced in SQL
from the REAL billed all-purpose DBUs the cluster's jobs actually burned —
attributed by the cluster's job-active UPTIME share (WALL-CLOCK minutes a job
task ran / WALL-CLOCK minutes the cluster was running), times the REAL
``all_purpose_price - jobs_compute_price`` gap from ``list_prices``.

Attributing by uptime share (not by each job's share of total *job* runtime)
is what stops a mostly-idle cluster's whole cost being dumped onto a few short
runs: three ~11-second jobs on a 99%-idle streaming cluster used ~1% of its
uptime, so only ~1% of its DBUs are reassignable to job compute — the idle 99%
is auto-termination's problem, not a migration saving.

Savings model:

* **Units.** ``job_active_frac`` divides WALL-CLOCK job minutes by WALL-CLOCK
  running minutes (both distinct clock minutes), so the reassignable fraction is
  invariant to cluster size. The old denominator was ``COUNT(*)`` node-minutes,
  which shrank the fraction on bigger clusters (the unit bug).
* **Dedicated vs shared counterfactual.** On a job-DEDICATED cluster (jobs occupy
  ``>= dedicated_active_frac`` of the uptime — essentially no interactive/idle
  time) moving the jobs off ELIMINATES the all-purpose cluster, so the measured
  price gap is genuinely realized and the finding is **bookable** (low == point).
  On a SHARED/interactive cluster the cluster may keep running for interactive use
  after the jobs leave, so removing them is not proven to reduce source spend: the
  lower bound is **$0** and the attributed price gap is the upper scenario only —
  the finding stays **estimated** (out of the booked headline). When ``list_prices``
  carries no JOBS_COMPUTE counterpart to measure the gap, the fixed-fraction
  fallback is a heuristic, not a counterfactual, so the finding is **advisory**.

``score()`` applies the min-cost bar to the cluster's reassignable spend and emits
a single finding per cluster, carrying the per-job breakdown as evidence for the
detail view.
"""

from __future__ import annotations

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
    coverage_supports_booking,
    underpriced,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.billing.usage (ALL_PURPOSE DBUs) attributed to the cluster's jobs by "
    "job-active uptime share (job-task WALL-CLOCK minutes / cluster running "
    "WALL-CLOCK minutes from system.compute.node_timeline) x the real list_prices "
    "all-purpose vs jobs-compute gap, {n}-day lookback. The cluster is judged as a "
    "whole: only the DBUs burned while a job was actually running are counted as "
    "reassignable to job compute, so a mostly-idle cluster's idle cost is not "
    "mistaken for a migration saving."
)


@register
class InteractiveToJobRule(Rule):
    id = "interactive_to_job"
    # Savings model: the counterfactual now splits DEDICATED (bookable —
    # removing the jobs eliminates the all-purpose cluster) from SHARED (estimated,
    # low=0 — the cluster may keep running for interactive use). The class default is
    # ESTIMATED (the shared tier and the safe fallback); score() overrides individual
    # dedicated findings to bookable.
    default_savings_status = "estimated"
    version = "4.0.0"
    title = "Move scheduled work off all-purpose clusters"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "counterpart"  # savings = ALL_PURPOSE vs JOBS price gap
    sql_file = "interactive_to_job.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge the cluster's habitual pattern. See
        # finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        # PROTOTYPE (recency fix): the SQL now measures cost/savings over the short
        # recency window, so normalize to a month by recency_days, not lookback_days.
        # A cluster stopped days ago contributes ~0 recent spend and drops out.
        recency_days = int(t.get("recency_days", 7))

        # The reassignable spend: the all-purpose cost of only the DBUs jobs
        # actually burned on the cluster (uptime-attributed). This — not the
        # cluster's whole bill — is what gates the recommendation, because the
        # idle remainder cannot be reclaimed by moving jobs to job compute.
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, recency_days)
        if monthly_cost < t["min_monthly_cost_usd"]:
            return None  # too little reassignable spend to be worth the migration

        # Real price-delta savings computed in SQL (attributed DBUs x
        # (all_purpose_price - jobs_compute_price)), scaled to a monthly figure.
        # Fall back to a conservative fraction only if the price gap is missing
        # (e.g. list_prices lacks a jobs-compute SKU for this cloud).
        fallback_frac = float(t.get("fallback_savings_fraction", 0.30))
        dedicated = bool(row.get("is_dedicated"))
        window_savings = row.get("savings_30d_usd")
        measured = window_savings is not None and float(window_savings) > 0

        # Whether the measured gap is actually BOOKABLE: a job-dedicated cluster whose
        # spend is well-enough priced to trust. Drives both the tier AND the narrative
        # below, so the copy can never claim a realized gap the tier does not book.
        booked = measured and dedicated and coverage_supports_booking(row)
        # What the band's ends mean for this cluster (see SAVINGS_CONFIDENCE_MODEL.md);
        # None when the band is collapsed (bookable). Shown on the Data Quality panel.
        range_basis: str | None = None
        if measured:
            # The measured, region-correct price gap on the reassignable DBUs.
            point = min(to_monthly(float(window_savings), recency_days), monthly_cost)
            if booked:
                # Job-only cluster: removing the jobs eliminates the all-purpose
                # cluster, so the measured gap is realized. Bookable, low == point.
                low, high, status = point, point, "bookable"
            else:
                # Shared/interactive (or coverage too thin to book): the cluster may
                # keep running after the jobs leave, so the removal is not proven to
                # cut source spend. Lower bound $0; the attributed gap is the upper
                # scenario only.
                low, high, status = 0.0, point, "estimated"
                range_basis = (
                    "Low = $0 — this cluster is also used interactively, so moving the "
                    "jobs to job compute may not let it shrink; high = the full measured "
                    "all-purpose-vs-jobs price gap if it does."
                )
        else:
            # No jobs-compute rate in list_prices for this cloud => the gap cannot be
            # MEASURED. The fixed fraction is a heuristic, not a counterfactual, so it
            # is advisory (shown/rankable, books nothing) rather than a booked figure.
            point = min(monthly_cost * fallback_frac, monthly_cost)
            low, high, status = 0.0, point, "advisory"
            range_basis = (
                f"Low = $0 — the jobs-compute rate wasn't in list_prices for this "
                f"cloud, so the figure is a conservative {fallback_frac:.0%} of the "
                f"reassignable spend, not a measured price gap."
            )

        savings = point
        if savings <= 0:
            return None

        # Counterpart contract pricing (finops_rules.price_basis): the same gap, priced
        # at contract rates on BOTH SKUs by the SQL, with the same tier shape as the
        # list figure. Only for a MEASURED gap — the fixed-fraction fallback has no
        # contract counterpart. The engine applies the coverage gate afterwards.
        contract_window = row.get("savings_30d_contract_usd")
        contract_point = None
        if measured and contract_window is not None:
            contract_point = min(to_monthly(float(contract_window), recency_days), monthly_cost)

        job_count = int(row.get("job_count") or 0)
        recurring = int(row.get("recurring_job_count") or 0)
        run_count = int(row.get("run_count") or 0)
        attributed_dbus = float(row.get("attributed_dbus") or 0.0)
        active_frac = float(row.get("job_active_frac") or 0.0)
        name = row.get("cluster_name") or str(row["cluster_id"])

        # Phrase the count honestly: one job vs several sharing the cluster.
        if recurring <= 1:
            jobs_phrase = "A recurring job"
        else:
            jobs_phrase = f"{recurring} recurring jobs"
        # Be honest about which path priced the saving and how much trust it carries:
        #   * dedicated + measured  -> the exact gap is realized (High, bookable);
        #   * shared + measured     -> the gap is the upper scenario, low bound $0
        #                              (High measurement, but estimated);
        #   * no jobs-compute rate   -> conservative fixed fraction (Medium, advisory).
        if booked:
            savings_clause = (
                f"This cluster is effectively job-dedicated (jobs ran for "
                f"~{active_frac * 100:.0f}% of its uptime), so moving them to job "
                f"compute retires the all-purpose cluster and realizes the exact "
                f"price gap — ~${savings:,.0f}/mo."
            )
        elif measured:
            savings_clause = (
                f"Job compute bills the same DBUs at a lower rate: up to "
                f"~${savings:,.0f}/mo of the exact price gap. Because the cluster is "
                f"also used interactively, moving the jobs off may not shrink it, so "
                f"this is an upper estimate (lower bound $0) rather than booked savings."
            )
        else:
            savings_clause = (
                f"The jobs-compute price wasn't available in list_prices for this "
                f"cluster's cloud, so the saving is estimated conservatively at "
                f"~{fallback_frac:.0%} of the reassignable spend (~${savings:,.0f}/mo) — "
                f"the low end of the typical all-purpose-vs-jobs gap. Advisory only."
            )
        why = (
            f"{jobs_phrase} ran on all-purpose (interactive) cluster '{name}' "
            f"({run_count} run(s) over the last {recency_days} days). Judged "
            f"across the whole cluster, jobs were active for ~{active_frac * 100:.0f}% "
            f"of its billed uptime, burning ~{attributed_dbus:,.0f} DBUs (~${monthly_cost:,.0f}/mo) "
            f"at the pricier all-purpose rate. {savings_clause} (Any idle "
            f"time between runs is a separate auto-termination question, not counted here.)"
        )
        how = (
            "Move these scheduled runs onto job compute (or serverless jobs), which "
            "bill at the lower job-compute DBU rate — for each job: Workflows > the "
            "job > swap the all-purpose cluster for a job cluster / serverless. "
            "Serverless jobs also scale to zero between runs, removing the idle cost "
            "entirely."
        )

        # High only when the exact gap was measured; Medium on the fixed-fraction
        # fallback.
        confidence = CONFIDENCE_HIGH if measured else CONFIDENCE_MEDIUM
        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            monthly_savings_contract_usd=(
                None if contract_point is None else round(contract_point, 2)),
            monthly_savings_contract_low_usd=(
                None if contract_point is None else round(contract_point if booked else 0.0, 2)),
            monthly_savings_contract_high_usd=(
                None if contract_point is None else round(contract_point, 2)),
            # Bookable only for a job-dedicated cluster whose gap we measured; shared
            # clusters stay estimated (low=0), and the unmeasured fallback is advisory.
            savings_status=status,
            effort=EffortBand.HIGH,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=confidence,
                data_basis=_BASIS.format(n=recency_days)
                + (
                    ""
                    if measured
                    else (
                        f" The jobs-compute SKU price was unavailable for this cloud, so "
                        f"the saving is a conservative {fallback_frac:.0%}-of-reassignable-"
                        f"spend fallback (the low end of the 30-55% gap), not the measured "
                        f"delta — advisory only, contributing nothing to booked savings."
                    )
                )
                + history_note(row, ctx) + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                is_dedicated=dedicated,
                job_count=job_count,
                recurring_job_count=recurring,
                run_count=run_count,
                runtime_hours=row.get("runtime_hours"),
                running_minutes=row.get("running_minutes"),
                job_active_minutes=row.get("job_active_minutes"),
                job_active_frac=row.get("job_active_frac"),
                attributed_dbus=row.get("attributed_dbus"),
                dbus_30d=row.get("dbus_30d"),
                price_all_purpose=row.get("price_all_purpose"),
                price_jobs_compute=row.get("price_jobs_compute"),
                cost_30d_usd=row.get("cost_30d_usd"),
                savings_30d_usd=window_savings,
                job_breakdown=row.get("job_breakdown"),
                **({"savings_range_basis": range_basis} if range_basis else {}),
            ),
        )
