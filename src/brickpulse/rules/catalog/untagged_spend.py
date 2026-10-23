"""Rule: spend that cannot be charged back (missing ownership tags).

The FinOps pillar the catalog was missing. Every other rule answers "what are we
paying for that we don't need"; this one answers "whose spend is this?" — and
without an answer, the other fifteen rules' findings have nobody to send them to.
A platform admin looking at "this cluster wastes $800/mo" across 400 workspaces
cannot act until each row has an owner.

**Savings are deliberately $0.** Tagging a cluster does not reduce the bill by one
cent, and claiming otherwise would inflate the headline total with money that
cannot be banked — the exact failure the engine's core invariant forbids (an
estimate must never exceed true avoidable spend). What the finding carries instead
is ``monthly_waste_usd``: the spend that is currently *unattributable*. That is a
real, checkable number, and it makes the rule rank on the size of the governance
gap rather than on a fiction.

Because savings are 0, the NBA ranking (savings-per-effort) naturally places these
below every rule with real money attached, which is the right order: fix the
$800/mo idle cluster first, but know who owns it.
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

def _missing_tags(value) -> list[str]:
    """Normalize the ``missing_tags`` column to a list of tag names.

    The three readers hand back an ARRAY<STRING> in three different shapes, and
    two of them are traps:

    * Spark / the SQL connector: a real ``list`` — use it directly.
    * The REST statement API: the JSON TEXT ``'["team","cost_center"]'``. Iterating
      that string yields single characters, which produced "missing 22 of 5 tags
      (a, c, c, e, ...)" before this existed.
    * A precompute snapshot round-tripped through ``to_json``: also JSON text.

    Anything unparseable yields ``[]``, which suppresses the finding rather than
    emitting a garbled one — a governance report that lists punctuation as a
    missing tag is worse than no report.
    """
    if value is None:
        return []
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if isinstance(value, (list, tuple)):
        return sorted(str(v) for v in value if v)
    return []


_BASIS = (
    "system.billing.usage.custom_tags over a {n}-day lookback, matched "
    "case-insensitively against the configured tag aliases. Direct measurement of "
    "what the billing record carries — a tag present but blank counts as absent."
)

#: How an admin refers to each resource type in the UI, for the "how" text.
_UI_PATH = {
    "cluster": "Compute > select the cluster > Edit > Advanced options > Tags",
    "warehouse": "SQL Warehouses > select the warehouse > Edit > Tags",
    "job": "Workflows > select the job > Edit > Tags",
}


@register
class UntaggedSpendRule(Rule):
    id = "untagged_spend"
    # governance/sidecar finding, $0 savings by design (advisory). The fix
    # changes attribution, not the bill.
    default_savings_status = "advisory"
    version = "1.0.0"
    title = "Tag spend for chargeback"
    #: Set per finding from the SQL row (a cluster, warehouse or job). The class
    #: attribute is only the default the registry sees; ``score`` overrides it, and
    #: all three values are types the frontend already deep-links.
    resource_type = "cluster"
    category = "governance"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "untagged_spend.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A resource seen for a day or two may simply not have been tagged YET —
        # someone is probably still building it. Nagging about it is noise.
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        # The SQL hands us the missing tag NAMES rather than per-tag booleans, so
        # nothing here has to guess how a given reader spelled `false`. See the
        # note on the `missing_tags` column in the template.
        missing_names = _missing_tags(row.get("missing_tags"))
        missing = len(missing_names)
        if missing < int(t.get("min_missing_tags", 1)):
            return None  # adequately tagged

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        # Floor: below this the governance gap is real but not worth an admin's
        # attention, and a per-resource finding for every trivial cluster would bury
        # the rules that carry actual money.
        if monthly_cost < float(t.get("min_monthly_cost_usd", 50)):
            return None

        resource_type = str(row.get("resource_type") or "cluster")
        resource_id = str(row["resource_id"])
        name = row.get("job_name") or resource_id
        required = int(row.get("required_tag_count") or 0)
        identity = row.get("identity_hint")

        # Priority tracks the size of the unattributable spend, not the tag count:
        # a fully-untagged $50/mo cluster matters less than a $5k/mo one missing
        # only its cost centre.
        if monthly_cost >= float(t.get("high_priority_monthly_usd", 1000)):
            priority = RulePriority.HIGH
        elif monthly_cost >= float(t.get("medium_priority_monthly_usd", 250)):
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        tag_list = ", ".join(missing_names)
        why = (
            f"{resource_type.capitalize()} '{name}' billed ~${monthly_cost:,.0f}/mo but "
            f"is missing {missing} of {required} required ownership tags ({tag_list}), "
            f"so that spend cannot be charged back to a team, project or cost centre."
        )
        if identity:
            why += f" Billing records show it was run by {identity}."
        how = (
            f"Add the missing tag(s) — {tag_list} — to the {resource_type} "
            f"({_UI_PATH.get(resource_type, 'the resource settings')}). "
            "Tags apply to future usage only, so earlier spend stays unattributed. "
            "Consider a cluster policy or a tag-enforcement policy to prevent "
            "untagged resources being created."
        )
        if identity:
            how += f" If in doubt, ask {identity}, who ran it."

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=name,
            # The unattributable spend — the size of the governance gap.
            monthly_waste_usd=round(monthly_cost, 2),
            # $0 by design: tagging changes attribution, not the bill. See the
            # module docstring.
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
                missing_tags=missing_names,
                missing_tag_count=missing,
                required_tag_count=required,
                present_tags=sorted(
                    set(t.get("required_tags", {})) - set(missing_names)
                ),
                unattributable_monthly_usd=round(monthly_cost, 2),
                identity_hint=identity,
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
