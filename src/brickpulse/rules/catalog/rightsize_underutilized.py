"""Rule: rightsize underutilized clusters.

An all-purpose (UI) cluster whose average CPU sits well below its headroom
threshold is oversized: it is provisioned for far more work than it does.
Because cluster cost scales roughly linearly with worker count, cutting workers
cuts cost.

the target size and the saving are keyed on the MEASURED PEAK
(p95 CPU), not a blind halving with a flat haircut. A cluster is kept large
enough that its p95 CPU would still sit near ``target_utilization`` with
``headroom`` to spare after the shrink; the candidate worker count and the
saving are two views of the same ``keep_fraction``. This is what
:func:`finops_rules.savings.rightsize_savings` computes, and wiring it in fixes
the central flaw of the old flat 30%: a BURSTY cluster (low average CPU but a
high p95) now correctly yields ~0 saving and drops out, because shrinking it
would starve the peak it exists to serve. The saving is capped at
``max_savings_fraction`` — CPU is a proxy for the binding resource — so it stays
estimated, never booked.

CPU is the primary signal. Memory is a one-way *veto*, not a co-requirement: a
JVM cluster holds heap resident even when idle, so ``mem_used_percent`` routinely
sits at 40-70% on a CPU-idle cluster — requiring memory to *also* be below the
CPU headroom (the old ``AND avg_mem < 40``) hid genuinely-idle clusters. We
decline to downsize when memory is high enough (``mem_bound_pct``, default 85%)
that the cluster is plausibly memory-bound and shrinking it would risk OOM/spill
— and, because fewer workers concentrate the same data onto fewer nodes, the
veto tests the PEAK (p95) memory the smaller cluster would have to absorb, not
just the average.

**Phased (2026-10-06, docs/ADDING_A_RULE.md "Rules that reduce capacity").** The peak
may support 64 -> 4 workers, but CPU is a proxy for whatever binds first, so the
finding recommends halving per step (``phasing_max_step_ratio: 2``: 64 -> 32 -> 16 -> 8
-> 4) and leads with the FULL saving: point = high = the target's saving (unchanged),
low = the first step's, priced with the same ``rightsize_savings`` math at keep
fraction ``step / workers``. ``target_workers`` stays the data target (the chart draws
the peak there); ``step_workers`` is what to set now.
"""

from __future__ import annotations

import math

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import (
    cluster_kind_label,
    cluster_resource_metrics,
    cluster_resources_viz,
    cpu_area_evidence,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.phasing import (
    count_plan,
    max_step_ratio,
    observe_days,
    phase_sentence,
    phase_target_clause,
    phasing_evidence,
)
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import rightsize_savings

_WATCH = "memory, spill, I/O wait and job runtime"


@register
class RightsizeUnderutilizedRule(Rule):
    id = "rightsize_underutilized"
    # Savings model: stays ESTIMATED — the saving is sized from measured p95
    # CPU with headroom, but CPU is only a proxy for the binding resource (memory/
    # IO/spill may bind before CPU), so it is capped and never booked.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Rightsize underutilized clusters"
    resource_type = "cluster"
    # "Saved so far" (finops_rules.change_tracking): max workers moved toward the
    # target. ``max_workers`` is COALESCE(max_autoscale_workers, worker_count) in the
    # tracker's cluster query. Valued on evidence.phasing when present, else linear.
    tracked_setting = TrackedSetting(
        resource="cluster", column="max_workers",
        baseline_key="worker_count", proposed_key="target_workers",
        direction="decrease", uses_phasing=True, label="Max workers",
    )
    category = "compute"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "rightsize_underutilized.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. Sizing keys on the workload's PEAKS, and a
        # one-day sample may simply not contain them — shrinking a cluster on that
        # evidence risks a regression. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds
        avg_cpu = float(row.get("avg_cpu") or 0.0)
        avg_mem = float(row.get("avg_mem") or 0.0)
        worker_count = int(row.get("worker_count") or 0)

        # CPU is the primary signal: average below headroom => oversized on the whole.
        if avg_cpu >= t["cpu_headroom_pct"]:
            return None
        if worker_count < 2:
            return None  # nothing to shrink

        # Memory veto (one-way). A CPU-idle JVM cluster holds heap resident (40-70%
        # is normal), so we do NOT require low memory to downsize — we only DECLINE
        # when memory is high enough to be plausibly memory-bound (OOM/spill on a
        # smaller cluster). Fewer workers concentrate the same data onto fewer nodes,
        # so the PEAK (p95) memory is the real risk and is tested first; avg_mem is a
        # back-compat fallback. p95_mem/avg_mem below are compared on the same scale
        # as mem_bound_pct (percent): p95_mem is a 0-1 fraction, avg_mem is 0-100.
        mem_bound_pct = float(t.get("mem_bound_pct", t.get("mem_headroom_pct", 40)))
        p95_mem = row.get("p95_mem")  # 0-1 fraction from the SQL, or None
        if p95_mem is not None and float(p95_mem) * 100.0 >= mem_bound_pct:
            return None
        if avg_mem >= mem_bound_pct:
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None  # no billed cost attributed => nothing to save

        # Peak-based sizing. p95_cpu (0-1) is the observed peak the shrunk
        # cluster must still serve. keep_fraction is the share of current capacity to
        # retain so that peak sits near target_utilization with headroom to spare;
        # target_workers and the dollar saving are two views of the SAME fraction —
        # rightsize_savings applies this same formula (and the max_savings_fraction
        # cap) to the dollars, so the count and the money never drift. A bursty
        # cluster (high p95 despite low average) gives keep_fraction ~1 => ~0 saving,
        # so it correctly drops out at the target/savings gates below.
        p95_cpu = float(row.get("p95_cpu") or 0.0)
        target_util = float(t.get("target_utilization", 0.6))
        headroom = float(t.get("headroom", 0.15))
        max_frac = float(t.get("max_savings_fraction", 0.6))
        keep_fraction = (
            min(1.0, (p95_cpu * (1.0 + headroom)) / target_util)
            if target_util > 0 else 1.0
        )
        target_workers = max(1, math.ceil(worker_count * keep_fraction))
        if target_workers >= worker_count:
            return None  # the peak needs the current size; nothing to reclaim
        savings = rightsize_savings(
            p95_cpu=p95_cpu,
            monthly_cost=monthly_cost,
            target_utilization=target_util,
            headroom=headroom,
            max_savings_fraction=max_frac,
        )
        if savings < float(t.get("min_savings_usd", 25)):
            return None

        # Phased plan: halve per step by default (this rule's phasing_max_step_ratio).
        plan = count_plan(worker_count, target_workers, max_step_ratio=max_step_ratio(ctx))
        step_workers = plan[0]
        phased = len(plan) > 1
        if phased:
            step_savings = min(savings, rightsize_savings(
                p95_cpu=p95_cpu,
                monthly_cost=monthly_cost,
                max_savings_fraction=max_frac,
                keep_fraction=step_workers / worker_count,
            ))
        else:
            step_savings = savings  # one step straight to the target
        obs = observe_days(ctx)

        cluster_name = row.get("cluster_name") or str(row["cluster_id"])
        kind = cluster_kind_label(row.get("cluster_source"))
        memory_peak = (
            f"p95 memory was {float(p95_mem) * 100:.0f}%"
            if p95_mem is not None
            else f"average memory was {avg_mem:.0f}%"
        )
        why = (
            f"{kind} '{cluster_name}' averaged {avg_cpu:.1f}% CPU across "
            f"{worker_count} workers over the last {ctx.lookback_days} days, and even "
            f"its p95 CPU reached only {p95_cpu * 100:.0f}%. {memory_peak}, below the "
            f"{mem_bound_pct:.0f}% safety veto. "
            f"Keeping headroom above that measured peak, {worker_count} workers can "
            f"come down to about {target_workers}. It spends ~${monthly_cost:,.0f}/mo, "
            f"with ~${savings:,.0f}/mo estimated as recoverable. CPU is a sizing proxy, "
            f"so validate the change under representative load."
        )
        if phased:
            why += phase_target_clause(
                target=f"~{target_workers} workers", target_savings=savings)
            how = (
                f"Reduce max workers {worker_count} → {step_workers} now (Compute > "
                f"select the cluster > Edit); the data supports ~{target_workers}. Run a "
                f"representative workload on it."
                + phase_sentence(plan=plan, observe_days=obs, watch=_WATCH, verb="reduce it")
            )
        else:
            how = (
                f"Reduce max workers from {worker_count} toward {target_workers} "
                f"(Compute > select the cluster > Edit), then run a representative workload. "
                f"Restore capacity if memory, spill, I/O wait, or runtime regresses."
            )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=cluster_name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            # Phased: point = high = the full saving at the data target; low = the
            # first step's (collapsed to the point for a single-step plan).
            monthly_savings_low_usd=round(step_savings, 2),
            monthly_savings_high_usd=round(savings, 2),
            effort=EffortBand.MEDIUM,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=cpu_area_evidence(
                cpu_hourly=row.get("cpu_hourly"),
                p95_cpu=row.get("p95_cpu"),
                cpu_ts=row.get("cpu_ts"),
                idle_frac=row.get("idle_frac"),
                lookback_days=ctx.lookback_days,
                basis_extra=history_note(row, ctx),
                # Second chart: the CPU-headroom view answers "is it oversized?",
                # this one answers "is it safe to shrink?" — a cluster with high
                # memory, swap or I/O wait should not be downsized on CPU alone.
                viz_extra=[
                    cluster_resources_viz(
                        row=row, lookback_days=ctx.lookback_days, cluster_kind=kind
                    )
                ],
                cluster_kind=kind,
                node_type=row.get("node_type_id"),
                **cluster_resource_metrics(row),
                **history_evidence(row, ctx),
                avg_cpu_pct=round(avg_cpu, 1),
                avg_mem_pct=round(avg_mem, 1),
                p95_mem_pct=(
                    round(float(row["p95_mem"]) * 100, 1)
                    if row.get("p95_mem") is not None else None
                ),
                worker_count=worker_count,
                target_workers=target_workers,
                step_workers=step_workers,
                cost_30d_usd=row.get("cost_30d_usd"),
                phasing=phasing_evidence(
                    "Workers", worker_count, plan, observe_days=obs, watch=_WATCH,
                    step_savings=round(step_savings, 2), target_savings=round(savings, 2),
                ),
                **({"savings_range_basis": (
                    f"Low = the first step to {step_workers} workers (~${step_savings:,.0f}/mo "
                    f"on its own); high = ~{target_workers} workers, what the measured peak "
                    f"supports (~${savings:,.0f}/mo). Reach it halving the workers per step."
                )} if phased else {}),
            ),
        )
