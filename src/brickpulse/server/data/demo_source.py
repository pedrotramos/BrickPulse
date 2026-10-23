"""Demo mode: realistic, deterministic sample data — no warehouse required.

This source lets the app run fully offline (``FINOPS_COMPUTE_MODE=demo``) for
local UI development, screenshots and self-contained demos. It never opens a SQL
connection: it synthesizes :class:`RuleFinding` objects across a fixed set of
fictional workspaces and turns them into :class:`Recommendation` objects through
the *same* ``build_recommendation`` path production uses, so effort scores, NBA
ranking and ids are computed identically to live/precompute.

The data is deterministic (a fixed seed) so the UI looks the same on every run.
"""

from __future__ import annotations

import hashlib
import os
import random
from datetime import date, timedelta

from ..identity import Scope
from ..models import Recommendation
from .base import RecommendationSource

# --- fictional but plausible account layout -------------------------------
# Numeric ids only (the scoping layer validates workspace ids as digits).
_WORKSPACES: list[tuple[str, str]] = [
    ("1470583920164821", "prod-analytics"),
    ("2938174650298374", "prod-ml-platform"),
    ("3811940275610928", "data-eng-prod"),
    ("4657201938475610", "marketing-analytics"),
    ("5029384756102938", "finance-dwh"),
    ("6120394857612039", "customer360"),
    ("7391028475610293", "iot-streaming"),
    ("8471029384756102", "ds-experimentation"),
    ("9182736450192837", "staging-sandbox"),
    ("1029384756102934", "dev-shared"),
]


# Per-rule "personality" for demo generation. effort/priority mirror what each
# rule's score() emits in the real catalog; the rest shapes plausible numbers
# and copy. Keyed by rule id; unknown rules fall back to sensible defaults.
_RULE_PROFILE: dict[str, dict] = {
    "cluster_autotermination": {
        "effort": "trivial", "priority": "HIGH", "noun": "cluster",
        "waste": (900, 4200), "save_frac": (0.55, 0.85), "n": 9,
        "how": "Set (or shorten) auto-termination on the cluster: Compute > "
               "select cluster > Edit > Terminate after N minutes of inactivity.",
    },
    "warehouse_autostop": {
        "effort": "trivial", "priority": "HIGH", "noun": "warehouse",
        "waste": (1200, 6500), "save_frac": (0.5, 0.8), "band": (0.7, 1.25), "n": 7,
        "how": "Enable / shorten Auto-stop on the SQL warehouse (SQL Warehouses > "
               "select warehouse > Edit > Auto stop after 10 minutes).",
    },
    "idle_zombie_clusters": {
        "effort": "low", "priority": "HIGH", "noun": "cluster",
        "waste": (1500, 9000), "save_frac": (0.7, 0.95), "band": (0.85, 1.15), "n": 8,
        "how": "Terminate the cluster, or attach it to a job / schedule instead of "
               "leaving it running (Compute > select cluster > Terminate).",
    },
    "unused_scheduled_jobs": {
        "effort": "low", "priority": "LOW", "noun": "job",
        "waste": (300, 2200), "save_frac": (0.9, 1.0), "n": 6,
        "how": "Pause or delete the job if it is no longer needed "
               "(Workflows > Jobs > select job > Pause).",
    },
    "rightsize_underutilized": {
        "effort": "medium", "priority": "MEDIUM", "noun": "cluster",
        "waste": (2000, 12000), "save_frac": (0.3, 0.55), "band": (0.6, 1.4), "n": 7,
        "how": "Reduce worker count / node size to match observed utilization, "
               "or enable autoscaling with a lower max.",
    },
    "jobs_compute_rightsize": {
        "effort": "medium", "priority": "MEDIUM", "noun": "job",
        "waste": (1500, 11000), "save_frac": (0.3, 0.6), "band": (0.6, 1.4), "n": 6,
        "how": "Switch a driver-bound job to single-node, cut idle workers, or move "
               "to a smaller-memory node type, then re-test the run.",
    },
    "enable_photon_dbr": {
        "effort": "medium", "priority": "MEDIUM", "noun": "cluster",
        "waste": (2500, 15000), "save_frac": (0.2, 0.4), "band": (0.45, 1.55), "n": 6,
        "how": "Upgrade to a current LTS Databricks Runtime and enable Photon to "
               "cut wall-clock time (and therefore DBU spend) on heavy workloads.",
    },
    "enable_serverless": {
        "effort": "low", "priority": "MEDIUM", "noun": "warehouse",
        # Point/high = the figure incl. the estimated cloud-VM saving; low = the
        # measurable DBU-only floor billing can prove.
        "waste": (1800, 8000), "save_frac": (0.25, 0.45), "band": (0.45, 1.0), "n": 5,
        "how": "Switch the warehouse type to Serverless for instant start/stop and "
               "sub-second scaling, eliminating idle warm-pool spend.",
    },
    "interactive_to_job": {
        "effort": "high", "priority": "MEDIUM", "noun": "cluster",
        # Shared/interactive cluster (the demo's estimated case): the measured price
        # gap is the upper scenario (point/high); low = $0 because moving the jobs off
        # may not let the cluster shrink.
        "waste": (3000, 18000), "save_frac": (0.4, 0.65), "band": (0.0, 1.0), "n": 5,
        "how": "Move the scheduled notebook/workload onto a job cluster so it spins "
               "up per-run and terminates on completion (jobs compute is cheaper).",
    },
    # Advisory, not a savings rule: several jobs run AT ONCE on a shared all-purpose
    # cluster and saturate it, so each runs slower. The all-purpose->job-compute
    # price saving is interactive_to_job's; here save_frac is 0 (same invariant as
    # scale_out/untagged) and `waste` is the contended spend at risk. Fix = isolate
    # the jobs onto their own job clusters.
    "cluster_job_contention": {
        "effort": "high", "priority": "MEDIUM", "noun": "cluster",
        "waste": (400, 3500), "save_frac": (0.0, 0.0), "n": 4,
        "how": "Move the overlapping jobs onto their own job clusters (Workflows > "
               "each job > swap the shared all-purpose cluster for a dedicated job "
               "cluster or serverless job), which removes the contention and bills "
               "at the lower job-compute rate. Upsizing the shared cluster is the "
               "wrong lever — it raises the bill without decoupling the jobs.",
    },
    "spot_fallback": {
        "effort": "low", "priority": "MEDIUM", "noun": "cluster",
        "waste": (2000, 14000), "save_frac": (0.2, 0.45), "band": (0.5, 1.5), "n": 6,
        "how": "Set worker availability to SPOT_WITH_FALLBACK (Compute > cluster > "
               "Edit > Advanced > Availability); keep the driver on-demand.",
    },
    "serverless_cost_optimized": {
        "effort": "low", "priority": "MEDIUM", "noun": "job",
        "waste": (1500, 9000), "save_frac": (0.3, 0.5), "band": (0.7, 1.3), "n": 6,
        "how": "Switch the serverless job/pipeline performance target to Standard "
               "(cost-optimized) mode; confirm the slightly slower start is fine.",
    },
    "warehouse_autostop_serverless": {
        "effort": "trivial", "priority": "HIGH", "noun": "warehouse",
        # Band mirrors the real rule's enable-vs-tighten split: the point is the full
        # proposed setting (the high end), the low is the conservative low-churn floor.
        "waste": (800, 5000), "save_frac": (0.3, 0.6), "band": (0.45, 1.0), "n": 5,
        "how": "Shorten Auto-stop on the serverless SQL warehouse to the value the "
               "chart recommends (SQL Warehouses > select warehouse > Edit > Auto "
               "stop) — short enough to reclaim idle, long enough that users don't "
               "wait on a restart between queries.",
    },
    "failing_jobs": {
        "effort": "medium", "priority": "MEDIUM", "noun": "job",
        "waste": (400, 3500), "save_frac": (0.5, 0.9), "band": (0.5, 1.4), "n": 6,
        "how": "Investigate the failure cause (Workflows > the job > a failed run > "
               "output/logs), fix the root cause or retire the job, and add a "
               "retry/alert policy so failures aren't silently retried.",
    },
    "unused_data_jobs": {
        "effort": "low", "priority": "LOW", "noun": "job",
        "waste": (300, 2600), "save_frac": (0.5, 0.5), "n": 5,
        "how": "Confirm the output really is unused (downstream dashboards, external "
               "readers, ad-hoc queries not captured by lineage), then pause or "
               "delete the job (Workflows > the job > pause/delete).",
    },
    "warehouse_rightsize": {
        "effort": "low", "priority": "MEDIUM", "noun": "warehouse",
        # Band mirrors the real rule: the point is the single measured step (the low
        # end), the high is the deeper size the measured headroom could ultimately reach.
        "waste": (1500, 9000), "save_frac": (0.5, 0.5), "band": (1.0, 1.55), "n": 6,
        "how": "Drop the warehouse one size (SQL Warehouses > the warehouse > Edit "
               "> Cluster size) and watch for new queueing or spill before going further.",
    },
    # The mirror of rightsize: an UNDER-provisioned warehouse whose queries queue at
    # capacity. Not a savings rule — adding a cluster raises the busy-hour bill — so
    # save_frac is 0 (same invariant as untagged_spend); `waste` is the spend exposed
    # to the queueing. The fix is more clusters, not a bigger size.
    "warehouse_scale_out": {
        "effort": "trivial", "priority": "HIGH", "noun": "warehouse",
        "waste": (800, 6000), "save_frac": (0.0, 0.0), "n": 5,
        "how": "Raise the warehouse's max clusters by one (SQL Warehouses > the "
               "warehouse > Edit > Scaling) so multi-cluster load balancing clears "
               "the queue; the extra cluster auto-stops when demand falls.",
    },
    # Zero-traffic endpoints reclaim their FULL cost (save_frac 1.0): the request
    # count is measured per-request, not inferred, so there is nothing to discount.
    "idle_serving_endpoints": {
        "effort": "low", "priority": "MEDIUM", "noun": "endpoint",
        "waste": (400, 4000), "save_frac": (1.0, 1.0), "n": 5,
        "how": "Confirm nothing calls the endpoint, then enable scale-to-zero by "
               "setting min_provisioned_concurrency to 0, or delete it "
               "(Serving > the endpoint > Edit).",
    },
    # DLT pipelines in continuous mode: compute stays up around the clock, so the
    # recoverable share is large (the measured idle fraction on real pipelines was
    # 86-89%). save_frac reflects that, less the start-up overhead a triggered
    # schedule pays back.
    "continuous_pipeline": {
        "effort": "low", "priority": "HIGH", "noun": "pipeline",
        "waste": (300, 2500), "save_frac": (0.6, 0.85), "n": 6,
        "how": "If the data does not need sub-minute freshness, switch the pipeline "
               "to triggered and set a schedule matching how often data arrives "
               "(Pipelines > the pipeline > Settings > uncheck Continuous).",
    },
    # Idle Databricks Apps: a deployed, running app holds serverless compute and
    # bills around the clock even when nobody opens it, so the reclaimable share is
    # the full always-on cost (like idle_serving_endpoints).
    "idle_apps": {
        "effort": "low", "priority": "MEDIUM", "noun": "app",
        "waste": (300, 3000), "save_frac": (1.0, 1.0), "n": 4,
        "how": "Confirm nothing depends on the app, then stop it to release its "
               "compute, or delete it if no longer needed (Compute > Apps > the app).",
    },
    # Chargeback, not savings: tagging a resource changes who the spend is
    # attributed to, never the bill. save_frac is 0 so demo mode honours the same
    # invariant the live rule does — otherwise the demo's savings headline would
    # include money that cannot be banked. `waste` is the unattributable spend.
    "untagged_spend": {
        "effort": "trivial", "priority": "MEDIUM", "noun": "cluster",
        "waste": (600, 9000), "save_frac": (0.0, 0.0), "n": 8,
        "how": "Add the missing ownership tags to the resource, and consider a "
               "cluster policy that requires them so untagged resources cannot be "
               "created.",
    },
    # Labs opportunity rules. Advisory ($0 savings, save_frac 0): `waste` is the
    # ADDRESSABLE compute the finding flags, not a booked saving. Where each appears
    # follows its Rule.experimental flag: sql_repeated_failures, pipeline_failed_updates
    # and instance_pool_idle were promoted to the main surfaces (2026-09-23); the rest
    # stay on the Labs tab.
    "sql_repeated_failures": {
        "effort": "medium", "priority": "HIGH", "noun": "warehouse",
        "waste": (150, 900), "save_frac": (0.0, 0.0), "n": 4,
        "how": "Open Query History filtered to Failed on this warehouse, group by the "
               "repeating statement, and fix or retire the job behind it.",
    },
    "manual_maintenance_overlap": {
        "effort": "low", "priority": "LOW", "noun": "warehouse",
        "waste": (100, 600), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Check whether Predictive Optimization is enabled on these tables "
               "(Catalog Explorer > Details); where it is, retire the manual "
               "OPTIMIZE/VACUUM tasks and let PO maintain them.",
    },
    # C — pipeline failed updates. Advisory with a $ addressable (failed-update compute).
    "pipeline_failed_updates": {
        "effort": "medium", "priority": "HIGH", "noun": "pipeline",
        "waste": (200, 3000), "save_frac": (0.0, 0.0), "n": 5,
        "how": "Open the pipeline's update history, read the error on the failing "
               "updates, and fix or pause it (a missing source, a schema change, an OOM).",
    },
    # D / G / F / H — OPERATIONAL: monthly_waste is $0 (the metric is a count / DBUs,
    # not priceable dollars), so the `waste` range is (0, 0). Their _evidence branches
    # generate the count/token/DBU series that carry the finding.
    "ai_gateway_wasted_inference": {
        "effort": "medium", "priority": "HIGH", "noun": "serving_endpoint",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 5,
        "how": "Filter ai_gateway.usage to this endpoint and status_code >= 400, group "
               "by the failing caller, and fix or throttle it.",
    },
    "instance_pool_idle": {
        "effort": "low", "priority": "MEDIUM", "noun": "instance_pool",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 4,
        "how": "If the pool doesn't need instant capacity, lower min_idle_instances and "
               "set an idle autotermination — savings land on your CLOUD bill, not DBUs.",
    },
    "delta_sharing_materialization": {
        "effort": "medium", "priority": "MEDIUM", "noun": "share",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Check how often recipients read the share; if the cadence outpaces reads, "
               "materialize less often or share live where the recipient allows.",
    },
    # Labs rules scored $0 in production (advisory): a demo profile with a saving
    # fraction would invent a saving the rule never claims.
    "job_runaway_no_timeout": {
        "effort": "low", "priority": "MEDIUM", "noun": "job",
        "waste": (100, 1500), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Set a job timeout a little above its normal longest run (Workflows > "
               "the job > Job details > Timeout) so a hung run stops instead of billing.",
    },
    "pipeline_full_refresh_churn": {
        "effort": "low", "priority": "MEDIUM", "noun": "pipeline",
        "waste": (100, 1500), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Find what triggers the full refreshes (a job task or a person) and switch "
               "it to a normal incremental update unless a schema change needs one.",
    },
    "allpurpose_shape_mismatch": {
        "effort": "low", "priority": "MEDIUM", "noun": "cluster",
        "waste": (200, 2000), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Match the node type to the load: a memory-optimized family where memory "
               "is the bottleneck, a smaller one where it sits unused.",
    },
    # Newer generations list at the same or a higher price: $0 addressable too.
    "instance_generation_modernize": {
        "effort": "medium", "priority": "LOW", "noun": "cluster",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Move to the current node generation when you next touch the cluster; "
               "the benefit is speed, which shows up as shorter runs.",
    },
    "storage_optimization_churn": {
        "effort": "medium", "priority": "MEDIUM", "noun": "table",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 4,
        "how": "Batch small streaming appends, use liquid clustering / a sensible "
               "partition key, and avoid row-by-row MERGEs to reduce fragmentation.",
    },
    # Scored by the real rule from a synthetic row (demo_rule_rows._smart_routing); the
    # profile only sets how many workspaces get one. One finding per workspace.
    "coding_agent_smart_routing": {
        "effort": "low", "priority": "MEDIUM", "noun": "ai_gateway",
        "waste": (0, 0), "save_frac": (0.0, 0.0), "n": 3,
        "how": "Enable the Enhanced Unity Gateway preview and launch with `ug claude`.",
    },
}

_DEFAULT_PROFILE = {
    "effort": "medium", "priority": "MEDIUM", "noun": "resource",
    "waste": (1000, 5000), "save_frac": (0.3, 0.6), "n": 5,
    "how": "Review the resource configuration and apply the recommended change.",
}

# Cosmetic resource-name fragments so the table doesn't read as UUIDs.
# Fictional owners (production reads these from billing identity metadata).
_OWNERS = [
    "anna.berg@example.com", "data-platform-team", "li.wei@example.com",
    "marco.rossi@example.com", "svc-etl-prod", "priya.nair@example.com",
    "ml-engineering", "jonas.keller@example.com",
]

_NAME_BITS = [
    "adhoc", "etl", "nightly", "ml-train", "bi", "ingest", "scratch", "reporting",
    "featurize", "streaming", "backfill", "sandbox", "shared", "explore", "batch",
]


class DemoRecommendationSource(RecommendationSource):
    """In-memory source returning a fixed, realistic recommendation set."""

    def __init__(self, settings, connect):  # connect is accepted but never used
        super().__init__(settings, connect)
        self._cache: list[Recommendation] | None = None

    def load(self, scope: Scope) -> list[Recommendation]:
        recs = self._all()
        if scope.all_workspaces:
            return list(recs)
        allowed = set(scope.workspace_ids)
        return [r for r in recs if r.workspace_id in allowed]

    # -- generation --------------------------------------------------------
    def _all(self) -> list[Recommendation]:
        if self._cache is not None:
            return self._cache
        # Lazy import to avoid a data<->services import cycle at package load.
        from brickpulse.rules import EffortBand, RuleFinding, RulePriority, list_rules

        from brickpulse.rules import load_thresholds

        from ..services.nba_ranker import build_recommendation
        from .demo_rule_rows import scored_demo_finding

        thresholds = load_thresholds()

        snapshot = date.today() - timedelta(days=1)
        ws_name = dict(_WORKSPACES)
        rng = random.Random(20260706)  # deterministic
        recs: list[Recommendation] = []

        # Several rules can land on the same resource id (same workspace, type and
        # index), and the UI groups them into one resource page. Pin the resource's
        # identity — name, monthly spend and owner — the first time it is seen, so
        # every option on that page describes the SAME resource (one name, one
        # cost) instead of each rule inventing its own.
        identity: dict[str, tuple[str, float, str, str]] = {}

        for rule in sorted(list_rules(), key=lambda r: r.id):
            prof = _RULE_PROFILE.get(rule.id, _DEFAULT_PROFILE)
            # Spread this rule's findings across a rotating subset of workspaces.
            picks = rng.sample(_WORKSPACES, min(prof["n"], len(_WORKSPACES)))
            for i, (wid, _wname) in enumerate(picks):
                res_type = rule.resource_type or prof["noun"]
                res_id = f"{res_type[:3]}-{wid[-4:]}-{i + 1:03d}"
                fresh_name = f"{rng.choice(_NAME_BITS)}-{res_type}-{i + 1:02d}"
                fresh_waste = round(rng.uniform(*prof["waste"]), 2)
                fresh_owner = rng.choice(_OWNERS)
                fresh_creator = rng.choice([fresh_owner, fresh_owner, rng.choice(_OWNERS)])
                res_name, waste, owner, creator = identity.setdefault(
                    res_id, (fresh_name, fresh_waste, fresh_owner, fresh_creator)
                )
                if tuple(prof["waste"]) == (0, 0):
                    # Operational / $0 rules: a spend pinned by another rule on the
                    # same resource is not what THIS rule addresses.
                    waste = 0.0
                # Series-based rules derive savings from the generated usage
                # series via the validated savings math (finops_rules.savings);
                # others fall back to the profile's rough fraction.
                computed, evidence = _evidence(rule.id, waste, rng)
                savings = (
                    computed
                    if computed is not None
                    else round(waste * rng.uniform(*prof["save_frac"]), 2)
                )
                evidence["avoidable_usd"] = savings
                # Optional savings BAND for the estimate-tier rules, so demo mode shows
                # the "likely $low–$high/mo" range the SavingsRange component surfaces
                # (production sets these from each rule's own bounds; the demo mirrors
                # that shape via a per-profile fraction). Left None for bookable/advisory
                # rules, whose band is collapsed (low == point).
                band = prof.get("band")
                low = round(savings * band[0], 2) if band else None
                high = round(savings * band[1], 2) if band else None
                # A rule that knows its own band (phased downsizing: low = the first
                # step, high = the data target) sets it instead of the profile fraction.
                if "_low_override" in evidence:
                    low = evidence.pop("_low_override")
                if "_high_override" in evidence:
                    high = evidence.pop("_high_override")
                finding = RuleFinding(
                    workspace_id=wid,
                    resource_type=res_type,
                    resource_id=res_id,
                    resource_name=res_name,
                    monthly_waste_usd=waste,
                    monthly_savings_usd=savings,
                    monthly_savings_low_usd=low,
                    monthly_savings_high_usd=high,
                    effort=EffortBand(prof["effort"]),
                    priority=RulePriority(prof["priority"]),
                    # A rule may supply its own copy when the generic template
                    # would misrepresent the finding (see `_why_override`).
                    why=evidence.pop("_why_override", None)
                    or _why(rule, res_name, waste, savings, evidence.get("phasing")),
                    how=evidence.pop("_how_override", None) or prof["how"],
                    # Per-finding tier where the rule overrides its class default.
                    savings_status=evidence.pop("_status_override", None),
                    evidence=evidence,
                )
                # Rules whose status, copy and chart come out of score(): replace the
                # profile finding with the real rule scoring a synthetic candidate row
                # (demo_rule_rows). The profile finding above is still built first so
                # the shared rng stream — and every other demo finding — is unchanged.
                scored = scored_demo_finding(
                    rule, workspace_id=wid, resource_id=res_id, resource_name=res_name,
                    rng=random.Random(f"{rule.id}|{res_id}"), thresholds=thresholds,
                )
                if scored is not None:
                    finding = scored
                    waste = finding.monthly_waste_usd
                _demo_price_basis(rule, finding, waste)
                recs.append(
                    build_recommendation(
                        rule,
                        finding,
                        snapshot=snapshot,
                        workspace_name=ws_name[wid],
                        # Mirror production's per-workspace host so the deep-link
                        # UI path exercises the same code in demo mode.
                        workspace_url=f"https://demo-{ws_name[wid]}.cloud.databricks.com",
                        # Production resolves these from billing identity metadata;
                        # the demo fills them so the "who do I talk to" line renders.
                        resource_owner=owner,
                        resource_creator=creator,
                    )
                )
        self._cache = recs
        return recs


# --- contract / cloud pricing states ---
#: FINOPS_DEMO_PRICING = list (default; exactly the historical demo) | contract (every
#: SKU ~22 % below list) | contract_partial (most SKUs discounted, a few resources
#: without a contract price — exercises the blended total, coverage chip and the
#: per-row "list" fallback marker). FINOPS_DEMO_CLOUD=1 adds classic cloud-VM cost.
DEMO_PRICING_ENV = "FINOPS_DEMO_PRICING"
DEMO_CLOUD_ENV = "FINOPS_DEMO_CLOUD"


def demo_pricing_state() -> tuple[str, bool]:
    mode = (os.getenv(DEMO_PRICING_ENV) or "list").strip().lower()
    if mode not in ("list", "contract", "contract_partial"):
        mode = "list"
    return mode, (os.getenv(DEMO_CLOUD_ENV) or "0").strip() in ("1", "true", "yes")


def _unit(key: str) -> float:
    """Deterministic [0, 1) from a string — keeps the pricing states from drawing on
    the main demo RNG, so the list-mode demo stays byte-identical."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0x100000000


def _demo_price_basis(rule, finding, waste: float) -> None:
    """Run the demo finding through the REAL price_basis derivation with a
    synthesized candidate row, the way precompute does with a SQL row."""
    mode, cloud = demo_pricing_state()
    if mode == "list" and not cloud:
        return
    from brickpulse.rules.contract import RuleContext
    from brickpulse.rules.price_basis import apply_price_basis

    key = f"{rule.id}|{finding.resource_id}"
    ratio = round(0.78 + (_unit(key + "|r") - 0.5) * 0.06, 4)  # ~22 % off, ±3 pts
    coverage = 1.0
    if mode == "contract_partial" and _unit(key + "|c") < 0.15:
        coverage = round(0.35 + _unit(key + "|c2") * 0.4, 4)   # below the 95 % gate
    row = {
        "cost_30d_usd": waste,
        "contract_ratio": ratio,
        "contract_price_coverage": coverage,
    }
    if cloud and getattr(rule, "cloud_savings", "none") != "none" and _unit(key + "|cl") < 0.85:
        row["cloud_cost_30d_usd"] = round(waste * (0.35 + _unit(key + "|cv") * 0.25), 2)
    if getattr(rule, "contract_pricing", "ratio") == "counterpart" and mode != "list":
        # A counterpart rule prices both SKUs itself; the demo approximates that gap
        # at the same discount (production computes it from both contract prices).
        finding.monthly_savings_contract_usd = round(finding.monthly_savings_usd * ratio, 2)
    ctx = RuleContext(thresholds={}, contract_prices=mode != "list", cloud_costs=cloud)
    apply_price_basis(rule, row, finding, ctx)


def _phase_label(v) -> str:
    """A phasing value for prose: counts as-is, ladder sizes "X_LARGE" -> "X-Large"."""
    if isinstance(v, str):
        return v.replace("_", "-").title()  # same as warehouse_rightsize._fmt
    return str(v)


def _why(rule, resource_name: str, waste: float, savings: float, phasing=None) -> str:
    # Fallback copy for demo rules without a rule-specific override. States the
    # numbers, not "matches the criterion" — restating the rule title told the
    # reader nothing the heading above it didn't.
    share = savings / waste * 100 if waste else 0
    head = (
        f"'{resource_name}' billed ~${waste:,.0f}/mo over the last 30 days; ~${savings:,.0f}/mo "
        f"of that ({share:.0f}%) is avoidable"
    )
    plan = (phasing or {}).get("plan") or []
    if len(plan) > 1:
        # Phased: the figure is the saving at the data target, reached in steps — say
        # so, and what the first step alone is worth (same wording as phase_sentence).
        step_usd = (phasing or {}).get("step_savings_usd")
        worth = f", ~${step_usd:,.0f}/mo on its own" if step_usd else ""
        return (
            f"{head} by stepping down to {_phase_label(plan[-1])} "
            f"({_phase_label(plan[0])} first{worth})."
        )
    return f"{head} with this change."


def _evidence(rule_id: str, waste: float, rng: random.Random) -> tuple[float | None, dict]:
    """Synthesize a finding's evidence: raw metrics + the standardized
    confidence / data_basis / viz block (see finops_rules.evidence). The demo
    builds the *same* shape the live rule ``score()`` emits, so the frontend
    charts render identically offline and in production.

    Returns ``(savings, evidence)``. For series-based rules ``savings`` is
    computed from the generated series via the validated ``finops_rules.savings``
    math (so the demo dollars match production and the benchmark); otherwise it
    is ``None`` and the caller applies the profile's rough fraction."""
    from brickpulse.rules.catalog.idle_serving_endpoints import _UNTRACKED_WARNING
    from brickpulse.rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_LOW,
        CONFIDENCE_MEDIUM,
        DEFAULT_MIN_OBSERVATION_DAYS,
        evidence_block,
        viz_autostop_tradeoff,
        viz_cluster_contention,
        viz_cluster_daily,
        viz_cluster_resources,
        viz_cluster_week_heatmap,
        viz_cpu_area,
        viz_driver_worker_split,
        viz_ai_gateway_errors,
        viz_hour_heatmap,
        viz_instance_pool,
        viz_job_runs,
        viz_pipeline_failures,
        viz_serving_traffic,
        viz_sharing_materialization,
        viz_sql_failures,
        viz_sql_maintenance,
        viz_storage_po,
        viz_wh_rightsize,
        viz_wh_scale_out,
        viz_write_no_read,
    )
    from brickpulse.rules.evidence import (
        cluster_resource_metrics,
        cluster_resources_viz,
        gap_buckets_for_viz,
        gap_histogram_from_row,
    )
    from brickpulse.rules.savings import (
        autostop_savings_tiers,
        choose_serverless_autostop,
        continuous_pipeline_savings,
        failing_job_savings,
        idle_serving_endpoint_savings,
        jobs_compute_rightsize_savings,
        max_safe_steps_down_by_slots,
        rightsize_savings,
        serverless_autostop_savings_at,
        serverless_migration_savings,
        summarize_hourly_states,
        unused_data_job_savings,
        warehouse_autostop_savings,
        warehouse_downsize_savings,
        warehouse_size_slots,
        WAREHOUSE_SIZE_LADDER,
    )
    from brickpulse.rules.phasing import count_plan, ladder_plan, phasing_evidence

    metrics: dict = {"monthly_cost_usd": waste}
    confidence = CONFIDENCE_HIGH
    basis = "system.billing.usage x list_prices, 30-day lookback."
    viz: dict | None = None
    viz_extra: list[dict] = []
    savings: float | None = None

    if rule_id == "warehouse_autostop":
        grid, _ = _gen_hour_heatmap(rng)
        agg = summarize_hourly_states([s for row in grid for s in row])
        savings, reclaimable = warehouse_autostop_savings(
            warm_hours=agg["warm_hours"],
            idle_warm_hours=agg["idle_warm_hours"],
            active_sessions=agg["active_sessions"],
            monthly_cost=waste,
        )
        metrics["warehouse_size"] = rng.choice(["SMALL", "MEDIUM", "LARGE"])
        metrics["dbus_30d"] = round(waste / 0.7)
        metrics["autostop_minutes"] = rng.choice([60, 120, 240])
        metrics["idle_warm_hours_14d"] = reclaimable
        metrics["_why_override"] = (
            f"This warehouse has a {metrics['autostop_minutes']}-minute auto-stop and "
            f"spends ~${waste:,.0f}/mo. The observed state history contains "
            f"~{reclaimable:,.0f} warm-idle hours; a 10-minute auto-stop is estimated "
            f"to reclaim ~${savings:,.0f}/mo. Users may see a cold start after long "
            f"idle gaps."
        )
        basis = (
            "system.compute.warehouse_events + system.query.history, 30-day "
            "lookback. Billing is hourly, so within-hour idle is rounded down "
            "(the real figure may be slightly higher)."
        )
        viz = viz_hour_heatmap(
            grid=grid,
            day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            reclaimable_hours=reclaimable,
        )
    elif rule_id == "enable_serverless":
        grid, _ = _gen_hour_heatmap(rng)
        flat = [s for row in grid for s in row]
        warm_hours = float(sum(1 for s in flat if s in (1, 2)))
        idle_warm_hours = float(sum(1 for s in flat if s == 2))
        idle_pct = round(idle_warm_hours / max(warm_hours, 1) * 100, 1)
        cold = round(rng.uniform(6, 22), 1)
        wh_type = rng.choice(["CLASSIC", "PRO"])
        tier_rate = 0.55 if wh_type == "PRO" else 0.22
        mig = serverless_migration_savings(
            warm_hours=warm_hours,
            idle_warm_hours=idle_warm_hours,
            monthly_cost=waste,
            tier_dbu_rate=tier_rate,
        )
        savings = mig.net_monthly_savings
        metrics["warehouse_type"] = wh_type
        metrics["warehouse_size"] = rng.choice(["SMALL", "MEDIUM", "LARGE"])
        metrics["dbus_30d"] = round(waste / tier_rate)
        metrics["all_in_monthly_est_usd"] = mig.all_in_monthly_est
        metrics["est_vm_cost_per_dbu"] = mig.est_vm_cost_per_dbu
        metrics["idle_saved_est_usd"] = mig.idle_saved_est
        metrics["active_premium_est_usd"] = mig.active_delta_est
        metrics["idle_hours_pct"] = idle_pct
        metrics["avg_cold_start_s"] = cold
        # Estimate-sensitive (VM cost is estimated for classic/pro), so medium.
        confidence = CONFIDENCE_MEDIUM
        metrics["net_dbu_only_usd"] = round(mig.net_monthly_savings * rng.uniform(0.35, 0.6), 2)
        metrics["savings_range_basis"] = (
            "Low = the DBU-only saving billing can prove; high = includes the "
            "estimated cloud-VM cost Databricks doesn't expose per-warehouse for "
            "classic/pro — the gap between them is that estimate's uncertainty."
        )
        basis = (
            "system.query.history (waiting_for_compute_duration_ms) + system.billing.usage, "
            "30-day lookback. Saving = removed all-in idle cost minus the higher serverless "
            "rate on active hours; classic/pro cloud-VM cost is ESTIMATED (not in billing "
            "data). Confirm workload compatibility."
        )
        viz = viz_hour_heatmap(
            grid=grid,
            day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            reclaimable_hours=idle_warm_hours,
        )
    elif rule_id == "continuous_pipeline":
        # Keep the main generator's state identical to the old generic fallback.
        # The caller previously consumed this draw when it estimated savings. Using
        # a separate generator for the new evidence preserves stable resource names,
        # ids and bookmarked demo URLs.
        rng.uniform(0.6, 0.85)
        pipeline_rng = random.Random(f"continuous-pipeline:{waste:.2f}")

        # A continuous pipeline is billed all day. Blue cells are whole hours with
        # an update in flight; amber cells are billed hours with no active update.
        # The production rule receives this same state encoding from SQL.
        grid: list[list[int]] = []
        updates = 0
        active_days = 0
        for day in range(30):
            weekday = (day % 7) < 5
            active_hours: set[int] = set()
            runs_today = 0
            if weekday:
                runs_today = pipeline_rng.choice([1, 1, 2])
                for _ in range(runs_today):
                    start = pipeline_rng.choice([5, 6, 7, 12, 13, 17])
                    duration = pipeline_rng.choice([1, 1, 2])
                    active_hours.update(range(start, min(24, start + duration)))
            elif pipeline_rng.random() < 0.25:
                runs_today = 1
                active_hours.add(pipeline_rng.choice([6, 7, 8]))
            updates += runs_today
            active_days += int(bool(active_hours))
            grid.append([1 if hour in active_hours else 2 for hour in range(24)])

        billed_hours = float(len(grid) * 24)
        update_hours = float(sum(state == 1 for row in grid for state in row))
        idle_hours_gross = billed_hours - update_hours
        is_serverless = pipeline_rng.random() < 0.65
        startup_floor = 0.05 if is_serverless else 0.15
        startup_minutes = 0.5 if is_serverless else 7.0
        cadence_overhead = (updates * startup_minutes) / max(idle_hours_gross * 60, 1)
        startup_overhead = min(1.0, max(startup_floor, cadence_overhead))
        savings, idle_hours = continuous_pipeline_savings(
            billed_hours=billed_hours,
            active_hours=update_hours,
            monthly_cost=waste,
            startup_overhead_frac=startup_overhead,
        )
        avg_update_minutes = round(
            update_hours * 60 / max(updates, 1) * pipeline_rng.uniform(0.55, 0.85), 1
        )
        failed_updates = pipeline_rng.choice([0, 0, 1])
        per_active_day = updates / max(active_days, 1)

        metrics.update(
            billed_hours=round(billed_hours, 1),
            active_hours=round(update_hours, 1),
            idle_hours=idle_hours,
            idle_pct=round(idle_hours_gross / billed_hours * 100, 1),
            updates_30d=updates,
            active_days=active_days,
            avg_update_minutes=avg_update_minutes,
            failed_updates=failed_updates,
            is_serverless=is_serverless,
            startup_overhead_frac=round(startup_overhead, 4),
            startup_overhead_floor=startup_floor,
            cadence_startup_overhead=round(cadence_overhead, 4),
            edition="ADVANCED",
            cost_30d_usd=waste,
            dbus_30d=round(waste / 0.70, 1),
        )
        metrics["_why_override"] = (
            f"This continuous pipeline billed {billed_hours:,.0f} hours in the last "
            f"30 days, but an update was in flight for only {update_hours:,.0f} hours. "
            f"That leaves {idle_hours_gross:,.0f} billed idle hours "
            f"({idle_hours_gross / billed_hours * 100:.0f}% of billed time). It spends "
            f"~${waste:,.0f}/mo, and a triggered schedule is estimated to reclaim "
            f"~${savings:,.0f}/mo after startup overhead. The measured cadence was "
            f"{updates} updates across {active_days} active days "
            f"({per_active_day:.1f} per active day), averaging {avg_update_minutes:.0f} minutes."
        )
        basis = (
            "system.billing.usage billed hours compared with "
            "system.lakeflow.pipeline_update_timeline update hours, 30-day lookback. "
            "Whole-hour matching rounds partly active hours up, so estimated idle "
            "savings are conservative. Input arrival latency is not available in "
            "account-level system tables."
        )
        viz = viz_hour_heatmap(
            grid=grid,
            day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            reclaimable_hours=idle_hours,
            context="pipeline",
        )
    elif rule_id == "rightsize_underutilized":
        cpu, p95, cpu_ts, cpu_idle = _gen_cpu(rng)
        savings = rightsize_savings(p95_cpu=p95, monthly_cost=waste)
        wc = rng.choice([4, 8, 16])
        metrics["worker_count"] = wc
        metrics["target_workers"] = max(1, wc // 2)
        # Phased downsizing: halving is one step here, so the plan is that step and the
        # band collapses onto it (step == target).
        metrics["step_workers"] = metrics["target_workers"]
        metrics["phasing"] = phasing_evidence(
            "Workers", wc, [metrics["target_workers"]], observe_days=7,
            watch="memory, spill, I/O wait and job runtime",
            step_savings=savings, target_savings=savings,
        )
        metrics["_low_override"] = metrics["_high_override"] = savings
        metrics["p95_cpu_pct"] = round(p95 * 100, 1)
        metrics["avg_cpu_pct"] = round(sum(cpu) / len(cpu) * 100, 1)
        # Second chart: whether shrinking is SAFE (memory / swap / I/O headroom),
        # which the CPU-headroom view alone can't answer.
        res_row, kind = _gen_cluster_resources(
            rng, ts=cpu_ts, cpu=cpu, workers=wc, signature="idle"
        )
        metrics["cluster_kind"] = kind
        metrics["node_type"] = res_row["node_type_id"]
        metrics.update(cluster_resource_metrics(res_row))
        mem_peak = float(metrics.get("mem_peak_pct") or 0.0)
        metrics["_why_override"] = (
            f"This {kind[:1].lower() + kind[1:]} averaged {metrics['avg_cpu_pct']:.1f}% CPU and reached "
            f"only {metrics['p95_cpu_pct']:.1f}% at p95 across {wc} workers. Peak "
            f"memory was {mem_peak:.1f}%, so memory does not veto a trial downsize. "
            f"Reducing toward {metrics['target_workers']} workers is estimated to reclaim "
            f"~${savings:,.0f}/mo; validate under representative load because CPU is a "
            f"proxy for the binding resource."
        )
        extra = cluster_resources_viz(row=res_row, lookback_days=30, cluster_kind=kind)
        if extra:
            viz_extra.append(extra)
        basis = (
            "system.compute.node_timeline, per-minute CPU across all nodes, "
            "30-day lookback. CPU is measured directly, but it is a proxy for "
            "the resource that will bind after downsizing."
        )
        confidence = CONFIDENCE_MEDIUM
        viz = viz_cpu_area(
            cpu=cpu, p95=p95, window_label="30 days",
            ts=cpu_ts, idle_frac=cpu_idle, idle_threshold=0.10,
        )
    elif rule_id == "jobs_compute_rightsize":
        # Driver-bound demo: a busy driver, idle workers (a pandas/single-node job).
        wc = rng.choice([2, 4, 8])
        worker_cpu = round(rng.uniform(1.0, 4.5), 1)   # below worker_idle_cpu_pct (5)
        driver_cpu = round(rng.uniform(35.0, 70.0), 1)  # driver carries the work
        worker_mem = round(rng.uniform(8.0, 22.0), 1)   # workers barely touch memory
        driver_mem = round(rng.uniform(40.0, 65.0), 1)
        worker_cost_fraction = wc / (wc + 1)
        savings = jobs_compute_rightsize_savings(
            symptom="driver_bound", monthly_cost=waste,
            worker_cost_fraction=worker_cost_fraction, savings_fraction=0.30,
        )
        metrics["symptom"] = "driver_bound"
        metrics["worker_count"] = wc
        metrics["has_workers"] = True
        metrics["driver_cpu_pct"] = driver_cpu
        metrics["worker_cpu_pct"] = worker_cpu
        metrics["driver_mem_pct"] = driver_mem
        metrics["worker_mem_pct"] = worker_mem
        metrics["worker_cost_fraction"] = round(worker_cost_fraction, 3)
        metrics["node_type"] = rng.choice(["Standard_DS4_v2", "m5d.2xlarge", "i3.xlarge"])
        metrics["run_clusters"] = rng.randint(8, 40)
        confidence = CONFIDENCE_MEDIUM  # measured utilization, inferred target
        basis = (
            "system.compute.node_timeline (driver vs worker CPU/memory) for the job's "
            "ephemeral run-clusters, mapped to the job via billing.usage.usage_metadata "
            "x list_prices, 30-day lookback. Conservative saving on the worker share."
        )
        metrics["_why_override"] = (
            f"This job ran on {wc} worker(s) that averaged {worker_cpu:.1f}% CPU while "
            f"its driver averaged {driver_cpu:.1f}% — the work is on the driver and the "
            f"workers are idle. It spends ~${waste:,.0f}/mo; going single-node reclaims "
            f"~${savings:,.0f}/mo of idle worker compute."
        )
        viz = viz_driver_worker_split(
            driver_cpu=driver_cpu, worker_cpu=worker_cpu,
            driver_mem=driver_mem, worker_mem=worker_mem,
            worker_count=wc, symptom="driver_bound",
        )
    elif rule_id in ("idle_zombie_clusters", "cluster_autotermination"):
        always_on = None
        if rule_id == "cluster_autotermination":
            # An auto-termination finding is about a cluster that stays UP through
            # idle stretches. Every chart on its page (daily bars, weekly heatmap,
            # resource lines) is drawn from ONE usage pattern so they agree —
            # independently generated series contradicted each other.
            always_on = _gen_always_on_cluster(rng)
            days, last_active, peak, avg_cpu = (
                always_on["days"], always_on["last_active"], always_on["peak"],
                always_on["avg_cpu"],
            )
            idle_w = total_w = 0
        else:
            days, last_active, peak, avg_cpu, idle_w, total_w = _gen_cluster_daily(rng)
        metrics["avg_cpu_pct"] = avg_cpu
        # CPU alone can't explain a cluster, so the demo carries the same
        # multi-resource picture production does (see cluster_resources_viz).
        # The hourly strips are derived from the same days the daily chart draws,
        # so a cluster that ran on three days doesn't show two weeks of metrics.
        hourly_ts, hourly_cpu = (
            (always_on["ts"], always_on["cpu"]) if always_on else _hours_from_days(days, rng)
        )
        res_row, kind = _gen_cluster_resources(rng, ts=hourly_ts, cpu=hourly_cpu)
        # Both idle findings are about clusters someone left up; a job cluster
        # terminates when its run ends, so label these as all-purpose.
        if kind.startswith("Job"):
            from brickpulse.rules.evidence import cluster_kind_label

            kind = cluster_kind_label("UI")
        metrics["cluster_kind"] = kind
        metrics["node_type"] = res_row["node_type_id"]
        metrics["worker_count"] = res_row["worker_count"]
        metrics.update(cluster_resource_metrics(res_row))
        extra = cluster_resources_viz(row=res_row, lookback_days=30, cluster_kind=kind)
        if extra:
            viz_extra.append(extra)
        run_h = sum(d["a"] + d["i"] for d in days)
        idle_h = sum(d["i"] for d in days)
        # CPU idle is a proxy for session activity => medium, not high, confidence.
        confidence = CONFIDENCE_MEDIUM
        if rule_id == "cluster_autotermination":
            assert always_on is not None
            # Job clusters terminate when their run ends, so auto-termination is an
            # all-purpose-cluster finding; keep the demo label consistent with that.
            if kind.startswith("Job"):
                from brickpulse.rules.evidence import cluster_kind_label

                kind = cluster_kind_label("UI")
                metrics["cluster_kind"] = kind
            current = rng.choice([0, 0, 120, 240])
            proposed = 60
            # Same wording the live rule emits (catalog/autotermination.py).
            metrics["_how_override"] = (
                f"Set auto-termination to {proposed} minutes (Compute > select the cluster > "
                f"Edit > Auto Termination). First confirm no attached notebook, stream, or "
                f"JDBC session is deliberately holding it — those keep it alive regardless "
                f"of the timeout."
            )
            # Idle stretches longer than the proposed timeout are what it reclaims;
            # the business-hours lulls between bursts are not.
            reclaimable_h = always_on["reclaimable_hours"]
            savings = round(waste * min(0.9, reclaimable_h / max(run_h, 1.0)) * 0.9, 2)
            metrics["auto_termination_minutes"] = current
            metrics["proposed_auto_termination_minutes"] = proposed
            metrics["running_hours"] = round(run_h, 1)
            metrics["idle_hours"] = round(idle_h, 1)
            metrics["reclaimable_hours"] = round(reclaimable_h, 1)
            metrics["longest_idle_gap_hours"] = always_on["longest_gap_hours"]
            metrics["job_runs_30d"] = 0
            metrics["cost_30d_usd"] = waste
            setting = "no" if not current else f"{current}-min"
            metrics["_why_override"] = (
                f"{kind} has {setting} auto-termination. Over the last 30 days it ran "
                f"{run_h:,.0f}h and was idle (CPU below 10%) for {idle_h:,.0f}h of that. "
                f"About {reclaimable_h:,.0f}h of the idle time sat in stretches longer "
                f"than {proposed} min (longest {always_on['longest_gap_hours']:,.0f}h, "
                f"mostly nights and weekends) — worth ~${savings:,.0f}/mo of the "
                f"~${waste:,.0f}/mo spend."
            )
            wk_heat = viz_cluster_week_heatmap(
                active_by_dow_hour=always_on["active_dh"],
                up_by_dow_hour=always_on["up_dh"],
                active_by_hour_minute=always_on["active_hm"],
                lookback_days=30,
            )
            if wk_heat:
                viz_extra.insert(0, wk_heat)  # first extra, before resources
            basis = (
                "system.compute.node_timeline per-minute CPU, 30-day lookback; reclaimable = "
                "idle in contiguous gaps beyond the timeout. Auto-termination keys on session "
                "activity, not exposed for interactive clusters — an upper bound."
            )
        else:
            savings = round(waste * rng.uniform(0.7, 0.95), 2)
            metrics["idle_windows"] = idle_w
            metrics["total_windows"] = total_w
            # A genuine zombie: already on a TIGHT auto-stop yet still idle around
            # the clock (a loose/absent timeout would defer to cluster_autotermination
            # — see idle_zombie_clusters.score()), so terminate is the honest call.
            metrics["auto_termination_minutes"] = rng.choice([20, 30, 45])
            ran_days = sum(1 for d in days if d["a"] or d["i"])
            since = (date.today() - date.fromisoformat(last_active)).days if last_active else None
            metrics["_why_override"] = (
                f"This {kind[:1].lower() + kind[1:]} ran {run_h:,.0f}h on {ran_days} of the last "
                f"30 days and was idle (CPU below 10%) for {idle_h:,.0f}h of that"
                + (f"; it was last active {since} days ago" if since is not None else "")
                + f". It spends ~${waste:,.0f}/mo, with ~${savings:,.0f}/mo estimated as "
                f"reclaimable. CPU idleness is a proxy, so verify held notebooks and "
                f"streams before terminating."
            )
            basis = (
                "system.compute.node_timeline per-minute CPU, 30-day lookback. CPU idle is a "
                "proxy for true inactivity — verify no held session before terminating."
            )
        viz = viz_cluster_daily(
            days=days, lookback_days=30, idle_threshold=0.10,
            last_active=last_active, peak_overall=peak,
        )
    elif rule_id == "spot_fallback":
        # Mirrors the live rule: spot changes the cloud-VM bill, not the
        # DBUs, so nothing is measurable from Databricks billing — advisory, $0.
        savings = 0.0
        confidence = CONFIDENCE_LOW
        metrics["_why_override"] = (
            f"This cluster runs its workers on-demand and spent ~${waste:,.0f}/mo in DBUs over "
            f"the last 30 days. Moving workers to spot (with fallback) can cut the cloud-VM "
            f"part of its cost, which Databricks billing doesn't show."
        )
        basis = (
            "system.compute.clusters (availability = ON_DEMAND) + billing.usage. The spot "
            "discount is on the cloud-provider VM bill, which system tables don't expose."
        )
    elif rule_id == "unused_scheduled_jobs":
        metrics["runs_30d"] = 0
        metrics["last_run_days_ago"] = rng.randint(45, 200)
        basis = "system.lakeflow.job_run_timeline + billing.usage, 30-day lookback."
    elif rule_id == "untagged_spend":
        # Chargeback finding: the number is the spend that cannot be attributed,
        # NOT avoidable waste. The generic demo `why` says "of which ~$X/mo is
        # avoidable waste", which would be flatly wrong here — savings are $0 by
        # design — so it is overridden.
        all_tags = ["owner", "team", "project", "environment", "cost_center"]
        missing = rng.sample(all_tags, rng.randint(1, 3))
        metrics["missing_tags"] = sorted(missing)
        metrics["missing_tag_count"] = len(missing)
        metrics["required_tag_count"] = len(all_tags)
        metrics["present_tags"] = sorted(set(all_tags) - set(missing))
        metrics["unattributable_monthly_usd"] = waste
        metrics["identity_hint"] = rng.choice(
            ["dana@example.com", "sam@example.com", "priya@example.com"]
        )
        tag_list = ", ".join(sorted(missing))
        metrics["_why_override"] = (
            f"This resource billed ~${waste:,.0f}/mo but is missing "
            f"{len(missing)} of {len(all_tags)} required ownership tags "
            f"({tag_list}), so that spend cannot be charged back to a team, project "
            f"or cost centre. Tagging does not reduce the bill — it makes the "
            f"${waste:,.0f}/mo attributable, which is what lets every other finding "
            f"be routed to whoever owns it."
        )
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.billing.usage.custom_tags over a 30-day lookback, matched "
            "case-insensitively against the configured tag aliases. A tag present "
            "but blank counts as absent."
        )
    elif rule_id == "enable_photon_dbr":
        metrics["dbr_version"] = rng.choice(["10.4", "11.3", "12.2"])
        metrics["photon_enabled"] = False
        confidence = CONFIDENCE_MEDIUM
        basis = (
            "system.compute.clusters (DBR version) + billing.usage. Speedup is "
            "estimated from typical Photon gains, not measured on this workload."
        )
    elif rule_id == "interactive_to_job":
        metrics["scheduled_runs_on_interactive_30d"] = rng.randint(20, 300)
        confidence = CONFIDENCE_HIGH
        metrics["savings_range_basis"] = (
            "Low = $0 — this cluster is also used interactively, so moving the jobs "
            "to job compute may not let it shrink; high = the full measured "
            "all-purpose-vs-jobs price gap if it does."
        )
        basis = (
            "system.billing.usage (ALL_PURPOSE DBUs) attributed to each job by "
            "runtime share x the real list_prices all-purpose vs jobs-compute gap. "
            "Only recurring jobs running alone on their cluster are counted."
        )
    elif rule_id == "cluster_job_contention":
        # Advisory: several job runs overlap on a shared all-purpose cluster and
        # saturate it. The signal is the solo->concurrent saturation RISE, so draw
        # a CPU (or memory) that idles solo and pegs under concurrency — the exact
        # shape the live rule requires before it fires.
        peak = rng.randint(3, 6)
        contended_min = rng.randint(120, 900)
        jobs = rng.randint(3, 8)
        cpu_solo = round(rng.uniform(25.0, 45.0), 1)
        cpu_conc = round(rng.uniform(80.0, 92.0), 1)
        cpu_p95 = round(min(99.0, cpu_conc + rng.uniform(4.0, 9.0)), 1)
        mem_solo = round(rng.uniform(50.0, 62.0), 1)
        mem_conc = round(rng.uniform(64.0, 76.0), 1)
        mem_p95 = round(min(95.0, mem_conc + rng.uniform(2.0, 6.0)), 1)
        savings = 0.0  # advisory — the price saving is interactive_to_job's
        metrics["peak_concurrent_runs"] = peak
        metrics["avg_concurrent_runs"] = round(rng.uniform(2.0, float(peak) - 0.5), 2)
        metrics["contended_minutes"] = contended_min
        metrics["contended_job_count"] = jobs
        metrics["cpu_pct_solo"] = cpu_solo
        metrics["cpu_pct_concurrent"] = cpu_conc
        metrics["cpu_pct_concurrent_p95"] = cpu_p95
        metrics["mem_pct_solo"] = mem_solo
        metrics["mem_pct_concurrent"] = mem_conc
        metrics["mem_pct_concurrent_p95"] = mem_p95
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.lakeflow.job_task_run_timeline (distinct job runs live per "
            "minute) joined to system.compute.node_timeline (cluster-wide CPU/memory "
            "that minute), 30-day lookback. Directly measured. Contention is read as "
            "saturation high during overlap AND above the single-job level. No dollar "
            "saving is claimed — the all-purpose vs job-compute price gap is reported "
            "separately."
        )
        viz = viz_cluster_contention(
            cpu_solo=cpu_solo, cpu_concurrent=cpu_conc, cpu_concurrent_p95=cpu_p95,
            mem_solo=mem_solo, mem_concurrent=mem_conc, mem_concurrent_p95=mem_p95,
            peak_concurrent_runs=peak, contended_minutes=contended_min,
            contended_job_count=jobs,
        )
        metrics["_why_override"] = (
            f"Up to {peak} job runs executed at once on this all-purpose cluster "
            f"across {contended_min:,} overlapping minutes ({jobs} jobs involved), and "
            f"its CPU saturated while they competed: {cpu_p95:.0f}% during overlap vs "
            f"{cpu_solo:.0f}% running solo. The overlapping runs share one fixed pool "
            f"of cores, so each finishes slower than it would alone — ~${waste:,.0f}/mo "
            f"of all-purpose spend is buying this self-competing throughput. Isolating "
            f"the jobs onto their own job clusters removes the contention (and bills "
            f"cheaper). This is a performance/reliability fix, not a saving."
        )
    elif rule_id == "failing_jobs":
        (days, total_runs, failed_runs, fail_rate, failed_cost,
         avg_failed_run_cost, avg_success_run_cost) = _gen_job_runs(rng, waste)
        baseline = 0.05
        savings = failing_job_savings(
            failed_run_cost=failed_cost, fail_rate=fail_rate, baseline_fail_rate=baseline,
            avg_failed_run_cost=avg_failed_run_cost,
            avg_success_run_cost=avg_success_run_cost,
        )
        metrics["monthly_cost_usd"] = round(failed_cost, 2)  # waste = failed-run spend
        metrics["total_runs"] = total_runs
        metrics["failed_runs"] = failed_runs
        metrics["fail_rate_pct"] = round(fail_rate * 100, 1)
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.lakeflow.job_run_timeline (terminal result_state per run) + "
            "billing.usage attributed per run via usage_metadata.job_run_id x "
            "list_prices, 30-day lookback. Failed-run cost is measured; savings "
            "credit only the failure rate above the 5% healthy baseline, net of the "
            "compute a fixed run still consumes once it completes successfully."
        )
        viz = viz_job_runs(
            days=days, lookback_days=30, fail_rate=fail_rate,
            baseline_fail_rate=baseline, failed_cost=round(failed_cost, 2),
        )
    elif rule_id == "unused_data_jobs":
        days, total_writes, last_write = _gen_write_no_read(rng)
        savings = unused_data_job_savings(production_cost=waste, recoverable_fraction=0.5)
        metrics["output_tables"] = 1
        metrics["total_writes"] = total_writes
        metrics["total_reads"] = 0
        confidence = CONFIDENCE_LOW
        basis = (
            "system.access.table_lineage (writes with zero corresponding reads) + "
            "billing.usage x list_prices, 30-day lookback. Lineage read-capture is "
            "incomplete, so 'never read' is a proxy — only 50% of the producing "
            "cost is credited and verification is advised before pausing."
        )
        viz = viz_write_no_read(
            days=days, lookback_days=30, total_writes=total_writes,
            total_reads=0, last_write=last_write,
        )
    elif rule_id == "warehouse_autostop_serverless":
        # Draw a usage pattern that the REAL chooser would actually act on. In
        # production a non-recommendable warehouse is simply suppressed (score()
        # returns None); the demo has to emit a finding for this rule, so we redraw
        # rather than fabricate an analysis the chooser never produced — an empty
        # `candidates` list would render the trade-off panel blank.
        for _ in range(12):
            row, warm_min = _gen_serverless_gaps(rng)
            current = row["auto_stop_minutes"]
            hist = gap_histogram_from_row(row)
            proposed, analysis = choose_serverless_autostop(
                gap_histogram=hist, current_autostop_min=current, lookback_days=30,
            )
            if proposed is not None:
                break
        else:
            # Deterministic last resort: a plainly sporadic warehouse (hours of
            # idle between queries), which any candidate value improves.
            row, warm_min = _gen_serverless_gaps(rng, archetype="bursty")
            row["auto_stop_minutes"] = current = 60
            proposed, analysis = choose_serverless_autostop(
                gap_histogram=gap_histogram_from_row(row),
                current_autostop_min=current, lookback_days=30,
            )
        chosen = analysis["chosen"]
        savings, reclaimable = serverless_autostop_savings_at(
            net_reclaimed_min=float(chosen["net_reclaimed_min"]),
            warm_minutes_current=warm_min, monthly_cost=waste,
        )
        metrics["warehouse_size"] = rng.choice(["SMALL", "MEDIUM", "LARGE"])
        metrics["dbus_30d"] = round(waste / 0.7)
        metrics["auto_stop_minutes"] = current
        metrics["proposed_autostop_minutes"] = proposed
        metrics["reclaimable_hours"] = reclaimable
        metrics["extra_restarts_per_day"] = chosen["extra_stops_per_day"]
        metrics["median_idle_gap_min"] = row["median_gap_min"]
        metrics["idle_gaps_analyzed"] = row["gap_count"]
        # Medium here (vs the low-churn HIGH case) so the demo shows the interesting
        # estimated-with-a-real-range card the confidence work is about.
        confidence = CONFIDENCE_MEDIUM
        metrics["savings_range_basis"] = (
            f"Low = the reclaim from a conservative, low-churn auto-stop setting; "
            f"high = the full {proposed}-minute setting, which adds "
            f"~{chosen['extra_stops_per_day']:.1f} extra restart(s)/day."
        )
        # Override the generic demo `why` with copy that states the actual
        # trade-off, so the demo reads like production instead of contradicting
        # the chart next to it.
        why_override = (
            f"This serverless SQL warehouse has {current}-min auto-stop and spent "
            f"~${waste:,.0f}/mo. Across {row['gap_count']:,} idle gaps between its "
            f"queries (median {row['median_gap_min']:,.0f} min), it stayed running — "
            f"billing per-second — through idle tail a {proposed}-minute auto-stop "
            f"would reclaim. {proposed} min is the shortest setting this usage pattern "
            f"tolerates: it adds only ~{chosen['extra_stops_per_day']:.1f} extra "
            f"restart(s) per day. Going shorter would stop the warehouse between "
            f"queries its users fire minutes apart."
        )
        metrics["_why_override"] = why_override
        basis = (
            "system.query.history per-query idle gaps + billing.usage x list_prices, "
            "30-day lookback. The value is chosen from THIS warehouse's gap "
            "distribution: the shortest setting between 5 and 10 min that reclaims "
            "idle without adding more than 3 extra restarts/day. We never recommend "
            "below 5 min — the Databricks UI floor for serverless — because stopping "
            "between every query degrades the experience for its users."
        )
        tiers = autostop_savings_tiers(
            gap_histogram=hist, current_autostop_min=current or float("inf"),
            chosen_minutes=proposed, warm_minutes_current=warm_min,
            monthly_cost=waste, lookback_days=30,
        )
        viz = viz_autostop_tradeoff(
            gap_buckets=gap_buckets_for_viz(row),
            candidates=analysis.get("candidates", []),
            current_minutes=current, chosen_minutes=proposed,
            median_gap_min=row["median_gap_min"], p90_gap_min=row["p90_gap_min"],
            queries_by_hour=row["queries_by_hour"],
            queries_by_dow_hour=row.get("queries_by_dow_hour"),
            tiers=tiers, lookback_days=30,
            max_extra_stops_per_day=3,
        )
    elif rule_id == "warehouse_rightsize":
        current = rng.choice(["MEDIUM", "LARGE", "X_LARGE"])
        # Measured slot occupancy: peak used a small fraction of the size's slots, so
        # a step is safe and confidence is medium (the real rule's primary path). We
        # RECOMMEND one step and show the deeper reachable floor as context.
        size_slots = warehouse_size_slots(current) or 16
        peak_slots = round(rng.uniform(0.5, size_slots * 0.10), 1)
        savings, proposed = warehouse_downsize_savings(
            monthly_cost=waste, current_size=current, steps_down=1
        )
        potential = max(1, max_safe_steps_down_by_slots(
            current_size=current, peak_slots_used=peak_slots, max_steps=8))
        floor_savings, floor_size = warehouse_downsize_savings(
            monthly_cost=waste, current_size=current, steps_down=potential)
        has_further = potential > 1 and floor_size != proposed
        # Phased downsizing (the real rule since 2026-10-06): the point is the floor's
        # saving, the one step is the low end, and the ladder plan rides as phasing.
        step_savings = savings
        plan = ladder_plan(current, floor_size, WAREHOUSE_SIZE_LADDER) if has_further else []
        plan = plan or [proposed]
        if has_further:
            savings = max(step_savings, floor_savings)
        metrics["step_savings_usd"] = step_savings
        metrics["phasing"] = phasing_evidence(
            "Cluster size", current, plan, observe_days=7,
            watch="queued-for-capacity %, spill % and p95 query runtime",
            step_savings=step_savings, target_savings=savings,
        )
        metrics["_low_override"] = step_savings
        metrics["_high_override"] = savings
        # Sits well inside every guard's headroom — that's why it's a candidate.
        queued, spilled = round(rng.uniform(0.0, 0.4), 2), 0.0
        p95_read = round(rng.uniform(0.0, 2.5), 2)
        p95_runtime, peak_conc = round(rng.uniform(1.5, 9.0), 1), rng.randint(2, 8)
        metrics["warehouse_size"] = current
        # Carried so the serverless-fit verdict can classify classic vs serverless
        # (mostly classic here → "serverless wouldn't save" when it's the only rule).
        metrics["warehouse_type"] = rng.choices(["PRO", "SERVERLESS"], weights=[3, 1])[0]
        metrics["proposed_size"] = proposed
        metrics["dbus_30d"] = round(waste / 0.7)
        metrics["queued_pct"] = queued / 100
        metrics["spilled_pct"] = spilled / 100
        metrics["peak_slots_used"] = peak_slots
        metrics["size_slots"] = size_slots
        metrics["slot_util_pct"] = round(100.0 * peak_slots / size_slots, 1)
        confidence = CONFIDENCE_MEDIUM
        basis = (
            "system.query.history measured Spark slot occupancy (total_task_duration_ms "
            "per minute / 60) + queue wait, spill, scanned bytes + billing.usage x "
            "list_prices, 30-day lookback. Peak slot demand is well under the size's "
            "capacity, so one step down is safe — medium confidence; the deeper floor "
            "is shown as context to reach one step at a time."
        )
        metrics["savings_range_basis"] = (
            f"Low = the one measured step to {proposed or 'the next size down'} (peak "
            f"slot demand fits it with room to spare); high = the deeper size the "
            f"measured headroom could ultimately reach — one step at a time. The figure "
            f"shown is the full saving."
        )
        viz = viz_wh_rightsize(
            current_size=current, proposed_size=proposed or "X_SMALL",
            queued_pct=queued, max_queued_pct=2.0,
            spilled_pct=spilled, max_spilled_pct=1.0,
            p95_read_gb=p95_read, max_p95_read_gb=10.0,
            p95_runtime_s=p95_runtime, peak_concurrency=peak_conc,
            peak_slots_used=peak_slots, size_slots=size_slots,
            floor_size=floor_size if has_further else None,
            floor_savings_usd=floor_savings if has_further else None,
        )
    elif rule_id == "warehouse_scale_out":
        # Under-provisioned: a material share of queries queued at capacity, waiting
        # long enough to notice. Not a saving (adding a cluster raises the busy-hour
        # bill); `waste` is the spend exposed to the queueing.
        cur_max = rng.choice([1, 2])
        proposed_max = cur_max + rng.choice([1, 2])
        queued_pct = round(rng.uniform(0.12, 0.55), 3)
        queued_queries = rng.randint(40, 700)
        queries = round(queued_queries / queued_pct)
        p95_wait = round(rng.uniform(3.0, 22.0), 1)
        avg_wait = round(p95_wait * rng.uniform(0.2, 0.35), 2)
        auto_stop = rng.choice([10, 15, 30])
        # Concentrated business-hours usage (so the extra cluster is a peak-hour cost).
        qbh = [0] * 24
        for h in range(8, 19):
            qbh[h] = rng.randint(int(queries * 0.03), int(queries * 0.11) + 1)
        queued_bh = [round(c * queued_pct) for c in qbh]
        savings = 0.0
        metrics["warehouse_size"] = rng.choice(["SMALL", "MEDIUM", "LARGE"])
        # Multi-cluster scale-out applies to both classic and serverless warehouses.
        metrics["warehouse_type"] = rng.choice(["PRO", "SERVERLESS"])
        metrics["min_clusters"] = 1
        metrics["max_clusters"] = cur_max
        metrics["proposed_max_clusters"] = proposed_max
        metrics["queued_pct"] = queued_pct
        metrics["queued_queries"] = queued_queries
        metrics["queries"] = queries
        metrics["p95_queue_wait_s"] = p95_wait
        metrics["avg_queue_wait_s"] = avg_wait
        metrics["auto_stop_minutes"] = auto_stop
        # Measured query shape: a queueing warehouse rarely spills (concurrency, not
        # memory), so spill is ~0 while scan/shuffle show the queued queries' weight.
        metrics["spilled_pct"] = round(rng.uniform(0.0, 0.02), 3)
        metrics["max_spill_gb"] = round(rng.uniform(0.0, 0.4), 2)
        metrics["p95_shuffle_gb"] = round(rng.uniform(0.2, 4.0), 2)
        metrics["p95_read_gb"] = round(rng.uniform(1.0, 30.0), 2)
        metrics["p95_runtime_s"] = round(rng.uniform(2.0, 20.0), 1)
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.query.history (waiting_at_capacity_duration_ms) + "
            "system.compute.warehouses (min/max clusters), 30-day lookback. Queue "
            "wait is directly measured per query — high confidence. No dollar saving "
            "is claimed (adding a cluster raises the busy-hour bill)."
        )
        viz = viz_wh_scale_out(
            current_max_clusters=cur_max, proposed_max_clusters=proposed_max,
            min_clusters=1, queued_pct=queued_pct * 100, p95_queue_wait_s=p95_wait,
            avg_queue_wait_s=avg_wait, queries=queries, queued_queries=queued_queries,
            queries_by_hour=qbh, queued_by_hour=queued_bh, auto_stop_minutes=auto_stop,
        )
        metrics["_why_override"] = (
            f"This SQL warehouse capped its cluster count at {cur_max}, and "
            f"{queued_pct * 100:.0f}% of its {queries:,} queries waited for a free "
            f"cluster before they could run (p95 {p95_wait:.0f}s). These queries "
            f"aren't spilling to disk, so the shortage is concurrency (too few "
            f"clusters), not per-query memory (size). Raising max clusters to "
            f"{proposed_max} lets multi-cluster load balancing clear the queue; the "
            f"added cluster auto-stops when demand falls. This is a performance and "
            f"reliability fix, not a saving — adding a cluster raises the busy-hour "
            f"bill, so no dollar saving is claimed."
        )
    elif rule_id == "idle_serving_endpoints":
        # An always-on endpoint bills EVERY day and serves nobody, so the series is
        # a wall of billed days against a flat-zero request line.
        days_billed = 30
        days = [
            {"d": str(date.today() - timedelta(days=30 - i)), "w": 1, "r": 0}
            for i in range(days_billed)
        ]
        min_conc = 4
        savings = idle_serving_endpoint_savings(
            monthly_cost=waste, requests=0, scale_to_zero_available=True
        )
        metrics["entity_type"] = "CUSTOM_MODEL"
        metrics["min_concurrency"] = min_conc
        metrics["compute_type"] = "CPU"
        metrics["provisioned_throughput"] = False
        metrics["requests_window"] = 0
        metrics["distinct_requesters"] = 0
        metrics["days_billed"] = days_billed
        metrics["dbus_window"] = round(waste / 0.07)
        metrics["cost_window_usd"] = waste
        # Guard 3 of the real rule: some endpoints have NEVER logged a request, so
        # zero traffic can't be told apart from usage tracking being off. Picked by
        # hash (not the main RNG) so the rest of the demo is unchanged.
        unverified = _unit(f"isv-unverified-{waste}") < 0.4
        basis = (
            "system.serving.endpoint_usage (per-request log) + serving.served_entities "
            "x billing.usage x list_prices, 30-day lookback. Requests are COUNTED, not "
            "inferred, and only endpoints that bill while idle (provisioned throughput, "
            "or min_concurrency > 0) are considered."
        )
        metrics["usage_tracking_verified"] = not unverified
        if unverified:
            metrics["traffic_tier"] = "zero_unverified"
            metrics["requests_retained"] = 0
            metrics["data_warning"] = _UNTRACKED_WARNING
            metrics["_status_override"] = "estimated"
            metrics["_how_override"] = (
                "First verify it is really unused: open Serving > the endpoint > Metrics "
                "and check for request traffic, or ask its owner — the request log in "
                "system tables only covers endpoints with usage tracking enabled. Only "
                "then: enable scale-to-zero by setting min_provisioned_concurrency to 0, "
                "or delete it (Serving > the endpoint > Edit)."
            )
            metrics["_why_override"] = (
                f"This endpoint billed on all {days_billed} observed days, costing "
                f"~${waste:,.0f}/mo, and has NO logged inference requests — but it has "
                f"never logged a request at all, so usage tracking may simply be off for "
                f"it. If it really is unused, the full always-on cost is reclaimable; "
                f"that is not yet verified."
            )
            confidence = CONFIDENCE_MEDIUM
            basis += (
                " This endpoint has never logged a request in the retained history, so "
                "zero traffic cannot be told apart from usage tracking being off — "
                "confidence lowered and nothing booked until verified."
            )
        else:
            metrics["traffic_tier"] = "zero"
            metrics["requests_retained"] = 214
            last = date.today() - timedelta(days=41)
            metrics["last_request_retained"] = str(last)
            metrics["_why_override"] = (
                f"This endpoint billed on all {days_billed} observed days, costing "
                f"~${waste:,.0f}/mo, while the per-request serving log recorded 0 inference "
                f"requests. Its last logged request was on {last}, so usage tracking is "
                f"recording it — the silence since is real. Setting min concurrency to "
                f"zero or deleting the endpoint reclaims the full always-on cost; confirm "
                f"no external dependency first."
            )
            # Requests are counted, not inferred, and tracking is demonstrably on.
            confidence = CONFIDENCE_HIGH
        viz = viz_serving_traffic(
            days=days, lookback_days=30, days_billed=days_billed,
            total_requests=0, last_request=None,
        )

    elif rule_id == "sql_repeated_failures":
        # Labs: a warehouse running the same query to failure. Advisory —
        # savings $0; `waste` (the addressable failed-compute) is the headline metric.
        savings = 0.0
        n_days = 14
        days = []
        total_q = failed_q = 0
        for i in range(n_days):
            t = rng.randint(40, 120)
            f = int(t * rng.uniform(0.15, 0.40))
            total_q += t
            failed_q += f
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "t": t, "f": f})
        fail_rate = round(failed_q / max(1, total_q), 4)
        share = round(rng.uniform(0.25, 0.55), 4)
        top_count = max(2, int(failed_q * rng.uniform(0.4, 0.8)))
        shapes = rng.randint(1, 4)
        top_stmt = "INSERT INTO gold.orders SELECT * FROM silver.orders WHERE dt = current_date()"
        metrics["queries"] = total_q
        metrics["failed_queries"] = failed_q
        metrics["fail_rate"] = fail_rate
        metrics["distinct_failed_shapes"] = shapes
        metrics["failed_compute_share"] = share
        metrics["addressable_monthly_usd"] = waste
        metrics["warehouse_type"] = "PRO"
        metrics["top_failed_statement"] = top_stmt
        metrics["top_failed_count"] = top_count
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.query.history (execution_status='FAILED') + billing.usage x "
            "list_prices, 30-day lookback. Experimental (Labs): failures are counted; "
            "the dollar figure is addressable failed-compute, not a booked saving."
        )
        viz = viz_sql_failures(
            days=days, lookback_days=30, fail_rate=fail_rate, failed_queries=failed_q,
            total_queries=total_q, failed_compute_share=share, addressable_monthly_usd=waste,
            top_failed_statement=top_stmt, top_failed_count=top_count,
            distinct_failed_shapes=shapes,
        )
        metrics["_why_override"] = (
            f"This warehouse failed {failed_q:,} of {total_q:,} queries "
            f"({fail_rate * 100:.0f}%) over 14 days across {shapes} repeating shape(s), "
            f"burning ~${waste:,.0f}/mo of compute on work that produced nothing — "
            f"directly avoidable by fixing or retiring the failing query."
        )

    elif rule_id == "manual_maintenance_overlap":
        # Labs: manual OPTIMIZE/VACUUM that Predictive Optimization may
        # already handle. Advisory — savings $0; `waste` is the maintenance compute.
        savings = 0.0
        n_days = 14
        days = []
        opt = vac = 0
        for i in range(n_days):
            o = rng.randint(0, 3)
            v = rng.randint(0, 2)
            opt += o
            vac += v
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "o": o, "v": v})
        share = round(rng.uniform(0.10, 0.30), 4)
        metrics["optimize_runs"] = opt
        metrics["vacuum_runs"] = vac
        metrics["maintenance_runs"] = opt + vac
        metrics["maintenance_compute_share"] = share
        metrics["addressable_monthly_usd"] = waste
        metrics["warehouse_type"] = "PRO"
        confidence = CONFIDENCE_MEDIUM
        basis = (
            "system.query.history (OPTIMIZE / VACUUM statements) + billing.usage x "
            "list_prices, 30-day lookback. Experimental (Labs): the manual maintenance "
            "is counted, but PO coverage of the same tables is unproven here — verify "
            "in Catalog Explorer. Advisory, no booked saving."
        )
        viz = viz_sql_maintenance(
            days=days, lookback_days=30, optimize_runs=opt, vacuum_runs=vac,
            maintenance_compute_share=share, addressable_monthly_usd=waste,
        )
        metrics["_why_override"] = (
            f"This warehouse ran {opt:,} OPTIMIZE and {vac:,} VACUUM statements over "
            f"14 days (~${waste:,.0f}/mo of compute). If Predictive Optimization already "
            f"maintains these tables, that work is redundant — verify in Catalog Explorer."
        )

    elif rule_id == "pipeline_failed_updates":
        # Labs: a DLT pipeline burning compute on FAILED updates. Advisory —
        # savings $0; `waste` (the failed-update compute) is the headline metric.
        savings = 0.0
        n_days = 14
        days = []
        total_u = failed_u = 0
        for i in range(n_days):
            t_ = rng.randint(6, 24)
            f_ = int(t_ * rng.uniform(0.15, 0.45))
            total_u += t_
            failed_u += f_
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "t": t_, "f": f_})
        fail_rate = round(failed_u / max(1, total_u), 4)
        share = round(rng.uniform(0.25, 0.55), 4)
        failed_requests = max(1, int(failed_u * rng.uniform(0.3, 0.6)))
        metrics["updates"] = total_u
        metrics["failed_updates"] = failed_u
        metrics["failed_requests"] = failed_requests
        metrics["fail_rate"] = fail_rate
        metrics["failed_compute_share"] = share
        metrics["addressable_monthly_usd"] = waste
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.lakeflow.pipeline_update_timeline (result_state) + billing.usage x "
            "list_prices, 30-day lookback. Experimental (Labs): failed updates are "
            "counted; the dollar figure is addressable failed-update compute, not booked."
        )
        viz = viz_pipeline_failures(
            days=days, lookback_days=30, fail_rate=fail_rate, failed_updates=failed_u,
            total_updates=total_u, failed_compute_share=share, addressable_monthly_usd=waste,
            failed_requests=failed_requests,
        )
        metrics["_why_override"] = (
            f"This pipeline failed {failed_u:,} of {total_u:,} updates ({fail_rate * 100:.0f}%) "
            f"over 14 days, burning ~${waste:,.0f}/mo of compute on updates that committed "
            f"nothing — usually a broken source or transform."
        )

    elif rule_id == "ai_gateway_wasted_inference":
        # Labs (operational): a serving endpoint failing a high share of its
        # AI-gateway requests. $0 (gateway token cost isn't per-endpoint in billing).
        savings = 0.0
        n_days = 14
        days = []
        total_r = failed_r = 0
        for i in range(n_days):
            t_ = rng.randint(200, 900)
            f_ = int(t_ * rng.uniform(0.2, 0.7))
            total_r += t_
            failed_r += f_
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "t": t_, "f": f_})
        error_rate = round(failed_r / max(1, total_r), 4)
        server_errors = int(failed_r * rng.uniform(0.4, 0.8))
        failed_tokens = int(server_errors * rng.uniform(800, 2500))
        top_code = rng.choice([429, 500, 503, 400])
        top_count = max(2, int(failed_r * rng.uniform(0.4, 0.8)))
        metrics["requests"] = total_r
        metrics["failed_requests"] = failed_r
        metrics["server_errors"] = server_errors
        metrics["failed_input_tokens"] = failed_tokens
        metrics["error_rate"] = error_rate
        metrics["top_status_code"] = top_code
        metrics["top_status_count"] = top_count
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.ai_gateway.usage (status_code >= 400), 30-day lookback. Experimental "
            "(Labs, operational): failed requests are counted; gateway token cost is not "
            "per-endpoint in billing, so the metric is failed requests + wasted tokens."
        )
        viz = viz_ai_gateway_errors(
            days=days, lookback_days=30, error_rate=error_rate, failed_requests=failed_r,
            total_requests=total_r, server_errors=server_errors, failed_input_tokens=failed_tokens,
            top_status_code=top_code, top_status_count=top_count,
        )
        metrics["_why_override"] = (
            f"This serving endpoint errored on {failed_r:,} of {total_r:,} AI-gateway "
            f"requests ({error_rate * 100:.0f}%) over 14 days; {server_errors:,} were 5xx "
            f"that burned ~{failed_tokens:,} input tokens for no usable output — a broken client."
        )

    elif rule_id == "instance_pool_idle":
        # Labs (operational): a pool keeping warm VMs on standby. $0 (cloud VMs).
        savings = 0.0
        n_days = 14
        warm = rng.randint(3, 10)
        node = rng.choice(["i3.xlarge", "m5d.2xlarge", "Standard_DS4_v2"])
        days = []
        launches = acquisitions = 0
        for i in range(n_days):
            l_ = rng.randint(0, 2)
            a_ = rng.randint(0, 1)
            launches += l_
            acquisitions += a_
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "l": l_, "a": a_})
        metrics["min_idle_instances"] = warm
        metrics["idle_instance_autotermination_minutes"] = 0
        pool_plan = count_plan(warm, 0)
        metrics["phasing"] = phasing_evidence(
            "Min idle instances", warm, pool_plan, observe_days=7,
            watch="cluster start-up times on the pool", step_savings=0.0, target_savings=0.0,
        )
        metrics["_how_override"] = (
            f"If this pool doesn't need instant capacity, lower min_idle_instances from "
            f"{warm} to {pool_plan[0]} now and set an idle autotermination. Watch cluster "
            f"start-up times on the pool for about 7 days, then lower it again "
            f"({', then '.join(str(x) for x in pool_plan[1:])}). Savings land on your "
            f"CLOUD bill, not DBUs."
        )
        metrics["node_type"] = node
        metrics["launches"] = launches
        metrics["acquisitions"] = acquisitions
        metrics["operational_only"] = True
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.compute.instance_pools (min_idle_instances) + instance_events, "
            "30-day lookback. Experimental (Labs, operational): warm VMs are cloud-billed "
            "outside DBUs, so this reports the warm-instance count, not dollars."
        )
        viz = viz_instance_pool(
            days=days, lookback_days=30, min_idle_instances=warm,
            idle_autotermination_minutes=0, launches=launches, acquisitions=acquisitions,
            node_type=node,
        )
        metrics["_why_override"] = (
            f"This pool keeps {warm} {node} instance(s) warm at all times "
            f"(min_idle_instances = {warm}) but only {acquisitions} were acquired by a "
            f"cluster over 14 days — warm cloud VMs billed for capacity nothing used."
        )

    elif rule_id == "delta_sharing_materialization":
        # Labs (operational): a share re-materialized repeatedly. $0.
        savings = 0.0
        n_days = 14
        days = []
        total_m = 0
        for i in range(n_days):
            n_ = rng.randint(4, 30)
            total_m += n_
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "n": n_})
        tables = rng.randint(1, 5)
        recipients = rng.randint(1, 4)
        metrics["materializations"] = total_m
        metrics["distinct_tables"] = tables
        metrics["distinct_recipients"] = recipients
        metrics["operational_only"] = True
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.sharing.materialization_history, 30-day lookback. Experimental (Labs, "
            "operational): the table records the event, not its cost, so this reports the "
            "materialization count, not dollars."
        )
        viz = viz_sharing_materialization(
            days=days, lookback_days=30, materializations=total_m,
            distinct_tables=tables, distinct_recipients=recipients,
        )
        metrics["_why_override"] = (
            f"This share materialized its data {total_m:,} times over 14 days across "
            f"{tables} table(s) for {recipients} recipient(s) — rebuilding the same data "
            f"far more often than most recipients read it (compute + cross-region egress)."
        )

    elif rule_id == "storage_optimization_churn":
        # Labs (operational): a table drawing heavy PO maintenance. $0 (DBUs).
        savings = 0.0
        n_days = 14
        days = []
        po_dbus = 0.0
        for i in range(n_days):
            n_ = round(rng.uniform(5, 40), 2)
            po_dbus += n_
            days.append({"d": str(date.today() - timedelta(days=n_days - i)), "n": n_})
        po_dbus = round(po_dbus, 2)
        compaction = round(po_dbus * rng.uniform(0.5, 0.8), 2)
        clustering = round(po_dbus * rng.uniform(0.1, 0.3), 2)
        vacuum = round(max(0.0, po_dbus - compaction - clustering), 2)
        operations = rng.randint(80, 600)
        metrics["po_dbus"] = po_dbus
        metrics["operations"] = operations
        metrics["compaction_dbus"] = compaction
        metrics["clustering_dbus"] = clustering
        metrics["vacuum_dbus"] = vacuum
        metrics["top_operation"] = "COMPACTION"
        metrics["operational_only"] = True
        confidence = CONFIDENCE_HIGH
        basis = (
            "system.storage.predictive_optimization_operations_history, 30-day lookback. "
            "Experimental (Labs, operational): the DBUs are PO's own estimate, not booked "
            "dollars (PO bills under its own SKU, not split per table here)."
        )
        viz = viz_storage_po(
            days=days, lookback_days=30, po_dbus=po_dbus, operations=operations,
            compaction_dbus=compaction, clustering_dbus=clustering, vacuum_dbus=vacuum,
            top_operation="COMPACTION",
        )
        metrics["_why_override"] = (
            f"Predictive Optimization spent ~{po_dbus:,.0f} estimated DBUs across "
            f"{operations:,} operations maintaining this table over 14 days — mostly "
            f"compaction, the classic sign of a fragmenting write pattern (frequent tiny writes)."
        )

    # Demo findings are all "fully observed": the point of demo mode is to show the
    # UI with real-looking findings, and a resource below the observation minimum
    # would be suppressed in production, so it must not appear here either. Stated
    # explicitly (rather than left absent) so the facts strip and the raw evidence
    # table render exactly as they do live. See rules/finops_rules/history.py.
    metrics.setdefault("observed_days", 30)
    metrics.setdefault("min_observation_days", DEFAULT_MIN_OBSERVATION_DAYS)

    evidence = evidence_block(
        confidence=confidence, data_basis=basis, viz=viz, viz_extra=viz_extra, **metrics
    )
    return savings, evidence


# --- deterministic demo series generators ---------------------------------
# These stand in for what the live SQL will aggregate from system tables
# (Commit 2). Shape/units match the viz_* builders exactly.

def _gen_hour_heatmap(rng: random.Random) -> tuple[list[list[int]], int]:
    """14 days x 24 hours of warehouse state. 0=stopped, 1=active, 2=idle-warm.
    Weekdays run ~8-18 with a warm tail (long auto-stop); weekends mostly off."""
    grid: list[list[int]] = []
    reclaimable = 0
    for d in range(14):
        weekday = (d % 7) < 5
        row: list[int] = []
        for h in range(24):
            state = 0
            if weekday:
                if 8 <= h <= 18:
                    state = 1 if rng.random() > 0.18 else 2
                elif 18 < h <= 21:
                    state = 2  # warm tail while auto-stop hasn't fired
                elif h == 7:
                    state = 2
            elif 10 <= h <= 13 and rng.random() > 0.6:
                state = 2  # stray weekend warm-up
            row.append(state)
            if state == 2:
                reclaimable += 1
        grid.append(row)
    return grid, reclaimable


def _gen_cluster_daily(rng: random.Random):
    """30-day daily activity for a cluster: mostly-off with idle-heavy run days
    and occasional real bursts. Returns (days, last_active_iso, peak_overall,
    avg_cpu_pct, idle_windows, total_windows) matching cluster_daily_evidence."""
    lookback = 30
    today = date.today()
    # Two archetypes: an abandoned POC (ran a cluster of days early, then gone)
    # or a rarely-used cluster that flickers on a handful of scattered days.
    abandoned = rng.random() < 0.5
    if abandoned:
        start = rng.randint(16, 26)  # days ago it was last used
        run_days = {start - k for k in range(rng.randint(1, 3))}
    else:
        run_days = set(rng.sample(range(2, lookback), rng.randint(3, 7)))

    days, total_windows, idle_windows, peaks, cpu_sum, cpu_n = [], 0, 0, [], 0.0, 0
    for back in range(lookback - 1, -1, -1):  # oldest -> newest
        d = today - timedelta(days=back)
        if back in run_days:
            up = rng.randint(2, 9)
            active = rng.randint(0, 1) + (1 if rng.random() < 0.5 else 0)
            active = min(active, up)
            idle = up - active
            pk = round(rng.uniform(0.55, 0.9) if active else rng.uniform(0.03, 0.09), 3)
            total_windows += up * 60
            idle_windows += idle * 60 + int(active * 60 * 0.7)
            peaks.append(pk)
            cpu_sum += (0.02 * idle + 0.12 * active); cpu_n += up
            days.append({"d": d.isoformat(), "a": active, "i": idle, "pk": pk})
        else:
            days.append({"d": d.isoformat(), "a": 0, "i": 0, "pk": 0.0})

    active_days = [x for x in days if x["a"] or x["i"]]
    last_active = active_days[-1]["d"] if active_days else None
    peak_overall = round(max(peaks), 3) if peaks else 0.0
    avg_cpu = round((cpu_sum / cpu_n * 100) if cpu_n else 2.0, 1)
    return days, last_active, peak_overall, avg_cpu, idle_windows, max(total_windows, 1)


def _hours_from_days(days: list[dict], rng: random.Random) -> tuple[list[int], list[float]]:
    """Hourly (ts, cpu) samples for the hours the daily bars say the cluster ran:
    each run day's hours start mid-morning, active hours busy, idle hours near 0."""
    import time

    now_hour = int(time.time()) // 3600 * 3600
    today = date.today()
    ts: list[int] = []
    cpu: list[float] = []
    for d in days:
        up = d["a"] + d["i"]
        if not up:
            continue
        back = (today - date.fromisoformat(d["d"])).days
        start = 9
        for k in range(up):
            h = min(23, start + k)
            ts.append(now_hour - (back * 24 + (23 - h)) * 3600)
            # The first active hour carries the day's peak; other active hours are
            # light, so the hourly average matches the daily chart's "mostly idle".
            if k == 0 and d["a"]:
                c = d["pk"]
            elif k < d["a"]:
                c = rng.uniform(0.1, 0.25)
            else:
                c = rng.uniform(0.02, 0.06)
            cpu.append(round(c, 3))
    return ts, cpu


def _gen_always_on_cluster(rng: random.Random) -> dict:
    """One coherent usage pattern for an auto-termination finding: a cluster that is
    UP nearly around the clock but IN USE only on weekday business hours. Returns the
    typical-week heatmap matrices, the 30 per-day bars and an hourly CPU series, all
    derived from the same week so the charts on the page agree with each other."""
    import time

    active_dh = [0] * 168
    up_dh = [0] * 168
    for d in range(7):
        for h in range(24):
            i = d * 24 + h
            # Up almost around the clock (never auto-terminates), with a couple of
            # truly-off graveyard hours to show the "off" state.
            up_dh[i] = 0 if (3 <= h <= 4 and rng.random() < 0.5) else rng.randint(55, 60)
            if d < 5 and 8 <= h <= 18:  # weekday business hours: in use
                active_dh[i] = min(up_dh[i], rng.randint(20, 60))
            elif d < 5 and (7 <= h <= 19):  # shoulders: light use
                active_dh[i] = min(up_dh[i], rng.randint(2, 15))
    # Minute drill: one representative day. A scheduled poller spikes on :00, the
    # rest is scattered ad-hoc use during business hours.
    active_hm = [0] * 1440
    for h in range(24):
        base = 8 if 8 <= h <= 18 else 0
        for mnt in range(60):
            c = base + (12 if mnt == 0 and 8 <= h <= 18 else 0)
            if base and rng.random() < 0.3:
                c += rng.randint(0, 4)
            active_hm[h * 60 + mnt] = c

    lookback = 30
    today = date.today()
    now_hour = int(time.time()) // 3600 * 3600
    days, ts, cpu, peaks = [], [], [], []
    run_h = idle_h = reclaim_h = 0.0
    gap, longest = 0, 0
    for back in range(lookback - 1, -1, -1):  # oldest -> newest
        d = today - timedelta(days=back)
        dow = d.weekday()
        active = idle = 0
        pk = 0.0
        for h in range(24):
            up = up_dh[dow * 24 + h]
            act = active_dh[dow * 24 + h]
            if up == 0:
                gap = 0
                continue
            hour_ts = now_hour - (back * 24 + (23 - h)) * 3600
            if act >= 10:
                active += 1
                gap = 0
                c = min(0.9, 0.18 + act / 60 * 0.5 + rng.random() * 0.1)
                pk = max(pk, min(0.95, c + rng.uniform(0.1, 0.25)))
            else:
                idle += 1
                gap += 1
                longest = max(longest, gap)
                # Idle beyond the first hour is what a 60-min timeout reclaims.
                if gap > 1:
                    reclaim_h += 1
                c = 0.02 + rng.random() * 0.03
            ts.append(hour_ts)
            cpu.append(round(c, 3))
        run_h += active + idle
        idle_h += idle
        if active:
            peaks.append(pk)
        days.append({"d": d.isoformat(), "a": active, "i": idle, "pk": round(pk, 3)})
    last_active = next((x["d"] for x in reversed(days) if x["a"]), None)
    # Production sums each weekday x hour cell over the whole lookback (SQL
    # dow_activity), so scale the typical week by how often each weekday occurred —
    # the heatmap totals then agree with the per-day bars above.
    occurrences = [0] * 7
    for x in days:
        occurrences[date.fromisoformat(x["d"]).weekday()] += 1
    active_dh = [v * occurrences[i // 24] for i, v in enumerate(active_dh)]
    up_dh = [v * occurrences[i // 24] for i, v in enumerate(up_dh)]
    return {
        "active_dh": active_dh, "up_dh": up_dh, "active_hm": active_hm,
        "days": days, "ts": ts, "cpu": cpu, "last_active": last_active,
        "peak": round(max(peaks), 3) if peaks else 0.0,
        "avg_cpu": round(sum(cpu) / max(len(cpu), 1) * 100, 1),
        "reclaimable_hours": reclaim_h, "longest_gap_hours": float(longest),
    }


def _gen_cpu(rng: random.Random) -> tuple[list[float], float, list[int], list[float]]:
    """~14 days of hourly CPU samples (0..1) hugging low utilization. Returns
    (cpu, p95, ts, idle_frac): ts = unix-second hour timestamps ending ~now;
    idle_frac = share of the hour below the 10% idle threshold."""
    import math
    import time

    n = 336  # 14 days of hourly points
    now_hour = int(time.time()) // 3600 * 3600
    cpu: list[float] = []
    ts: list[int] = []
    idle_frac: list[float] = []
    for i in range(n):
        v = 0.06 + abs(math.sin(i / 11)) * 0.05 + rng.random() * 0.03
        if i % 24 in (2, 3, 4):
            v += 0.09  # a nightly job bump
        if rng.random() > 0.98:
            v += 0.10  # rare peak
        v = min(v, 0.95)
        cpu.append(v)
        ts.append(now_hour - (n - 1 - i) * 3600)
        # more idle minutes the lower the hour's average CPU
        idle_frac.append(round(max(0.0, min(1.0, (0.14 - v) / 0.14)), 3))
    ordered = sorted(cpu)
    p95 = ordered[int(len(ordered) * 0.95)]
    return cpu, p95, ts, idle_frac


def _gen_job_runs(rng: random.Random, waste: float):
    """~14 days of daily job outcomes for the failing-jobs demo. Returns
    (days, total_runs, failed_runs, fail_rate, failed_cost, avg_failed_run_cost,
    avg_success_run_cost). A chronically-failing job: a high, sustained failure
    share across the window. failed_cost is derived so the failed-run spend is
    proportional to the failed share."""
    lookback = 14
    today = date.today()
    fail_rate = round(rng.uniform(0.35, 0.85), 3)
    runs_per_day = rng.randint(6, 24)
    days, total_runs, failed_runs = [], 0, 0
    for back in range(lookback - 1, -1, -1):
        d = today - timedelta(days=back)
        r = runs_per_day + rng.randint(-2, 2)
        f = max(0, min(r, round(r * fail_rate + rng.randint(-1, 1))))
        s = r - f
        days.append({"d": d.isoformat(), "s": s, "f": f})
        total_runs += r
        failed_runs += f
    actual_rate = failed_runs / max(total_runs, 1)
    # waste = the failed-run spend (the chart's callout figure).
    failed_cost = round(waste, 2)
    succeeded_runs = total_runs - failed_runs
    avg_failed_run_cost = failed_cost / failed_runs if failed_runs else 0.0
    # A clean run of this job costs less than a failed one (the failure was cutting
    # it short); pick a plausible ratio so the demo shows the completion-cost netting
    # reducing the credit below the full failed spend.
    avg_success_run_cost = (
        round(avg_failed_run_cost * rng.uniform(0.3, 0.6), 2) if succeeded_runs else None
    )
    return (
        days, total_runs, failed_runs, round(actual_rate, 3), failed_cost,
        round(avg_failed_run_cost, 2), avg_success_run_cost,
    )


def _gen_cluster_resources(
    rng: random.Random, ts=None, cpu=None, workers: int | None = None,
    signature: str | None = None,
) -> tuple[dict, str]:
    """Hourly multi-resource samples for a demo cluster, shaped like the columns
    the cluster SQL emits. Returns ``(row, cluster_kind_label)``.

    Picks one of three plausible signatures so the chart actually teaches
    something: ``idle`` (everything low — genuinely oversized/abandoned),
    ``memory_bound`` (low CPU but high memory, sometimes swapping — do NOT shrink)
    and ``io_bound`` (low CPU, high I/O wait — the bottleneck isn't cores)."""
    import math
    import time

    if ts is None:
        n = 336  # 14 days hourly
        now_hour = int(time.time()) // 3600 * 3600
        ts = [now_hour - (n - 1 - i) * 3600 for i in range(n)]
    n = len(ts)
    if cpu is None:
        cpu = [
            min(0.95, 0.05 + abs(math.sin(i / 13)) * 0.06 + rng.random() * 0.03)
            for i in range(n)
        ]

    signature = signature or rng.choice(["idle", "memory_bound", "io_bound"])
    if signature == "memory_bound":
        mem = [min(0.97, 0.74 + abs(math.sin(i / 17)) * 0.16) for i in range(n)]
        swap = [max(0.0, (m - 0.88) * 0.6) for m in mem]
        iowait = [0.02 + rng.random() * 0.03 for _ in range(n)]
    elif signature == "io_bound":
        mem = [0.34 + rng.random() * 0.08 for _ in range(n)]
        swap = [0.0] * n
        iowait = [min(0.7, 0.22 + abs(math.sin(i / 9)) * 0.3) for i in range(n)]
    else:
        mem = [0.22 + rng.random() * 0.1 for _ in range(n)]
        swap = [0.0] * n
        iowait = [0.01 + rng.random() * 0.02 for _ in range(n)]

    # Network tracks CPU loosely (work moves data), in bytes/minute.
    net = [max(0.0, c * rng.uniform(3.0, 9.0) * 1024 * 1024) for c in cpu]
    kind = rng.choice(["UI", "UI", "API", "JOB"])
    row = {
        "cpu_ts": ts,
        "cpu_hourly": cpu,
        "mem_hourly": mem,
        "swap_hourly": swap,
        "iowait_hourly": iowait,
        "net_hourly": net,
        "node_type_id": rng.choice(
            ["Standard_D4ds_v5", "Standard_E8ds_v4", "i3.xlarge", "m5d.2xlarge"]
        ),
        "worker_count": workers if workers is not None else rng.choice([2, 4, 8]),
    }
    from brickpulse.rules.evidence import cluster_kind_label

    return row, cluster_kind_label(kind)


def _gen_serverless_gaps(
    rng: random.Random, archetype: str | None = None,
) -> tuple[dict, float]:
    """A serverless warehouse's inter-query gap distribution + billed warm time.

    Shapes a realistic interactive/BI pattern: most queries arrive within a few
    minutes of each other during working hours (the gaps a short auto-stop would
    thrash on), plus long overnight/weekend idle (the gaps worth reclaiming). Fed
    through the real ``choose_serverless_autostop`` so the demo shows the same
    trade-off the live rule computes. Returns a row shaped like the rule SQL."""
    current = rng.choice([15, 20, 30, 60])
    # Three archetypes, so the demo shows the trade-off actually discriminating
    # rather than always landing on the floor:
    #   "bursty"      — sporadic ad-hoc use, long idle => the short setting wins.
    #   "interactive" — a BI/dashboard warehouse queried every few minutes all day
    #                   => a short setting would restart constantly, so it needs a
    #                   longer value inside the window.
    #   "mixed"       — working sessions plus real overnight idle.
    archetype = archetype or rng.choice(["bursty", "interactive", "mixed"])
    if archetype == "bursty":
        tight, mid, longer, overnight = (
            rng.randint(60, 140), rng.randint(20, 50),
            rng.randint(120, 260), rng.randint(60, 120),
        )
    elif archetype == "interactive":
        # Heavy 5-10 min think-time band: this is what punishes a 5-min auto-stop.
        tight, mid, longer, overnight = (
            rng.randint(300, 700), rng.randint(900, 1800),
            rng.randint(80, 160), rng.randint(25, 45),
        )
    else:
        tight, mid, longer, overnight = (
            rng.randint(200, 500), rng.randint(250, 500),
            rng.randint(150, 320), rng.randint(40, 80),
        )
    row = {
        "gap_hist_1": round(tight * 0.5), "gap_hist_2": round(tight * 0.2),
        "gap_hist_3": round(tight * 0.14), "gap_hist_4": round(tight * 0.09),
        "gap_hist_5": round(tight * 0.07),
        "gap_hist_6": round(mid * 0.3), "gap_hist_7": round(mid * 0.25),
        "gap_hist_8": round(mid * 0.2), "gap_hist_9": round(mid * 0.15),
        "gap_hist_10": round(mid * 0.1),
        "gap_hist_15": round(longer * 0.4), "gap_hist_20": round(longer * 0.25),
        "gap_hist_30": round(longer * 0.2), "gap_hist_60": round(longer * 0.15),
        "gap_hist_over": overnight,
        "auto_stop_minutes": current,
        "median_gap_min": round(rng.uniform(3.0, 9.0), 1),
        "p90_gap_min": round(rng.uniform(45.0, 180.0), 1),
        # Business-hours weighted rhythm, with a little overnight ETL.
        "queries_by_hour": [
            rng.randint(0, 3) if h < 7 or h > 19 else rng.randint(8, 60)
            for h in range(24)
        ],
        # 168 day-of-week x hour buckets (index = dow*24 + h, dow 0=Mon..6=Sun):
        # busy weekdays, near-empty weekends — so the demo heatmap reads like the
        # real weekly rhythm the auto-stop acts on.
        "queries_by_dow_hour": [
            (0 if (k // 24) >= 5 else (rng.randint(0, 2) if (k % 24) < 7 or (k % 24) > 19
                                       else rng.randint(6, 55)))
            for k in range(168)
        ],
    }
    # The hour-of-day profile is the weekly matrix folded over its days, as it is in
    # production (both come from the same query history). The independent draw above
    # is kept only so the generator's random stream — and every demo figure after
    # it — stays unchanged.
    row["queries_by_hour"] = [
        sum(row["queries_by_dow_hour"][d * 24 + h] for d in range(7)) for h in range(24)
    ]
    # Measured minutes in the >60 bucket: overnight gaps average ~8h in this demo
    # shape, so the over-bucket keys near its true length instead of a flat 1440.
    row["gap_over_minutes"] = round(overnight * rng.uniform(240.0, 600.0))
    row["gap_count"] = sum(
        v for k, v in row.items() if k.startswith("gap_hist_")
    )
    # Billed warm time: every gap holds the warehouse open up to the current
    # setting, so a long setting inflates warm minutes (and the reclaimable share).
    warm = float(row["gap_count"]) * min(current, 12.0) + row["gap_count"] * 1.5
    return row, warm


def _gen_write_no_read(rng: random.Random):
    """~14 days of daily write counts for the unused-data demo, reads pinned at
    zero. Returns (days, total_writes, last_write_iso)."""
    lookback = 14
    today = date.today()
    per_day = rng.randint(24, 96)
    days, total = [], 0
    for back in range(lookback - 1, -1, -1):
        d = today - timedelta(days=back)
        w = per_day + rng.randint(-6, 6)
        days.append({"d": d.isoformat(), "w": w, "r": 0})
        total += w
    return days, total, today.isoformat()
