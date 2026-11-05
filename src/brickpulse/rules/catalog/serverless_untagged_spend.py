"""Rule: serverless spend that cannot be charged back (missing ownership tags).

The serverless twin of :mod:`untagged_spend`. That rule covers usage carrying a
cluster, warehouse or job id and deliberately excluded the rest, because the rest had
no resource type the app could name. Serverless products now bill against named
resources, and they are where untagged money accumulates: on the verified account
only 142 of 19,111 apps carried a cost-centre tag.

Scope (disjoint from ``untagged_spend`` by construction: no cluster, warehouse or job
id on the usage row):

* Databricks Apps (``app``), serverless pipelines (``pipeline``), Lakebase computes
  (``lakebase_endpoint``), Vector Search endpoints (``vector_search_endpoint``) and
  Model Serving endpoints (``serving_endpoint``).

Why tags and not "missing usage policy": ~98% of the account's serverless usage
carries custom tags while only ~4-13% carries a usage policy id (verified live
2026-11-04), so a missing policy is not by itself an attribution gap. What the
billing record is missing is the measurable thing.

**Savings are $0 by design**, exactly like ``untagged_spend``: tagging changes
attribution, not the bill. ``monthly_waste_usd`` carries the unattributable spend,
the size of the governance gap. Labs until it has been reviewed alongside
``untagged_spend``.
"""

from __future__ import annotations

from brickpulse.rules.catalog.untagged_spend import _missing_tags
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
    "system.billing.usage.custom_tags on serverless usage with no cluster, warehouse "
    "or job id (Apps, serverless pipelines, Lakebase, Vector Search, Model Serving), "
    "over a {n}-day lookback, matched case-insensitively against the same tag aliases "
    "as untagged_spend. Direct measurement of what the billing record carries; a tag "
    "present but blank counts as absent."
)

#: What an admin calls each resource type, for the copy.
_NOUN = {
    "app": "App",
    "pipeline": "Serverless pipeline",
    "lakebase_endpoint": "Lakebase compute",
    "vector_search_endpoint": "Vector Search endpoint",
    "serving_endpoint": "Serving endpoint",
}

#: Where the tags can come from, per resource type. Serverless usage takes its tags
#: from the serverless usage policy it runs under; the five products in scope all
#: bill rows carrying a policy id on the verified account, so all of them accept
#: one. Some also take tags on the resource itself.
_FIX = {
    "app": "assign the app a serverless usage policy that carries them",
    "pipeline": (
        "add them to the pipeline's tags, or assign it a serverless usage policy that "
        "carries them"
    ),
    "lakebase_endpoint": (
        "assign the Lakebase project a serverless usage policy that carries them"
    ),
    "vector_search_endpoint": (
        "assign the endpoint a serverless usage policy that carries them"
    ),
    "serving_endpoint": (
        "add them to the endpoint's tags, or assign it a serverless usage policy that "
        "carries them"
    ),
}


@register
class ServerlessUntaggedSpendRule(Rule):
    id = "serverless_untagged_spend"
    version = "0.1.0"
    title = "Tag serverless spend for chargeback"
    #: Set per finding from the SQL row; the class value is only the registry default.
    resource_type = "app"
    category = "governance"
    sql_file = "serverless_untagged_spend.sql.j2"
    # Labs: net-new coverage, kept out of every main surface until reviewed.
    experimental = True
    # $0 by design: tagging changes attribution, not the bill.
    default_savings_status = "advisory"
    contract_pricing = "none"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A resource seen for a day or two may simply not be tagged YET.
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        resource_type = str(row.get("resource_type") or "")
        if resource_type not in _NOUN:
            return None  # outside the five products this rule knows how to fix
        missing_names = _missing_tags(row.get("missing_tags"))
        missing = len(missing_names)
        if missing < int(t.get("min_missing_tags", 1)):
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < float(t.get("min_monthly_cost_usd", 50)):
            return None

        resource_id = str(row["resource_id"])
        name = row.get("app_name") or resource_id
        required = int(row.get("required_tag_count") or 0)
        identity = row.get("identity_hint")
        noun = _NOUN[resource_type]

        if monthly_cost >= float(t.get("high_priority_monthly_usd", 1000)):
            priority = RulePriority.HIGH
        elif monthly_cost >= float(t.get("medium_priority_monthly_usd", 250)):
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        tag_list = ", ".join(missing_names)
        why = (
            f"{noun} '{name}' billed ~${monthly_cost:,.0f}/mo but its usage is missing "
            f"{missing} of {required} required ownership tags ({tag_list}), so that "
            f"spend cannot be charged back to a team, project or cost centre."
        )
        if identity:
            why += f" Billing records show it was run by {identity}."
        how = (
            f"Add the missing tag(s), {tag_list}: {_FIX[resource_type]}. Tags apply to "
            f"future usage only, so earlier spend stays unattributed. A default usage "
            f"policy per team prevents new serverless resources from billing untagged."
        )
        if identity:
            how += f" If in doubt, ask {identity}, who ran it."

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=name,
            # The unattributable spend: the size of the governance gap.
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=0.0,
            effort=EffortBand.TRIVIAL,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=(
                    _BASIS.format(n=ctx.lookback_days)
                    + history_note(row, ctx)
                    + coverage_note(row)
                ),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                product=row.get("product"),
                missing_tags=missing_names,
                missing_tag_count=missing,
                required_tag_count=required,
                present_tags=sorted(set(t.get("required_tags", {})) - set(missing_names)),
                unattributable_monthly_usd=round(monthly_cost, 2),
                identity_hint=identity,
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
