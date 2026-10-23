"""The rule-knowledge catalog — the calculations, in a table Genie can read.

The Ask Genie space can already slice the *findings*. This module produces the
other half the user asked for: the *reasoning* behind them, as data. For every
enabled rule it captures, in one row:

* what it looks for (title, category, resource_type, the natural-language intent),
* the exact **detection SQL** it runs (rendered with the live thresholds), so a
  user can ask Genie to re-run it against the granted system tables with a
  parameter changed and see how the candidate set moves,
* the **thresholds and their current values** (parsed from the config block), so
  "what if max_acceptable_minutes were 15 instead of 30?" is a concrete question,
* the **scoring / savings logic** as the verbatim ``score()`` source, so Genie can
  explain *why* a specific finding fired and a user can push back on the estimate,
* which **system tables** the rule reads, so Genie knows where to go for detail.

Paired with SELECT on those system tables (granted at deploy time) and the
flattened evidence view, this is what lets a user interrogate and challenge the
numbers rather than just read them.

Pure and framework-agnostic (like ``precompute_transform``): it returns plain
dict records shaped like the ``finops_rule_catalog`` contract, so both precompute
backends and the tests build it identically, and it imports no Spark.
"""

from __future__ import annotations

import inspect
import json

from brickpulse.rules import RuleContext, get_enabled_rules, load_effort, load_thresholds
from brickpulse.rules.registry import list_rules
from brickpulse.rules.sources import rule_dependencies

from .nba_ranker import PRIORITY_MULTIPLIER


def _threshold_block(thresholds: dict, rule_id: str) -> dict:
    """The rule's own config block (its tunable parameters + current values).

    Everything except the engine-wide keys is per-rule, so the block is exactly
    the set of knobs a "what if this were different?" question can turn."""
    block = dict(thresholds.get(rule_id, {}))
    return block


def _scoring_source(rule) -> str:
    """The verbatim ``score()`` implementation — the calculation, as written.

    Genie shows this so a user can see (and argue with) how a candidate row
    becomes a dollar figure: the suppression guards, the savings call, the
    effort/priority. Best-effort: a dynamically-built rule with no Python source
    degrades to an empty string rather than failing the whole catalog."""
    try:
        return inspect.getsource(rule.__class__.score)
    except (OSError, TypeError):
        return ""


def rule_catalog_records(
    *,
    thresholds: dict | None = None,
    lookback_days: int | None = None,
    catalog: str | None = None,
    currency: str = "USD",
) -> list[dict]:
    """One knowledge record per ENABLED rule, shaped like the catalog contract.

    Disabled rules are omitted: the space should describe the engine as it is
    actually configured, not rules that never run. Defaults come from the same
    ``thresholds.yaml`` the pipeline loads, so the catalog reflects the deployed
    configuration by construction.
    """
    thresholds = thresholds if thresholds is not None else load_thresholds()
    lookback_days = lookback_days if lookback_days is not None else int(
        thresholds.get("lookback_days", 30)
    )
    catalog = catalog if catalog is not None else thresholds.get("catalog", "system")
    effort = load_effort()
    priority_mult = PRIORITY_MULTIPLIER

    enabled_ids = {r.id for r in get_enabled_rules(thresholds)}
    records: list[dict] = []
    for rule in list_rules():
        if rule.id not in enabled_ids:
            continue
        ctx = RuleContext(
            thresholds=thresholds.get(rule.id, {}),
            phasing=thresholds.get("phasing"),  # engine-wide phased-downsizing block
            lookback_days=lookback_days,
            catalog=catalog,
            currency=currency,
        )
        try:
            detection_sql = rule.render_sql(ctx)
        except Exception as exc:  # a misconfigured block shouldn't sink the catalog
            detection_sql = f"-- render failed: {exc}"
        block = _threshold_block(thresholds, rule.id)
        records.append({
            "rule_id": rule.id,
            "rule_version": rule.version,
            "rule_title": rule.title,
            "category": rule.category,
            "resource_type": rule.resource_type,
            "enabled": True,
            "lookback_days": lookback_days,
            # The tunable parameters + their current values, as a JSON object, so a
            # what-if question has a concrete starting point.
            "thresholds": json.dumps(block, default=str),
            # The detection SQL, rendered with those values. Re-runnable against the
            # granted system tables with a parameter changed.
            "detection_sql": detection_sql,
            # The scoring/savings logic verbatim — how a candidate becomes a dollar.
            "scoring_logic": _scoring_source(rule),
            # The NBA/effort weighting the ranker applies on top of savings.
            "nba_formula": (
                "nba_score = (monthly_savings_usd / effort_score) * priority_multiplier"
            ),
            "effort_weights": json.dumps(effort, default=str),
            "priority_multipliers": json.dumps(priority_mult, default=str),
            # The system tables this rule reads — where Genie should look for detail.
            "source_tables": json.dumps(sorted(rule_dependencies(rule, ctx)), default=str),
        })
    return records
