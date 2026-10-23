"""finops_rules — the pluggable cost-saving rule engine.

Public surface:

* :class:`Rule`, :class:`RuleContext`, :class:`RuleFinding`, :class:`EffortBand`,
  :class:`RulePriority` — the authoring contract.
* :func:`register` / :func:`list_rules` / :func:`get_enabled_rules` — discovery.
* :func:`load_thresholds` / :func:`load_effort` — externalized config.

Rules are added by dropping a file in ``catalog/`` + a template in ``sql/`` and
a block in ``config/thresholds.yaml``. Nothing else needs editing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .contract import (
    DEFAULT_MIN_OBSERVATION_DAYS,
    EFFORT_SCORES,
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from .evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    cluster_activity_stats,
    cluster_daily_evidence,
    cluster_kind_label,
    cluster_resource_metrics,
    cluster_resources_viz,
    evidence_block,
    gap_buckets_for_viz,
    gap_histogram_from_row,
    viz_autostop_tradeoff,
    viz_burst_compare,
    viz_cluster_daily,
    viz_cluster_resources,
    viz_cluster_contention,
    viz_cluster_week_heatmap,
    viz_cpu_area,
    viz_driver_worker_split,
    viz_hour_heatmap,
    viz_job_runs,
    viz_ai_gateway_errors,
    viz_instance_pool,
    viz_pipeline_failures,
    viz_sharing_materialization,
    viz_sql_failures,
    viz_sql_maintenance,
    viz_storage_po,
    viz_serving_traffic,
    viz_wh_rightsize,
    viz_wh_scale_out,
    viz_write_no_read,
)
from .history import (
    effective_min_days,
    history_evidence,
    history_note,
    insufficient_history,
    observed_days,
)
from .registry import discover, get_enabled_rules, get_rule, list_rules, register

CONFIG_DIR = Path(__file__).parent / "config"

__all__ = [
    "Rule",
    "RuleContext",
    "RuleFinding",
    "EffortBand",
    "RulePriority",
    "EFFORT_SCORES",
    "DEFAULT_MIN_OBSERVATION_DAYS",
    "insufficient_history",
    "effective_min_days",
    "observed_days",
    "history_note",
    "history_evidence",
    "register",
    "discover",
    "list_rules",
    "get_rule",
    "get_enabled_rules",
    "load_thresholds",
    "apply_disabled",
    "load_effort",
    "evidence_block",
    "viz_hour_heatmap",
    "viz_burst_compare",
    "viz_cpu_area",
    "viz_cluster_daily",
    "viz_job_runs",
    "viz_driver_worker_split",
    "viz_write_no_read",
    "viz_wh_rightsize",
    "viz_wh_scale_out",
    "viz_sql_failures",
    "viz_sql_maintenance",
    "viz_ai_gateway_errors",
    "viz_pipeline_failures",
    "viz_instance_pool",
    "viz_sharing_materialization",
    "viz_storage_po",
    "viz_serving_traffic",
    "viz_autostop_tradeoff",
    "viz_cluster_resources",
    "viz_cluster_contention",
    "viz_cluster_week_heatmap",
    "gap_histogram_from_row",
    "gap_buckets_for_viz",
    "cluster_daily_evidence",
    "cluster_kind_label",
    "cluster_resource_metrics",
    "cluster_resources_viz",
    "cluster_activity_stats",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_LOW",
]


@lru_cache(maxsize=1)
def _base_thresholds() -> dict:
    return _load_yaml("thresholds.yaml")


def load_thresholds() -> dict:
    """Per-rule thresholds + engine-wide settings from config/thresholds.yaml.

    Rules listed in BRICKPULSE_DISABLED_RULES (comma-separated) are switched off:
    the deploy sets it when a rule's system tables aren't readable."""
    from brickpulse.environment import get_env

    return apply_disabled(_base_thresholds(), get_env("BRICKPULSE_DISABLED_RULES", ""))


def apply_disabled(thresholds: dict, rule_ids) -> dict:
    """A copy of ``thresholds`` with ``enabled: false`` for each listed rule id."""
    if isinstance(rule_ids, str):
        rule_ids = [r.strip() for r in rule_ids.split(",") if r.strip() != "none"]
    out = dict(thresholds)
    for rid in filter(None, rule_ids):
        out[rid] = {**(out.get(rid) or {}), "enabled": False}
    return out


@lru_cache(maxsize=1)
def load_effort() -> dict[str, int]:
    """Effort-band -> numeric score overrides from config/effort.yaml."""
    data = _load_yaml("effort.yaml")
    return {k: int(v) for k, v in (data or {}).items()}


def _load_yaml(name: str) -> dict:
    import yaml  # lazy: only required when config is actually loaded

    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}
