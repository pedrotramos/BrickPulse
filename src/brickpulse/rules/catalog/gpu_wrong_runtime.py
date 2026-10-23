"""Rule: GPU instance provisioned but the runtime cannot use the GPU.

A cluster on a GPU node type (``node_types.gpu_count > 0``) only benefits from the
GPU when it runs a GPU-enabled ML runtime (a ``-gpu-ml`` Databricks Runtime). On
any other runtime — a standard runtime, a CPU-ML runtime, or Photon — the GPU
hardware is billed but physically unusable, so the workload pays the (large) GPU
premium for nothing and belongs on a comparable CPU instance.

WHY THIS IS ADVISORY (``monthly_savings_usd = 0``), not a booked saving:
    ``system.billing.usage`` prices only the Databricks DBU fee, and GPU vs CPU
    compute bill under the SAME sku (``ENTERPRISE_ALL_PURPOSE_COMPUTE``) — there is
    no GPU DBU line to isolate, and the DOMINANT GPU cost, the cloud VM/GPU instance
    price, is on the customer's cloud bill and NOT in system tables at all. So the
    true saving (the GPU DBU premium PLUS the invisible, usually larger, cloud-VM
    premium) cannot be priced from system tables. Booking a number we cannot
    substantiate would break the engine invariant that an estimate never exceeds
    true avoidable spend. The finding therefore ranks on ``monthly_waste_usd`` — the
    billed DBU spend on the misconfigured GPU cluster, a FLOOR on what is at risk —
    and says plainly that the real saving is larger. This is the same advisory shape
    as ``cluster_job_contention``.

DETECTION CONFIDENCE IS HIGH: it is pure configuration, not a utilization proxy
(``node_timeline`` exposes no GPU utilization at all, and CPU% is a trap on GPU
boxes since GPU-bound work shows near-zero CPU). The one false positive is a cluster
that reaches the GPU through a path the runtime string does not show — a custom GPU
container (Databricks Container Services) or a GPU library installed via init script
— so the guidance says to CONFIRM the GPU is genuinely unused before switching.
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
    "system.compute.clusters (current config) x system.compute.node_types "
    "(gpu_count) x billing.usage x list_prices, {n}-day lookback. Detection is pure "
    "configuration: a GPU node type running a non-'-gpu-ml' runtime cannot use the "
    "GPU (node_timeline exposes no GPU utilization, so idle-GPU is not measurable — "
    "this catches the GPU being unusable by construction). Cost is the billed "
    "Databricks DBU spend on the cluster; GPU and CPU compute share one SKU and the "
    "cloud VM/GPU instance price is not in system tables, so this DBU figure is a "
    "FLOOR — the real saving from moving to CPU (DBU premium plus the larger, "
    "unmeasured cloud-VM premium) is higher."
)


@register
class GpuWrongRuntimeRule(Rule):
    id = "gpu_wrong_runtime"
    # Advisory: the dollars cannot be priced from system tables (see module
    # docstring — GPU shares the CPU compute SKU and the cloud-VM premium is
    # invisible), so nothing is booked; the finding ranks on the at-risk DBU spend.
    default_savings_status = "advisory"
    # Net-new opportunity rule, still validating: relocated to the Labs / Experimental
    # tab (out of the headline, NBA and main surfaces) until proven, same as the other
    # Labs rules. Advisory economics, so it can never book savings anyway.
    experimental = True
    version = "1.0.0"
    title = "Move a GPU cluster with no GPU runtime to CPU"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "gpu_wrong_runtime.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge the cluster's habitual configuration.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # at-risk spend figure we rank on.
        if underpriced(row):
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(ctx.thresholds.get("min_monthly_cost_usd", 25)):
            return None  # too little visible spend to be worth an admin's attention

        worker_gpu = int(row.get("worker_gpu_count") or 0)
        driver_gpu = int(row.get("driver_gpu_count") or 0)
        if worker_gpu <= 0 and driver_gpu <= 0:
            return None  # SQL already filters this, but never trust the row blindly

        name = row.get("cluster_name") or str(row["cluster_id"])
        dbr = row.get("dbr_version") or "unknown"
        worker_nt = row.get("worker_node_type")
        driver_nt = row.get("driver_node_type")

        # Name which node(s) carry the wasted GPU, for honest copy.
        if worker_gpu > 0 and driver_gpu > 0:
            where = (
                f"both its driver ({driver_nt}) and worker ({worker_nt}) nodes are "
                f"GPU instances"
            )
        elif worker_gpu > 0:
            where = f"its worker nodes ({worker_nt}) are GPU instances"
        else:
            where = f"its driver node ({driver_nt}) is a GPU instance"

        why = (
            f"All-purpose cluster '{name}' runs on GPU hardware — {where} — but its "
            f"runtime is '{dbr}', which is not a GPU ML runtime (no '-gpu-ml'). "
            f"Databricks only drives the GPU on a '-gpu-ml' runtime, so the GPU is "
            f"billed but cannot be used by this workload. It billed about "
            f"${monthly_cost:,.0f}/mo in Databricks DBUs, and the cloud VM/GPU "
            f"instance premium on top (not visible in system tables) is typically the "
            f"larger cost — all of it paying for a GPU that does nothing."
        )
        how = (
            f"First CONFIRM the GPU is genuinely unused: check that the cluster has no "
            f"custom GPU container image and no init script installing CUDA/GPU "
            f"libraries (Compute > {name} > Edit > Advanced). If nothing uses the GPU, "
            f"switch the driver/worker node type to a comparable CPU instance (similar "
            f"cores/memory). If the workload IS meant to use the GPU, switch the "
            f"runtime to the matching '-gpu-ml' Databricks Runtime instead."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="cluster",
            resource_id=str(row["cluster_id"]),
            resource_name=name,
            # Ranks on the visible DBU spend at risk; the true GPU premium is larger.
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=0.0,  # advisory: cloud-VM premium unpriceable (docstring)
            # A node-type change needs a restart and a quick check that the GPU is
            # genuinely unused, so it is not a trivial toggle.
            effort=EffortBand.MEDIUM,
            priority=RulePriority.HIGH,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                dbr_version=dbr,
                worker_node_type=worker_nt,
                driver_node_type=driver_nt,
                worker_gpu_count=worker_gpu,
                driver_gpu_count=driver_gpu,
                worker_cores=row.get("worker_cores"),
                worker_memory_mb=row.get("worker_memory_mb"),
                driver_cores=row.get("driver_cores"),
                driver_memory_mb=row.get("driver_memory_mb"),
                worker_count=row.get("worker_count"),
                dbus_30d=row.get("dbus_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )
