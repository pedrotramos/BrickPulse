"""Rule: move eligible classic/pro SQL warehouses to Serverless.

Classic and pro SQL warehouses keep a cluster warm across idle gaps; Serverless
scales to zero between bursts. The saving is the net of two levers, both priced
against the warehouse's *measured* usage (see
``finops_rules.savings.serverless_migration_savings``):

* removing the all-in idle warm-pool cost (the DBU bill we measure PLUS the
  cloud VM that sits warm alongside it, which serverless folds into its rate), and
* the higher serverless per-DBU rate charged on the active work that still runs.

Because the serverless list rate is all-in but classic/pro DBU rates exclude the
cloud VM (billed separately, not in system.billing.usage), we ESTIMATE that VM
cost to compare like-for-like and surface it as an estimate. A warehouse whose
active spend dominates can net out to no saving, and is suppressed.
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
    CONFIDENCE_MEDIUM,
    calendar_grid,
    evidence_block,
    viz_hour_heatmap,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import (
    CLASSIC_SQL_DBU_RATE,
    EST_VM_COST_PER_DBU,
    PRO_SQL_DBU_RATE,
    SERVERLESS_SQL_DBU_RATE,
    serverless_migration_savings,
    summarize_hourly_states,
)

_BASIS = (
    "system.query.history (waiting_for_compute_duration_ms) + billing.usage, {n}-day "
    "lookback. Saving = removed all-in idle cost minus the higher serverless rate on "
    "active hours. Classic/pro cloud-VM cost is not in billing.usage, so it is "
    "ESTIMATED at ~${vm:.2f}/DBU (list-price based) — treat the figure as directional "
    "and confirm workload compatibility."
)
_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

#: Default minimum idle share before we surface a migration. The net saving already
#: reflects the economics, but the VM cost is an estimate, so we only surface cases
#: with enough idle headroom that the recommendation is robust to that estimate.
#: Overridable via ``min_idle_fraction`` in config/thresholds.yaml (0 disables).
#: PRO is gated lower: its active-hours premium is often negative (serverless is
#: cheaper per active hour too), so a low-idle pro migration is still net-positive
#: and a 30% gate would hide real savings.
_DEFAULT_MIN_IDLE_FRACTION = 0.30       # classic
_DEFAULT_MIN_IDLE_FRACTION_PRO = 0.15   # pro


def _is_pro(warehouse_type: str | None) -> bool:
    return (warehouse_type or "").strip().upper() == "PRO"


def _tier_dbu_rate(warehouse_type: str | None, t: dict) -> float:
    """List $/DBU for the warehouse's tier. Pro and classic have very different
    rates (pro ~2.5x classic), so the serverless premium differs sharply between
    them; we must not treat them alike. Unknown type falls back to the classic
    rate — the conservative choice, since its larger serverless premium yields the
    smaller (never overstated) saving."""
    if _is_pro(warehouse_type):
        return float(t.get("pro_dbu_rate", PRO_SQL_DBU_RATE))
    return float(t.get("classic_dbu_rate", CLASSIC_SQL_DBU_RATE))


def _min_idle_fraction(warehouse_type: str | None, t: dict) -> float:
    """Idle-share gate for this tier. Pro is gated lower (see the module constants)."""
    if _is_pro(warehouse_type):
        return float(t.get("min_idle_fraction_pro", _DEFAULT_MIN_IDLE_FRACTION_PRO))
    return float(t.get("min_idle_fraction", _DEFAULT_MIN_IDLE_FRACTION))


def _est_vm_cost(cloud: str | None, t: dict) -> float:
    """Estimated classic/pro cloud-VM cost ($/DBU), by cloud when known.

    The VM the classic/pro warehouse runs on is billed by the cloud provider and is
    absent from system.billing.usage, so it is ESTIMATED. AWS US East list is a poor
    default for Azure/GCP, so we pick the per-cloud value when the cloud
    is known and fall back to the flat default otherwise. ``cloud`` is a short code
    ('aws'/'azure'/'gcp'); when it is unknown the flat default applies."""
    default = float(t.get("est_vm_cost_per_dbu", EST_VM_COST_PER_DBU))
    by_cloud = t.get("est_vm_cost_per_dbu_by_cloud") or {}
    if cloud and isinstance(by_cloud, dict):
        key = str(cloud).strip().lower()
        if key in by_cloud:
            return float(by_cloud[key])
    return default


@register
class EnableServerlessRule(Rule):
    id = "enable_serverless"
    # Savings model: the DBU-only comparison is measurable (billing rates),
    # but the headline saving credits an ESTIMATED cloud VM cost that billing.usage does
    # not expose — so it is estimated (books $0), with a measurable DBU-only floor (low)
    # and the VM-included ceiling (high).
    default_savings_status = "estimated"
    version = "2.0.0"
    title = "Move eligible SQL warehouses to Serverless"
    resource_type = "warehouse"
    # "Saved so far" (finops_rules.change_tracking): the warehouse became serverless.
    # Binary: only the switch itself counts.
    tracked_setting = TrackedSetting(
        resource="warehouse", column="warehouse_type", proposed_value="SERVERLESS",
        direction="equals", valuation="binary", label="Warehouse type",
    )
    category = "sql"
    # Counterpart (a classic-vs-serverless price gap), but the gap is priced from the
    # CONFIGURED tier rates, not SKU lookups, so there is no contract counterpart yet:
    # score() leaves the contract figure None and the finding shows at list (with the
    # fallback marker). Follow-up: price the serverless SQL SKU via account_prices.
    contract_pricing = "counterpart"
    cloud_savings = "full"  # serverless DBUs include the VM; the classic VM goes away
    sql_file = "enable_serverless.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge: the idle warm-pool share that justifies
        # serverless is a property of the usage rhythm, not of one day's queries.
        # See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < t["min_monthly_cost_usd"]:
            return None  # too small to be worth migrating

        states = row.get("hourly_states") or []
        if not states:
            return None
        agg = summarize_hourly_states(list(states))
        warehouse_type = row.get("warehouse_type")
        tier_rate = _tier_dbu_rate(warehouse_type, t)
        est_vm = _est_vm_cost(row.get("cloud"), t)
        serverless_rate = float(t.get("serverless_dbu_rate", SERVERLESS_SQL_DBU_RATE))
        mig = serverless_migration_savings(
            warm_hours=agg["warm_hours"],
            idle_warm_hours=agg["idle_warm_hours"],
            monthly_cost=monthly_cost,
            tier_dbu_rate=tier_rate,
            serverless_dbu_rate=serverless_rate,
            est_vm_cost_per_dbu=est_vm,
        )
        savings = mig.net_monthly_savings
        # Net-of-premium can be <= 0 for an active-heavy warehouse: the higher
        # serverless rate on the work that still runs outweighs the idle saving.
        if savings <= 0:
            return None
        # The saving is estimate-sensitive (the VM cost is estimated), so only
        # surface warehouses with enough idle headroom that the call is robust.
        # Gated per tier: pro is gated lower because its active-hours premium is
        # often negative, so low-idle pro migrations are still net-positive.
        min_idle = _min_idle_fraction(warehouse_type, t)
        if mig.idle_fraction < min_idle:
            return None

        # split the saving into the MEASURABLE DBU-only comparison
        # (billing.usage rates only, no cloud VM) and the SEPARATE, ESTIMATED cloud-VM
        # range. Re-run the same model with the VM cost zeroed to get the DBU-only net:
        # the classic/pro serverless DBU rate is higher, so for classic this floor is
        # often <= 0 and the migration only pays off ONCE the (estimated) warm VM saving
        # is credited. That is exactly why this is ESTIMATED, not bookable — the low
        # bound is what the billing data can prove, the high bound adds the VM estimate.
        mig_dbu = serverless_migration_savings(
            warm_hours=agg["warm_hours"],
            idle_warm_hours=agg["idle_warm_hours"],
            monthly_cost=monthly_cost,
            tier_dbu_rate=tier_rate,
            serverless_dbu_rate=serverless_rate,
            est_vm_cost_per_dbu=0.0,
        )
        # low = measurable DBU-only floor (>= 0); high/point = net incl. the estimated VM.
        low = max(0.0, mig_dbu.net_monthly_savings)
        high = savings
        status = "estimated"
        # What the band's ends mean for this warehouse (see SAVINGS_CONFIDENCE_MODEL.md).
        range_basis = (
            "Low = the DBU-only saving billing can prove; high = includes the "
            "estimated cloud-VM cost Databricks doesn't expose per-warehouse for "
            "classic/pro — the gap between them is that estimate's uncertainty."
        )

        idle_pct = round(mig.idle_fraction * 100, 1)
        cold = round(float(row.get("avg_cold_start_s") or 0.0), 1)
        name = row.get("warehouse_name") or str(row["warehouse_id"])
        tier_label = "Pro" if (warehouse_type or "").strip().upper() == "PRO" else "Classic"
        # Frame the active-hours rate effect honestly: for classic it is an added
        # cost we net out; for pro serverless can be cheaper on active hours too.
        if mig.active_delta_est > 0:
            active_clause = (
                f" after netting out ~${mig.active_delta_est:,.0f}/mo more for the "
                f"higher serverless rate on active hours"
            )
        else:
            active_clause = " (serverless is also cheaper on this warehouse's active hours)"
        floor_clause = (
            f"At least ~${low:,.0f}/mo is measurable from the DBU bill alone; the rest "
            f"depends on the estimated cloud VM. "
            if low > 0 else
            f"On DBUs alone serverless is not cheaper for this warehouse — the saving "
            f"rests entirely on the estimated cloud VM it folds in. "
        )
        why = (
            f"{tier_label} SQL warehouse '{name}' cost ~${mig.all_in_monthly_est:,.0f}/mo "
            f"all-in (DBUs plus an estimated ~${est_vm:.2f}/DBU cloud VM) and was warm "
            f"but idle {idle_pct:.0f}% of its billed hours. Moving to Serverless scales "
            f"that idle pool to zero for an estimated ~${savings:,.0f}/mo{active_clause}. "
            f"{floor_clause}VM cost is estimated (not in billing data) — treat as "
            f"directional and validate workload compatibility first."
        )
        how = (
            "Switch the warehouse type to Serverless (Compute > SQL Warehouses > "
            "select the warehouse > Edit > Type). Validate workload compatibility first."
        )

        # Show the full lookback window (14-day day×hour heatmap): the warm-but-idle
        # hours (amber) are exactly what Serverless' scale-to-zero would stop billing.
        grid, dates = calendar_grid(list(states), row.get("grid_start"))
        viz = viz_hour_heatmap(
            grid=grid, day_labels=_DAY_LABELS, reclaimable_hours=agg["idle_warm_hours"],
            day_dates=dates,
        )
        evidence = evidence_block(
            # Medium, not high: the dollar figure now depends on an ESTIMATED cloud
            # VM cost that system.billing.usage does not expose for classic/pro.
            confidence=CONFIDENCE_MEDIUM,
            data_basis=_BASIS.format(n=ctx.lookback_days, vm=est_vm)
            + history_note(row, ctx)
            + coverage_note(row),
            viz=viz,
            **history_evidence(row, ctx),
                **coverage_evidence(row),
            warehouse_type=warehouse_type,
            warehouse_size=row.get("warehouse_size"),
            dbus_30d=row.get("dbus_30d"),
            cost_30d_usd=row.get("cost_30d_usd"),
            all_in_monthly_est_usd=mig.all_in_monthly_est,
            est_vm_cost_per_dbu=est_vm,
            cloud=row.get("cloud"),
            idle_saved_est_usd=mig.idle_saved_est,
            active_premium_est_usd=mig.active_delta_est,
            idle_hours_pct=idle_pct,
            avg_cold_start_s=cold,
            avoidable_usd=savings,
            # the measurable DBU-only net (VM zeroed) — the low bound the
            # billing data can prove, separate from the estimated-VM ceiling above.
            net_dbu_only_usd=round(mig_dbu.net_monthly_savings, 2),
            savings_range_basis=range_basis,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=round(savings, 2),
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # estimated — the DBU-only floor (low) is measurable, but the
            # headline figure credits an ESTIMATED cloud VM cost, so it books $0.
            savings_status=status,
            effort=EffortBand.LOW,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence,
        )
