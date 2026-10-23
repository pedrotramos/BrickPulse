"""Rule: all-purpose clusters with the wrong SHAPE (driver-bound or wrong family).

EXPERIMENTAL (Labs tab).
``jobs_compute_rightsize`` already classifies ``driver_bound`` / ``memory_oversize``,
but only for JOB clusters; ``rightsize_underutilized`` (all-purpose) is CPU-only and
changes the worker COUNT. This rule asks the orthogonal question for interactive
clusters: is the cluster the wrong *shape*?

* **driver_bound** — while the cluster is doing anything, the driver works and every
  worker idles (pandas / single-machine code on a multi-node cluster). Single-node
  frees the worker share of the cost for the driver-only share of active time.
* **memory_family** — the workers are a memory-dense family (>= ``memory_family_gb_per_core``
  GB per core) whose p95 memory stays far below what they carry. A general-purpose
  type with the SAME cores keeps the CPU and holds the measured p95 memory; the saving
  is the OBSERVED DBU/node-hour delta between the two types on the worker share. A
  target never billed in this account has no measured rate, so it is suggested
  without a dollar figure.
* **compute_family_pressure** — compute-optimized workers (<= ``compute_family_gb_per_core``)
  swapping or pinned at high memory. A general-purpose family fixes the spill; that
  is a reliability/performance change and usually costs MORE per node, so $0.

Activity-aware: interactive clusters idle most of the time, so averaging over every
running minute would read every cluster as "workers idle". The SQL splits ACTIVE
minutes (driver or any worker doing real work) from idle ones, and the driver-bound
test only looks at active minutes. Idle time itself is auto-termination's question.

**Advisory, $0 booked** (experimental ⇒ advisory, like every Phase-4 Labs rule). The
dollar figure is ADDRESSABLE spend: the shape change needs a re-test of the users'
workloads, and the same cluster may carry a right-size / auto-termination option.
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
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    cluster_kind_label,
    evidence_block,
    viz_driver_worker_split,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.compute.node_timeline (per-node-minute CPU / memory / swap, driver vs "
    "worker via the `driver` flag; ACTIVE minutes = driver or any worker at >= "
    "{active:.0f}% CPU) + system.compute.node_types (cores, memory) + "
    "system.billing.usage x list_prices for the cluster's corrected cost, {n}-day "
    "lookback. Node-type DBU rates are OBSERVED, not from a rate card: DBUs billed per "
    "node-hour on homogeneous clusters over full, constant-node hours across the "
    "account, kept only when stable. Experimental (Labs) — validate before trusting."
)

SYMPTOM_DRIVER_BOUND = "driver_bound"
SYMPTOM_MEMORY_FAMILY = "memory_family"
SYMPTOM_COMPUTE_PRESSURE = "compute_family_pressure"


def _f(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gb_per_core(memory_mb: float | None, cores: float | None) -> float | None:
    if not memory_mb or not cores:
        return None
    return memory_mb / 1024.0 / cores


def worker_cost_fraction(row: dict) -> float:
    """Share of the cluster's cost on WORKER nodes (0..1): worker node-minutes x
    worker cores over the whole cluster's core-minutes. Falls back to node-minutes
    alone (homogeneous cores) when a core spec is missing."""
    wmin = _f(row, "worker_node_minutes") or 0.0
    dmin = _f(row, "driver_node_minutes") or 0.0
    if wmin <= 0:
        return 0.0
    wc = _f(row, "worker_cores") or 0.0
    dc = _f(row, "driver_cores") or 0.0
    if wc > 0 and dc > 0:
        return (wmin * wc) / (wmin * wc + dmin * dc)
    return wmin / (wmin + dmin) if (wmin + dmin) > 0 else 0.0


def _rate_pair(row: dict, alt: dict) -> tuple[float, float] | None:
    """(current, target) DBU/node-hour at the SAME photon setting. Prefers the
    cluster's own setting; the ratio is photon-invariant, so the other setting is a
    valid fallback when both types were measured there. None when unmeasured."""
    photon = bool(int(row.get("photon") or 0))
    order = ("photon", "std") if photon else ("std", "photon")
    for key in order:
        cur = _f(row, f"current_rate_{key}")
        tgt = _f(alt, f"rate_{key}")
        if cur and tgt and cur > 0 and tgt > 0:
            return cur, tgt
    return None


def choose_target(
    row: dict, needed_mb: float, min_gb_per_core: float = 3.5
) -> tuple[dict | None, tuple[float, float] | None]:
    """Pick the worker type to move to among the SQL's same-core, leaner, non-GPU
    alternatives that still hold ``needed_mb`` and are GENERAL-PURPOSE density (at
    least ``min_gb_per_core`` — a compute-optimized family would trade a memory
    oversize for a memory squeeze). Prefers the cheapest MEASURED rate that is
    actually cheaper than the current type; with no measured candidate, the roomiest
    fitting type (safest) is suggested without a rate."""
    cores = _f(row, "worker_cores") or 0.0
    fitting = [
        a for a in (row.get("alt_node_types") or [])
        if a
        and (_f(a, "memory_mb") or 0.0) >= needed_mb
        and (cores <= 0 or (_gb_per_core(_f(a, "memory_mb"), cores) or 0.0) >= min_gb_per_core)
    ]
    if not fitting:
        return None, None
    priced = []
    for a in fitting:
        pair = _rate_pair(row, a)
        if pair and pair[1] < pair[0]:
            priced.append((pair[1] / pair[0], -(_f(a, "memory_mb") or 0.0), a, pair))
    if priced:
        priced.sort(key=lambda x: (x[0], x[1], str(x[2].get("node_type"))))
        _, _, alt, pair = priced[0]
        return alt, pair
    fitting.sort(key=lambda a: (-(_f(a, "memory_mb") or 0.0), str(a.get("node_type"))))
    return fitting[0], None


@register
class AllPurposeShapeMismatchRule(Rule):
    id = "allpurpose_shape_mismatch"
    experimental = True
    # Advisory: the shape signal is measured, but the change needs a workload re-test
    # and the same cluster may also carry right-size / auto-termination options.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "All-purpose cluster has the wrong shape"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    # cloud_savings stays "none" while advisory; "proportional" applies at promotion.
    sql_file = "allpurpose_shape_mismatch.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        # GPU node types belong to gpu_wrong_runtime; their premium is on the VM bill.
        if int(row.get("worker_gpu_count") or 0) > 0:
            return None

        symptom = self._classify(row, t)
        if symptom is None:
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        wfrac = worker_cost_fraction(row)
        name = row.get("cluster_name") or str(row["cluster_id"])
        worker_nt = row.get("worker_node_type") or "the worker type"
        kind = cluster_kind_label(row.get("cluster_source"))
        avg_workers = _f(row, "avg_workers") or 0.0
        lookback = ctx.lookback_days
        extra: dict = {}
        viz = None
        confidence = CONFIDENCE_MEDIUM

        if symptom == SYMPTOM_DRIVER_BOUND:
            active = _f(row, "active_minutes") or 0.0
            share = (_f(row, "driver_only_minutes") or 0.0) / active if active else 0.0
            share = max(0.0, min(1.0, share))
            addressable = monthly_cost * wfrac * share
            if addressable < float(t.get("min_addressable_usd", 25)):
                return None
            dcpu = _f(row, "active_driver_cpu") or 0.0
            wcpu = _f(row, "active_worker_cpu") or 0.0
            why = (
                f"{kind} '{name}' ran ~{avg_workers:.1f} worker(s) on average, but in "
                f"{share * 100:.0f}% of the {active / 60:,.0f} hours it was actually busy "
                f"over the last {lookback} days only the driver worked (driver ~{dcpu:.0f}% "
                f"CPU, workers ~{wcpu:.0f}%). That is single-machine code — pandas, plain "
                f"Python, a driver-side loop — on a multi-node cluster. The workers are "
                f"~{wfrac * 100:.0f}% of its ~${monthly_cost:,.0f}/mo; going single-node drops "
                f"that worker cost, counted here only for the {share * 100:.0f}% of busy "
                f"time the workers weren't needed: ~${addressable:,.0f}/mo."
            )
            how = (
                "Switch this cluster to single-node (Compute > the cluster > Edit > "
                "Single node), sizing the driver up if it needs more memory. If some users "
                "genuinely run distributed Spark here, split them: a small single-node "
                "cluster for the notebook work and the multi-node one for the Spark jobs. "
                "Ask the users first — idle time between sessions is auto-termination's "
                "question, not this one."
            )
            viz = viz_driver_worker_split(
                driver_cpu=dcpu,
                worker_cpu=wcpu,
                driver_mem=_f(row, "driver_mem") or 0.0,
                worker_mem=_f(row, "worker_mem"),
                worker_count=max(1, round(avg_workers)),
                symptom="driver_bound",
            )
            extra.update(driver_only_share=round(share, 4))
        elif symptom == SYMPTOM_MEMORY_FAMILY:
            p95_mem = _f(row, "worker_p95_mem") or 0.0
            cur_mb = _f(row, "worker_memory_mb") or 0.0
            fill = float(t.get("target_fill_pct", 70)) / 100.0
            needed_mb = cur_mb * p95_mem / 100.0 / fill if fill > 0 else cur_mb
            target, pair = choose_target(
                row, needed_mb, float(t.get("general_min_gb_per_core", 3.5)))
            if target is None:
                return None
            target_nt = target.get("node_type")
            target_gb = (_f(target, "memory_mb") or 0.0) / 1024.0
            if pair:
                cur_rate, tgt_rate = pair
                addressable = monthly_cost * wfrac * (1.0 - tgt_rate / cur_rate)
                if addressable < float(t.get("min_addressable_usd", 25)):
                    return None
                price_clause = (
                    f"This account bills {worker_nt} at ~{cur_rate:.2f} DBU/node-hour and "
                    f"{target_nt} at ~{tgt_rate:.2f} (measured from your own billing), so "
                    f"moving the workers (~{wfrac * 100:.0f}% of its ~${monthly_cost:,.0f}/mo) "
                    f"frees ~${addressable:,.0f}/mo."
                )
                extra.update(current_dbu_per_node_hour=round(cur_rate, 3),
                             target_dbu_per_node_hour=round(tgt_rate, 3))
            else:
                addressable = 0.0
                confidence = CONFIDENCE_LOW
                price_clause = (
                    f"{target_nt} has never been billed in this account, so there is no "
                    f"measured DBU rate to price the change — no dollar figure is shown."
                )
            cur_gb = cur_mb / 1024.0
            why = (
                f"{kind} '{name}' runs its workers on {worker_nt} ({cur_gb:.0f} GB, "
                f"~{cur_gb / (_f(row, 'worker_cores') or 1):.1f} GB per core — a memory-dense "
                f"type), but their memory peaked at only "
                f"{p95_mem:.0f}% (p95) over the last {lookback} days. {target_nt} has the "
                f"same cores and {target_gb:.0f} GB, which still holds that peak with "
                f"headroom. {price_clause}"
            )
            how = (
                f"Change the worker type to {target_nt} (Compute > the cluster > Edit > "
                f"Worker type), keep the driver as is, and re-run a representative "
                f"workload. If the cluster relies on the local-SSD disk cache, pick a "
                f"variant with local NVMe storage."
            )
            viz = viz_driver_worker_split(
                driver_cpu=_f(row, "driver_cpu") or 0.0,
                worker_cpu=_f(row, "worker_cpu"),
                driver_mem=_f(row, "driver_mem") or 0.0,
                worker_mem=_f(row, "worker_mem"),
                worker_count=max(1, round(avg_workers)),
                symptom="memory_oversize",
            )
            extra.update(target_node_type=target_nt,
                         target_memory_mb=_f(target, "memory_mb"),
                         target_rate_measured=pair is not None)
        else:  # SYMPTOM_COMPUTE_PRESSURE
            addressable = 0.0
            p95_mem = _f(row, "worker_p95_mem") or 0.0
            p95_swap = _f(row, "worker_p95_swap") or 0.0
            why = (
                f"{kind} '{name}' runs its workers on {worker_nt}, a compute-optimized "
                f"(low memory per core) type, and they are short of memory: p95 memory "
                f"{p95_mem:.0f}% and p95 swap {p95_swap:.1f}% over the last {lookback} "
                f"days. Swapping executors run slower and spill, so the cluster bills "
                f"longer than it needs to. The fix is a reliability change, so no dollar "
                f"figure is counted."
            )
            how = (
                "Move the workers to a general-purpose family with the same cores (about "
                "4 GB per core), or fewer larger nodes, then compare run times. A "
                "general-purpose node costs a little more per hour but usually finishes "
                "memory-heavy work sooner."
            )
            confidence = CONFIDENCE_LOW

        if addressable >= 500:
            priority = RulePriority.HIGH
        elif addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,  # a config change + a workload re-test
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=confidence,
                data_basis=_BASIS.format(
                    n=lookback, active=float(t.get("active_cpu_pct", 20)))
                + history_note(row, ctx)
                + coverage_note(row),
                **({"viz": viz} if viz else {}),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                symptom=symptom,
                cluster_source=row.get("cluster_source"),
                driver_node_type=row.get("driver_node_type"),
                worker_node_type=row.get("worker_node_type"),
                worker_cores=row.get("worker_cores"),
                worker_memory_mb=row.get("worker_memory_mb"),
                avg_workers=round(avg_workers, 2),
                running_minutes=row.get("running_minutes"),
                active_minutes=row.get("active_minutes"),
                driver_only_minutes=row.get("driver_only_minutes"),
                active_worker_p95_cpu_pct=row.get("active_worker_p95_cpu"),
                worker_p95_mem_pct=row.get("worker_p95_mem"),
                worker_p95_swap_pct=row.get("worker_p95_swap"),
                worker_cost_fraction=round(wfrac, 3),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
                **extra,
            ),
        )

    @staticmethod
    def _classify(row: dict, t: dict) -> str | None:
        """Most specific symptom first; None when the shape looks right."""
        active = _f(row, "active_minutes") or 0.0
        driver_only = _f(row, "driver_only_minutes") or 0.0
        avg_workers = _f(row, "avg_workers") or 0.0
        worker_p95 = _f(row, "active_worker_p95_cpu")
        # Peak veto: workers that do real work at their p95 are needed at the burst.
        # A missing peak can't veto (an older snapshot must not regress).
        workers_needed = worker_p95 is not None and worker_p95 >= float(t.get("cpu_peak_pct", 60))
        if (
            avg_workers >= 0.5
            and active >= float(t.get("min_active_minutes", 120))
            and active > 0
            and driver_only / active >= float(t.get("driver_only_share", 0.6))
            and not workers_needed
        ):
            return SYMPTOM_DRIVER_BOUND

        density = _gb_per_core(_f(row, "worker_memory_mb"), _f(row, "worker_cores"))
        if density is None:
            return None
        p95_mem = _f(row, "worker_p95_mem")
        if p95_mem is None:
            return None
        if (
            density >= float(t.get("memory_family_gb_per_core", 7))
            and p95_mem < float(t.get("mem_oversize_p95_pct", 35))
        ):
            return SYMPTOM_MEMORY_FAMILY
        p95_swap = _f(row, "worker_p95_swap") or 0.0
        if density <= float(t.get("compute_family_gb_per_core", 2.5)) and (
            p95_swap >= float(t.get("swap_p95_pct", 5))
            or p95_mem >= float(t.get("mem_pressure_p95_pct", 90))
        ):
            return SYMPTOM_COMPUTE_PRESSURE
        return None
