"""Rule: rightsize over-provisioned job compute.

Job clusters are ephemeral (one per run), so this rule aggregates utilization by
the recurring JOB across all its run-clusters. Using the ``driver`` flag on
``system.compute.node_timeline`` it separates driver from worker utilization and
classifies the dominant over-provisioning symptom:

* **driver_bound** — workers sit idle while the driver does the work. The job is a
  single-node/pandas workload in disguise; dropping the workers frees their whole
  share of the cost.
* **idle_workers** — too many workers, all lightly used; roughly halving them frees
  about half the worker share.
* **memory_oversize** — far more memory provisioned than the workload touches; a
  smaller-memory node class frees a slice of the node cost.

Savings model: the classification keyed on AVERAGE utilization alone, which
hides a burst the compute exists to serve — a job idle on average but spiking at
its p95 needs its workers/memory at the peak, and stripping them would starve that
peak. Each symptom now carries a p95 PEAK VETO: a high worker p95 CPU blocks the
worker-cut symptoms (``driver_bound`` / ``idle_workers``) and a high p95 memory
blocks ``memory_oversize``, so bursty jobs correctly drop out instead of being
sized on their quiet average.

Savings stay deliberately conservative (a haircut on top of the structural
recoverable base — see :func:`finops_rules.savings.jobs_compute_rightsize_savings`)
because CPU/memory are proxies for the binding resource; the peak veto removes the
class of false positive the average could not see, but does not chase more dollars.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import (
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_driver_worker_split,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import jobs_compute_rightsize_savings

_BASIS = (
    "system.compute.node_timeline (per-node CPU/memory, driver vs worker) for the "
    "job's ephemeral run-clusters, mapped to the job via billing.usage.usage_metadata "
    "and priced x list_prices, {n}-day lookback. Utilization is measured at both the "
    "AVERAGE and the p95 PEAK: the average flags the symptom, the peak vetoes it when "
    "a burst still needs the compute, so a job idle on average but spiking at p95 is "
    "not downsized. The rightsize target and its saving stay conservative (a haircut "
    "on the worker/node share), since CPU/memory are proxies for the binding resource."
)


@register
class JobsComputeRightsizeRule(Rule):
    id = "jobs_compute_rightsize"
    # Savings model: stays ESTIMATED — CPU/memory are proxies for the binding
    # resource and the recoverable target is inferred, not booked. The p95 peak veto
    # (below) removes the bursty-job false positive the average alone produced; it
    # tightens honesty, it does not promote the rule to bookable.
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Rightsize over-provisioned job compute"
    resource_type = "job"
    category = "compute"
    cloud_savings = "proportional"  # removes VM hours/nodes in step with DBUs
    sql_file = "jobs_compute_rightsize.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Sizing keys on the workload's peaks; a one-off run on a huge cluster is
        # not evidence the job is oversized. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds

        has_workers = bool(int(row.get("has_workers") or 0))
        worker_count = int(row.get("worker_count") or 0)
        driver_cpu = float(row.get("driver_cpu") or 0.0)
        # worker_* are NULL for a job that ran single-node; keep the raw value so we
        # can tell "no workers" apart from "workers idle".
        worker_cpu_raw = row.get("worker_cpu")
        worker_cpu = float(worker_cpu_raw) if worker_cpu_raw is not None else None
        worker_mem_raw = row.get("worker_mem")
        worker_mem = float(worker_mem_raw) if worker_mem_raw is not None else None
        driver_mem = float(row.get("driver_mem") or 0.0)

        # p95 PEAKS, same 0-100 scale as the averages, NULL when a side had
        # no samples (e.g. a single-node job has no workers). The average tells us the
        # symptom; the peak vetoes it when a burst still needs the compute.
        worker_p95_cpu_raw = row.get("worker_p95_cpu")
        worker_p95_cpu = float(worker_p95_cpu_raw) if worker_p95_cpu_raw is not None else None
        worker_p95_mem_raw = row.get("worker_p95_mem")
        worker_p95_mem = float(worker_p95_mem_raw) if worker_p95_mem_raw is not None else None
        driver_p95_mem = float(row.get("driver_p95_mem") or 0.0)

        worker_idle = float(t["worker_idle_cpu_pct"])
        driver_active = float(t["driver_active_cpu_pct"])
        cpu_headroom = float(t["cpu_headroom_pct"])
        cpu_peak = float(t.get("cpu_peak_pct", 60))
        mem_oversize = float(t["mem_oversize_pct"])
        mem_bound = float(t["mem_bound_pct"])

        # Worker share of the cluster cost, from node counts x per-node cores. Falls
        # back to node-count (homogeneous nodes) when core specs are unavailable.
        worker_cost_fraction = _worker_cost_fraction(row, worker_count)

        # CPU used for the CPU-headroom checks: worker CPU when there are workers,
        # else the driver's (a single-node job's only node).
        primary_cpu = worker_cpu if has_workers and worker_cpu is not None else driver_cpu
        # Memory used for the oversize check: likewise worker- or driver-side.
        primary_mem = worker_mem if has_workers and worker_mem is not None else driver_mem
        # p95 memory for the oversize veto: the peak the smaller node would have to hold.
        primary_p95_mem = (
            worker_p95_mem if has_workers and worker_p95_mem is not None else driver_p95_mem
        )

        # Peak vetoes. The worker-cut symptoms free WORKER capacity, so a
        # worker p95 CPU that reaches cpu_peak means the workers are doing real work at
        # the burst — stripping them would starve it, so we don't classify. memory_oversize
        # moves to a smaller-memory node, so a p95 memory at/above mem_bound means the
        # peak genuinely needs that memory and the smaller node would OOM/spill.
        # A missing peak (None) can't veto — an older snapshot must not regress.
        workers_needed_at_peak = worker_p95_cpu is not None and worker_p95_cpu >= cpu_peak
        memory_needed_at_peak = primary_p95_mem >= mem_bound

        # Classify the dominant symptom, most specific first.
        symptom = None
        if (
            has_workers
            and worker_count >= 1
            and worker_cpu is not None
            and worker_cpu < worker_idle
            and driver_cpu >= driver_active
            and not workers_needed_at_peak
        ):
            symptom = "driver_bound"
        elif (
            worker_count >= 2
            and worker_cpu is not None
            and worker_cpu < cpu_headroom
            and (worker_mem is None or worker_mem < mem_bound)
            and not workers_needed_at_peak
        ):
            symptom = "idle_workers"
        elif (
            primary_mem < mem_oversize
            and primary_cpu < cpu_headroom
            and not memory_needed_at_peak
        ):
            symptom = "memory_oversize"
        else:
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        savings = jobs_compute_rightsize_savings(
            symptom=symptom,
            monthly_cost=monthly_cost,
            worker_cost_fraction=worker_cost_fraction,
            savings_fraction=float(t["savings_fraction"]),
        )
        if savings <= 0:
            return None

        name = row.get("job_name") or str(row["job_id"])
        lookback = ctx.lookback_days
        driver_nt = row.get("driver_node_type")
        worker_nt = row.get("worker_node_type")
        if symptom == "driver_bound":
            why = (
                f"Job '{name}' ran on {worker_count} worker(s) that averaged "
                f"{worker_cpu:.1f}% CPU over {lookback} days while its driver averaged "
                f"{driver_cpu:.1f}% — the work is happening on the driver and the "
                f"workers are idle. It spends ~${monthly_cost:,.0f}/mo; going "
                f"single-node reclaims ~${savings:,.0f}/mo of idle worker compute."
            )
            how = (
                "Switch this job to single-node (0 workers) — set the job cluster to a "
                "single-node cluster, or if it is a pandas/single-machine workload run "
                "it on a bigger driver with no workers. Re-test once to confirm it "
                "still fits (Workflows > the job > the task's cluster > Edit)."
            )
        elif symptom == "idle_workers":
            target = max(1, worker_count // 2)
            why = (
                f"Job '{name}' ran on {worker_count} workers that averaged "
                f"{worker_cpu:.1f}% CPU over {lookback} days — well below headroom, so "
                f"the cluster is oversized. It spends ~${monthly_cost:,.0f}/mo; cutting "
                f"workers reclaims ~${savings:,.0f}/mo."
            )
            how = (
                f"Reduce the job cluster's worker count (e.g. from {worker_count} "
                f"toward {target}) or lower the autoscale max, then re-test the run "
                f"(Workflows > the job > the task's cluster > Edit)."
            )
        else:  # memory_oversize
            node_desc = worker_nt or driver_nt or "the node type"
            why = (
                f"Job '{name}' averaged only {primary_mem:.1f}% memory (and "
                f"{primary_cpu:.1f}% CPU) over {lookback} days on {node_desc} — far "
                f"below what is provisioned, so it is on a larger / more "
                f"memory-optimized node than it needs. It spends "
                f"~${monthly_cost:,.0f}/mo; a smaller-memory node class reclaims "
                f"~${savings:,.0f}/mo."
            )
            how = (
                "Move the job cluster to a smaller or less memory-optimized node type "
                "(e.g. a general-purpose instead of a memory-optimized family, or one "
                "size down), then re-test (Workflows > the job > the task's cluster > "
                "Edit)."
            )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            effort=EffortBand.MEDIUM,  # a config change + a re-test, not a toggle
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,  # measured utilization, inferred target
                data_basis=_BASIS.format(n=lookback) + history_note(row, ctx),
                # Driver-vs-worker CPU/memory bars: the picture behind the symptom.
                viz=viz_driver_worker_split(
                    driver_cpu=driver_cpu,
                    worker_cpu=worker_cpu,
                    driver_mem=driver_mem,
                    worker_mem=worker_mem,
                    worker_count=worker_count,
                    symptom=symptom,
                ),
                **history_evidence(row, ctx),
                symptom=symptom,
                worker_count=worker_count,
                has_workers=has_workers,
                driver_cpu_pct=round(driver_cpu, 1),
                worker_cpu_pct=(round(worker_cpu, 1) if worker_cpu is not None else None),
                driver_mem_pct=round(driver_mem, 1),
                worker_mem_pct=(round(worker_mem, 1) if worker_mem is not None else None),
                # p95 peaks behind the veto: what the average could not see.
                worker_p95_cpu_pct=(
                    round(worker_p95_cpu, 1) if worker_p95_cpu is not None else None
                ),
                worker_p95_mem_pct=(
                    round(worker_p95_mem, 1) if worker_p95_mem is not None else None
                ),
                driver_p95_mem_pct=round(driver_p95_mem, 1),
                driver_node_type=driver_nt,
                worker_node_type=worker_nt,
                driver_cores=row.get("driver_cores"),
                worker_cores=row.get("worker_cores"),
                worker_memory_mb=row.get("worker_memory_mb"),
                worker_cost_fraction=round(worker_cost_fraction, 3),
                run_clusters=int(row.get("run_clusters") or 0),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )


def _worker_cost_fraction(row: dict, worker_count: int) -> float:
    """Share of a job cluster's cost attributable to WORKER nodes (0..1).

    Prefers per-node cores (workers x worker_cores over the whole fleet of cores,
    driver included); falls back to a node-count ratio assuming homogeneous nodes
    when core specs are missing. 0 when there are no workers."""
    if worker_count <= 0:
        return 0.0
    driver_cores = float(row.get("driver_cores") or 0.0)
    worker_cores = float(row.get("worker_cores") or 0.0)
    worker_cores_total = worker_count * worker_cores
    if worker_cores_total > 0 and driver_cores > 0:
        return worker_cores_total / (worker_cores_total + driver_cores)
    # Homogeneous-node fallback: driver is one node among worker_count + 1.
    return worker_count / (worker_count + 1)
