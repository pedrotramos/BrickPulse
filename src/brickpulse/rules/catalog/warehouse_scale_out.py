"""Rule: scale OUT an under-provisioned SQL warehouse (queries queue at capacity).

The mirror image of ``warehouse_rightsize``. Right-size shrinks a warehouse whose
queries never queue and never spill — clear headroom. This one grows a warehouse
whose queries *wait for capacity*: the warehouse ran out of clusters to place
incoming queries on, so they sit in a queue while users wait.

**The lever is ``max_clusters``, not the t-shirt size.** Two different shortages
call for two different fixes, and conflating them makes things worse:

* **Queueing at capacity** is a CONCURRENCY shortage — many queries arriving at
  once, each of which would run fine alone. The fix is another cluster, and
  multi-cluster load balancing adds one only while the queue exists and removes it
  when the queue drains. Sizing up here would burn money on bigger nodes without
  clearing the queue.
* **Spilling to disk** is a per-query MEMORY shortage — the fix is a larger size.
  Surfaced in evidence, but the field data is emphatic that a queueing warehouse
  almost never spills, so this rule targets the concurrency lever.

**Savings are deliberately $0.** Adding a cluster raises the bill during busy
hours; it buys back user wait-time and reliability, not dollars — so claiming a
saving would be a fiction, the same reasoning as ``untagged_spend``. The finding
ranks on ``monthly_waste_usd`` = the measured queue-wait pain (query-hours spent
waiting) instead, and the copy frames it honestly as a performance fix. Because
savings are 0, the NBA ranking places it below every real-money rule, which is
the right order: reclaim idle spend first, but don't leave users queueing.
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
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_wh_scale_out
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import (
    concurrency_implied_clusters,
    warehouse_scale_out_target,
)

_BASIS = (
    "system.query.history (waiting_at_capacity_duration_ms — the time a query "
    "waited for a free cluster; plus measured spill / shuffle-read / scan volumes "
    "that show whether the shortage is concurrency or per-query memory) + "
    "system.compute.warehouses (min/max clusters), {n}-day lookback. Queue wait is "
    "DIRECTLY measured per query, not inferred — high confidence. The recommended "
    "max_clusters is a conservative single step; no dollar saving is claimed "
    "(adding a cluster raises the busy-hour bill)."
)


@register
class WarehouseScaleOutRule(Rule):
    id = "warehouse_scale_out"
    # surfaces measured spill/shuffle volumes for a sizing decision; $0
    # savings booked (advisory).
    default_savings_status = "advisory"
    version = "1.0.0"
    title = "Scale out an under-provisioned SQL warehouse"
    resource_type = "warehouse"
    category = "sql"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "warehouse_scale_out.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge: a single busy afternoon can queue heavily
        # without the warehouse being chronically under-provisioned. See history.
        if insufficient_history(row, ctx):
            return None
        # Coverage guards the DOLLAR figures; this rule books $0 savings but still
        # reports a monthly cost, so keep the same bar as every other rule.
        if underpriced(row):
            return None

        t = ctx.thresholds

        queued_pct = float(row.get("queued_pct") or 0.0)
        if queued_pct < float(t.get("min_queued_pct", 0.10)):
            return None  # not enough of the load is blocked to justify a bill increase

        # Queue wait must be long enough for a human to notice. A warehouse can
        # "queue" a few percent of queries for sub-second times that round to zero
        # hours of wait — real contention, but not worth adding a cluster (and paying
        # for it) over. This magnitude gate is what separates a genuine
        # under-provisioning from statistical noise on the capacity counter.
        p95_wait = float(row.get("p95_queue_wait_s") or 0.0)
        if p95_wait < float(t.get("min_p95_queue_wait_s", 2.0)):
            return None

        cur_max = int(row.get("max_clusters") or 1)
        step_cap = int(t.get("scale_out_step", 2))
        proposed_max = warehouse_scale_out_target(
            current_max_clusters=cur_max,
            queued_pct=queued_pct,
            ceiling=step_cap,
        )
        # Concurrency-implied cluster floor. Measured peak
        # concurrent queries / ~10-per-cluster is a principled sizing signal that
        # CPU/memory miss (a warehouse can exhaust its Spark slots first). When it
        # says the warehouse needs more clusters than the queue-share step alone
        # would add, take the larger target — but still never jump more than
        # step_cap clusters in one recommendation (widen iteratively).
        peak_conc_q = float(row.get("peak_concurrent_queries") or 0.0)
        implied_clusters = concurrency_implied_clusters(
            peak_concurrent_queries=peak_conc_q or None,
            max_queries_per_cluster=float(t.get("max_queries_per_cluster", 10)),
        )
        if implied_clusters is not None and implied_clusters > cur_max and proposed_max:
            proposed_max = max(proposed_max, min(implied_clusters, cur_max + step_cap))
        # Cap at the platform ceiling so we never propose beyond what's allowed.
        ceiling = int(t.get("max_clusters_ceiling", 10))
        if proposed_max is not None:
            proposed_max = min(proposed_max, ceiling)
        if not proposed_max or proposed_max <= cur_max:
            return None  # already at/above the ceiling, or nothing to add

        min_clusters = int(row.get("min_clusters") or 1)
        queries = int(row.get("queries") or 0)
        queued_queries = int(row.get("queued_queries") or 0)
        total_wait_s = float(row.get("total_queue_wait_s") or 0.0)
        avg_wait = float(row.get("avg_queue_wait_s") or 0.0)
        spilled_pct = float(row.get("spilled_pct") or 0.0) * 100
        # Measured query SHAPE — the evidence behind the cluster-vs-size fork below.
        max_spill_gb = float(row.get("max_spill_gb") or 0.0)
        p95_shuffle_gb = float(row.get("p95_shuffle_gb") or 0.0)
        p95_read_gb = float(row.get("p95_read_gb") or 0.0)
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        name = row.get("warehouse_name") or str(row["warehouse_id"])

        # The "waste" this finding ranks on is the queue-wait PAIN, expressed in the
        # same unit the reader thinks in: hours of user time spent waiting for a
        # free cluster, normalized to a month. It is a real, measured number — not a
        # dollar saving, which is exactly why monthly_savings_usd stays 0.
        wait_hours_month = round(to_monthly(total_wait_s, ctx.lookback_days) / 3600.0, 1)

        # Priority by how much of the load is blocked. A warehouse queueing a
        # quarter of its queries is actively hurting; a few percent is a nudge.
        if queued_pct >= 0.25:
            priority = RulePriority.HIGH
        elif queued_pct >= 0.10:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        # Spill is the OTHER shortage; mention it only when present so the reader
        # knows whether a size-up should accompany the extra cluster.
        spill_clause = (
            f" Separately, {spilled_pct:.0f}% of queries also spilled to disk"
            + (f" (worst: {max_spill_gb:.1f} GB)" if max_spill_gb >= 0.1 else "")
            + f", so a larger size may help those specific queries — but the queueing "
            f"itself is a concurrency problem that more clusters, not a bigger size, "
            f"fixes."
            if spilled_pct >= 1.0
            else " These queries are not spilling to disk, so the shortage is "
            "concurrency (too few clusters), not per-query memory (size) — adding a "
            "cluster is the right lever, not sizing up."
        )
        # Lead with the metric that reads honestly for THIS warehouse. When many
        # queries queue, the aggregated monthly wait is the visceral number; when
        # only a handful do (but each waits a long time), that monthly total rounds
        # to ~0 hours and would understate the pain, so lead with the p95 instead.
        if wait_hours_month >= 1.0:
            pain = (
                f"about {wait_hours_month:,.0f} hours of user wait time a month "
                f"(p95 {p95_wait:.0f}s per queued query)"
            )
        else:
            pain = (
                f"each waiting up to {p95_wait:.0f}s (p95) for a free cluster before "
                f"it could run"
            )
        why = (
            f"SQL warehouse '{name}' capped its cluster count at {cur_max}, and over "
            f"the last {ctx.lookback_days} days {queued_pct * 100:.0f}% of its "
            f"{queries:,} queries ({queued_queries:,}) waited for a free cluster — "
            f"{pain}.{spill_clause} This is a performance and reliability problem, not "
            f"a cost saving: raising max_clusters adds spend during busy hours, so no "
            f"dollar saving is claimed."
        )
        # Concurrency corroboration — the measured peak overlap and
        # what it implies at ~N queries/cluster, so the reader sees the cluster count
        # rests on a concurrency measurement, not only the queue share.
        if implied_clusters and peak_conc_q > 0:
            max_qpc = int(t.get("max_queries_per_cluster", 10))
            why += (
                f" At its busiest, ~{int(peak_conc_q)} queries ran concurrently; at "
                f"~{max_qpc} per cluster that points to about {implied_clusters} "
                f"cluster(s) of concurrency capacity."
            )
        auto_stop = row.get("auto_stop_minutes")
        auto_clause = (
            f" Because the warehouse auto-stops after {int(auto_stop)} min of idle, "
            f"the extra cluster is only billed while it is clearing a queue and stops "
            f"when demand falls, so idle-hour cost is unchanged."
            if auto_stop
            else " The extra cluster only spins up while a queue exists and stops "
            "when it drains, so it is billed during busy periods only."
        )
        how = (
            f"Raise the warehouse's maximum clusters from {cur_max} to {proposed_max} "
            f"(SQL Warehouses > the warehouse > Edit > Scaling / Clusters > Max). "
            f"Multi-cluster load balancing adds a cluster when queries queue and "
            f"removes it when the queue drains.{auto_clause} Watch the queued-query "
            f"share and p95 queue wait over the next few days; widen further only if "
            f"queueing persists."
        )

        viz = viz_wh_scale_out(
            current_max_clusters=cur_max,
            proposed_max_clusters=proposed_max,
            min_clusters=min_clusters,
            queued_pct=queued_pct * 100,
            p95_queue_wait_s=p95_wait,
            avg_queue_wait_s=avg_wait,
            queries=queries,
            queued_queries=queued_queries,
            queries_by_hour=row.get("queries_by_hour"),
            queued_by_hour=row.get("queued_by_hour"),
            auto_stop_minutes=int(auto_stop) if auto_stop else None,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            # The size of the problem, in the currency that matters here: the spend
            # exposed to the contention. (Ranking uses monthly_savings, which is 0.)
            monthly_waste_usd=round(monthly_cost, 2),
            # $0 by design: adding a cluster raises the bill; it buys back user
            # wait time, not dollars. See the module docstring.
            monthly_savings_usd=0.0,
            effort=EffortBand.TRIVIAL,  # a scaling dropdown
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_size=row.get("warehouse_size"),
                # Carried so the detail page can judge serverless fit even when
                # this is the only warehouse rule that fired (serverless.fit).
                warehouse_type=row.get("warehouse_type"),
                min_clusters=min_clusters,
                max_clusters=cur_max,
                proposed_max_clusters=proposed_max,
                # Concurrency signal: measured peak concurrent
                # queries and the clusters implied at ~10 queries/cluster.
                peak_concurrent_queries=int(peak_conc_q),
                concurrency_implied_clusters=implied_clusters,
                max_queries_per_cluster=int(t.get("max_queries_per_cluster", 10)),
                queries=queries,
                queued_queries=queued_queries,
                queued_pct=row.get("queued_pct"),
                spilled_pct=row.get("spilled_pct"),
                # Measured query shape behind the cluster-vs-size fork: spill volume
                # (memory pressure -> a size-up may help those queries), shuffle and
                # scan p95 (how heavy the queued queries are). Confirms — rather than
                # asserts — that the shortage here is concurrency, not per-query memory.
                max_spill_gb=round(max_spill_gb, 2),
                p95_shuffle_gb=round(p95_shuffle_gb, 2),
                p95_read_gb=round(p95_read_gb, 2),
                p95_runtime_s=row.get("p95_runtime_s"),
                p95_queue_wait_s=p95_wait,
                avg_queue_wait_s=avg_wait,
                monthly_queue_wait_hours=wait_hours_month,
                auto_stop_minutes=auto_stop,
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
