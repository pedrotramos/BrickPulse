"""Rule: instance pools keeping warm VMs on standby (operational).

The "instance pools" opportunity: a pool with
``min_idle_instances > 0`` keeps that many VMs powered on around the clock so a
cluster can grab one with no launch wait. Those warm instances are cloud VMs billed
by the cloud provider, NOT Databricks DBUs — so if the pool is not serving steady
cluster demand, the warm floor is standing spend for nothing.

**Operational, $0.** There is NO system-table price for a pool's idle VMs (they never
appear in system.billing.usage), so this rule books and addresses $0 in DBU terms and
reports the warm-instance COUNT and its activity instead. It is the same "we can see
the waste but cannot price it here" stance as spot_fallback's VM savings.
Promoted out of Labs (2026-09-23) as an advisory rule: shown on the main surfaces, $0 booked.
The finding says so in ``evidence.data_warning``: we can't tell what the pool costs.

**Phased (2026-10-06, docs/ADDING_A_RULE.md "Rules that reduce capacity").** Lowering
the warm floor straight to 0 makes every cluster start wait for a cold VM, so the copy
recommends one bounded step (``phasing.count_plan(X, 0)``: 10 -> 4 -> 2 -> 1 -> 0) and
``evidence.phasing`` carries the plan. Its savings stay $0 (unpriceable).
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_instance_pool
from brickpulse.rules.phasing import (
    count_plan,
    max_step_ratio,
    observe_days,
    phase_sentence,
    phasing_evidence,
)
from brickpulse.rules.registry import register

_BASIS = (
    "system.compute.instance_pools (latest non-deleted config: min_idle_instances = the "
    "always-warm floor) + system.compute.instance_events (instance launches and cluster "
    "acquisitions), {n}-day lookback. The warm floor is read DIRECTLY from config — high "
    "confidence in the SIGNAL. No dollar figure is shown because a pool's idle VMs are "
    "cloud spend billed OUTSIDE Databricks DBUs and have no system.billing.usage row; "
    "this is an operational finding, not a booked or addressable DBU saving. Validate "
    "against the pool's real cluster demand before shrinking it."
)

#: Promoted with $0 on purpose: the warm floor is certain, what it costs is not. Shown
#: above the action (evidence.data_warning) so a $0 finding doesn't read as "free".
_UNPRICED_WARNING = (
    "We can't tell what this pool costs you. Its warm instances are cloud VMs billed by "
    "your cloud provider, and they never appear in Databricks billing or system tables, "
    "so no saving is shown here. It's still worth looking into: check this node type's "
    "hourly price on your cloud bill, multiplied by the warm instances kept around the "
    "clock."
)


_WATCH = "cluster start-up times on the pool"


@register
class InstancePoolIdleRule(Rule):
    id = "instance_pool_idle"
    # Operational: we can see the standing warm capacity but cannot price the VMs from
    # system tables, so nothing is booked and nothing is addressable in DBU terms.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Instance pools keeping warm VMs on standby"
    resource_type = "instance_pool"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "instance_pool_idle.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # NOTE: no history / coverage guard here. A pool with a warm floor but almost no
        # activity is the WORST case (paying to keep VMs warm that nothing acquires), so
        # low observation must not suppress it — it is the finding.
        t = ctx.thresholds
        warm = int(row.get("min_idle_instances") or 0)
        if warm < int(t.get("min_warm_instances", 2)):
            return None

        launches = int(row.get("launches") or 0)
        acquisitions = int(row.get("acquisitions") or 0)
        observed_days = int(row.get("observed_days") or 0)
        autoterm = row.get("idle_instance_autotermination_minutes")
        node_type = row.get("node_type") or "?"
        name = row.get("instance_pool_name") or str(row["instance_pool_id"])

        # Priority on how much standing capacity is held: more warm VMs = more cloud
        # spend at risk. An always-warm pool (min_idle high) with few acquisitions is
        # the strongest case, so a low acquisition count bumps priority.
        underused = acquisitions <= max(1, warm)
        if warm >= 5 or (warm >= 3 and underused):
            priority = RulePriority.HIGH
        elif warm >= 3 or underused:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        acq_clause = (
            f" Over the window only {acquisitions:,} of the pool's instances were "
            f"acquired by a cluster"
            + (f" across {observed_days} active day(s)" if observed_days else "")
            + " — the warm floor looks larger than the demand it serves."
            if underused
            else f" It launched {launches:,} instances and served {acquisitions:,} "
            f"cluster acquisitions over the window."
        )
        why = (
            f"Instance pool '{name}' keeps {warm} {node_type} instance(s) warm at all "
            f"times (min_idle_instances = {warm}). Warm pool instances are cloud VMs "
            f"billed continuously, outside Databricks DBUs — they never show on your DBU "
            f"bill but the cloud provider charges for every hour they stay powered "
            f"on.{acq_clause}"
        )
        plan = count_plan(warm, 0, max_step_ratio=max_step_ratio(ctx))
        obs = observe_days(ctx)
        how = (
            f"If this pool does not need instant, zero-wait capacity, lower "
            f"min_idle_instances from {warm} to {plan[0]} now (0 warms on demand) and set "
            f"idle_instance_autotermination_minutes{'' if autoterm else ' (currently unset)'} "
            f"so idle instances release."
            + phase_sentence(plan=plan, observe_days=obs, watch=_WATCH)
            + " Savings land on your CLOUD bill (EC2 / VM), not your Databricks DBU bill, so "
            "confirm the trade-off against the pool's launch-latency needs — no DBU saving "
            "is booked here."
        )

        viz = viz_instance_pool(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            min_idle_instances=warm,
            idle_autotermination_minutes=int(autoterm) if autoterm is not None else None,
            launches=launches,
            acquisitions=acquisitions,
            node_type=node_type,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="instance_pool",
            resource_id=str(row["instance_pool_id"]),
            resource_name=name,
            # Operational finding: no priceable DBU spend. monthly_waste is $0 (the warm
            # VMs are cloud-billed and unpriceable from system tables); the warm-instance
            # count is the metric that matters, carried in the evidence + why.
            monthly_waste_usd=0.0,
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW,  # a config change on the pool
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days),
                viz=viz,
                min_idle_instances=warm,
                idle_instance_autotermination_minutes=autoterm,
                node_type=node_type,
                max_capacity=row.get("max_capacity"),
                launches=launches,
                acquisitions=acquisitions,
                observed_days=observed_days,
                operational_only=True,
                data_warning=_UNPRICED_WARNING,
                phasing=phasing_evidence(
                    "Min idle instances", warm, plan, observe_days=obs, watch=_WATCH,
                    step_savings=0.0, target_savings=0.0,
                ),
            ),
        )
