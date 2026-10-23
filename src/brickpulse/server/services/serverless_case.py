"""Build the "About Serverless" case from findings the pipeline already produced.

The premise: a platform admin does not need another list of cost tips, they need
to see that a whole *class* of work they do today — sizing clusters, tuning idle
timeouts, chasing runtime upgrades, absorbing spot evictions, explaining start-up
latency — is work serverless deletes. So we group existing findings by the problem
they represent rather than by rule, and attach this account's measured evidence.

Two deliberate choices:

* **Savings are de-duplicated per resource.** An oversized cluster is often
  flagged by several rules; counting each would inflate the total. Each resource
  contributes its single best option, mirroring
  ``services.aggregation._collapse_to_resources``.
* **Hidden rules are read here on purpose.** ``enable_photon_dbr`` and
  ``unused_scheduled_jobs`` are hidden from the recommendation GUI, but the old-DBR
  signal is exactly the evidence this page needs ("you are managing runtime
  upgrades; serverless does that for you"). We read the source *before* the
  hidden-rule filter for that reason, and we count their dollars separately from
  the recommendation totals so nothing double-reports.
"""

from __future__ import annotations

from collections import defaultdict

from ..models import Recommendation
from ..models.group import group_id
from ..models.serverless import (
    ServerlessCase,
    ServerlessIssue,
    ServerlessIssueExample,
    ServerlessReadiness,
)
from ..models.spend import AccountSpend

#: The story. Each entry maps a class of operational pain to the rules that
#: evidence it, in the order we want an admin to read them: sizing first (the
#: biggest, most visible chore), then the config-fiddling, then the platform
#: upkeep. ``rules`` are matched against ``rule_id``.
_ISSUE_SPECS: list[dict] = [
    {
        "key": "cluster_sizing",
        "title": "Cluster sizing you have to get right yourself",
        "rules": ["rightsize_underutilized"],
        "problem": (
            "Someone had to choose a node type and worker count for every cluster, "
            "then keep revisiting it as workloads change. These clusters are "
            "provisioned well above what they actually use, so you pay for headroom "
            "nobody consumes — and the only way to know was to measure it after the "
            "fact."
        ),
        "serverless_answer": (
            "Serverless has no size to pick. Compute scales to the query and you are "
            "billed for what runs, so over-provisioning stops being a decision you "
            "can get wrong."
        ),
        "evidence_note": (
            "Measured from per-minute CPU and memory in system.compute.node_timeline."
        ),
    },
    {
        "key": "idle_compute",
        "title": "Idle compute you pay for between bursts",
        "rules": [
            "idle_zombie_clusters",
            "cluster_autotermination",
            "warehouse_autostop",
            "warehouse_autostop_serverless",
        ],
        "problem": (
            "Classic compute bills while it sits warm doing nothing, so every cluster "
            "and warehouse needs an idle timeout tuned by hand — short enough to stop "
            "waste, long enough that users aren't waiting on restarts. That trade-off "
            "has to be re-made per resource, and it is usually left at the default."
        ),
        "serverless_answer": (
            "Serverless scales to zero on its own and bills per second, so the idle "
            "tail largely disappears and there is no timeout to tune per resource."
        ),
        "evidence_note": (
            "Idle time reconstructed from real query gaps (system.query.history) and "
            "per-minute CPU; auto-stop findings on serverless warehouses are included "
            "because tuning them is the same manual chore."
        ),
    },
    {
        "key": "wrong_compute_class",
        "title": "Workloads running on the wrong kind of compute",
        "rules": ["interactive_to_job", "enable_serverless", "serverless_cost_optimized"],
        "problem": (
            "Scheduled work ends up on always-on interactive clusters because that is "
            "where it was developed, and it then pays the all-purpose rate around the "
            "clock instead of the (cheaper) job rate for the minutes it runs."
        ),
        "serverless_answer": (
            "Serverless jobs and pipelines start per run and stop when the run ends, "
            "so the distinction between 'the cluster I develop on' and 'the compute my "
            "schedule should use' stops costing you money."
        ),
        "evidence_note": (
            "All-purpose DBUs attributed per job by runtime share against the real "
            "all-purpose vs jobs-compute price gap in system.billing.list_prices."
        ),
    },
    {
        "key": "runtime_upkeep",
        "title": "Runtime upgrades and Photon you have to roll out yourself",
        "rules": ["enable_photon_dbr"],
        "problem": (
            "Clusters pinned to old Databricks Runtimes miss performance and price "
            "improvements until somebody upgrades and re-tests them. That is recurring "
            "platform maintenance with no visible payoff, so it slips."
        ),
        "serverless_answer": (
            "Serverless is always on a current runtime with Photon, patched and "
            "upgraded by Databricks. Runtime upkeep leaves your backlog entirely."
        ),
        "evidence_note": (
            "DBR versions read from system.compute.clusters. The speedup is estimated "
            "from typical Photon gains, not measured on your workloads — so treat the "
            "dollar figure here as indicative."
        ),
    },
    {
        "key": "capacity_risk",
        "title": "Spot capacity and eviction handling",
        "rules": ["spot_fallback"],
        "problem": (
            "Getting classic compute cheap means running workers on spot capacity and "
            "accepting evictions — so you either pay the full on-demand price or take "
            "on retry logic and unpredictable runtimes."
        ),
        "serverless_answer": (
            "Capacity sourcing is Databricks' problem on serverless. You do not choose "
            "between price and reliability, and there is no eviction behaviour to design "
            "around."
        ),
        "evidence_note": (
            "On-demand worker spend from system.compute.clusters availability + billing "
            "usage. Spot/on-demand price gaps vary by region and instance type."
        ),
    },
    {
        "key": "reliability_waste",
        "title": "Failed and abandoned work that still bills",
        "rules": ["failing_jobs", "unused_scheduled_jobs", "unused_data_jobs"],
        "problem": (
            "Runs that fail still consume compute, and jobs nobody needs keep running "
            "on schedule. On long-lived classic compute this waste is easy to miss "
            "because the cluster was up anyway."
        ),
        "serverless_answer": (
            "Serverless does not remove the need to fix broken jobs, but per-second "
            "billing on per-run compute makes the cost of a failing or pointless job "
            "visible immediately instead of hiding inside a warm cluster."
        ),
        "evidence_note": (
            "Terminal run states from system.lakeflow.job_run_timeline with cost "
            "attributed per run. Included for completeness — serverless mitigates the "
            "blast radius here rather than eliminating the problem."
        ),
    },
]

#: Cap on examples per issue: enough to make it concrete, not a second table.
_MAX_EXAMPLES = 5


def _best_per_resource(recs: list[Recommendation]) -> dict[tuple, Recommendation]:
    """One (highest-savings) finding per resource — see the module docstring on
    why savings must not be summed across a resource's overlapping options."""
    best: dict[tuple, Recommendation] = {}
    for r in recs:
        key = (r.workspace_id, r.resource_type, r.resource_id)
        cur = best.get(key)
        if cur is None or r.monthly_savings_usd > cur.monthly_savings_usd:
            best[key] = r
    return best


def _example(rec: Recommendation) -> ServerlessIssueExample:
    # The rule's `why` is already a measured, human sentence; its first clause is
    # the specific finding, which is exactly the detail we want here.
    detail = rec.why.split(". ")[0].strip()
    if len(detail) > 180:
        detail = detail[:177].rstrip() + "…"
    return ServerlessIssueExample(
        resource_type=rec.resource_type,
        resource_id=rec.resource_id,
        resource_name=rec.resource_name,
        workspace_id=rec.workspace_id,
        workspace_name=rec.workspace_name,
        detail=detail,
        monthly_savings_usd=round(rec.monthly_savings_usd, 2),
        group_id=group_id(rec.workspace_id, rec.resource_type, rec.resource_id),
    )


def build_case(
    recs: list[Recommendation],
    *,
    spend: AccountSpend | None = None,
    fleet: dict | None = None,
    lookback_days: int = 30,
) -> ServerlessCase:
    """Assemble the serverless case from all findings (hidden rules included).

    ``recs`` should come from the source *before* the hidden-rule filter, so the
    old-DBR evidence is available here even though it is hidden from the
    recommendation GUI (see the module docstring).
    """
    by_rule: dict[str, list[Recommendation]] = defaultdict(list)
    for r in recs:
        by_rule[r.rule_id].append(r)

    issues: list[ServerlessIssue] = []
    # Track resources across ALL issues so the headline total counts each once even
    # when two different issue categories both touch it.
    global_best: dict[tuple, Recommendation] = {}

    for spec in _ISSUE_SPECS:
        members: list[Recommendation] = []
        for rule_id in spec["rules"]:
            members.extend(by_rule.get(rule_id, []))
        if not members:
            continue
        best = _best_per_resource(members)
        for key, rec in best.items():
            cur = global_best.get(key)
            if cur is None or rec.monthly_savings_usd > cur.monthly_savings_usd:
                global_best[key] = rec

        ranked = sorted(best.values(), key=lambda r: r.monthly_savings_usd, reverse=True)
        issues.append(
            ServerlessIssue(
                key=spec["key"],
                title=spec["title"],
                problem=spec["problem"],
                serverless_answer=spec["serverless_answer"],
                resource_count=len(ranked),
                monthly_savings_usd=round(sum(r.monthly_savings_usd for r in ranked), 2),
                evidence_note=spec["evidence_note"],
                examples=[_example(r) for r in ranked[:_MAX_EXAMPLES]],
            )
        )

    # Biggest opportunity first — that is the order an admin should read them in.
    issues.sort(key=lambda i: i.monthly_savings_usd, reverse=True)

    readiness = ServerlessReadiness(spend_available=False)
    if spend is not None and not spend.unavailable:
        total = spend.total_serverless_spend_usd + spend.total_classic_spend_usd
        readiness = ServerlessReadiness(
            serverless_spend_usd=spend.total_serverless_spend_usd,
            classic_spend_usd=spend.total_classic_spend_usd,
            serverless_share=(
                round(spend.total_serverless_spend_usd / total, 4) if total > 0 else 0.0
            ),
            spend_available=True,
        )

    return ServerlessCase(
        total_monthly_savings_usd=round(
            sum(r.monthly_savings_usd for r in global_best.values()), 2
        ),
        affected_resource_count=len(global_best),
        workspace_count=len({r.workspace_id for r in global_best.values()}),
        lookback_days=lookback_days,
        readiness=readiness,
        issues=issues,
        fleet=fleet or {},
    )
