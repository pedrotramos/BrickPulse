"""The rule plugin contract — the extensibility heart of FinOpsApp.

Every cost-saving criterion is a subclass of :class:`Rule`. A rule is authored
ONCE and runs in BOTH execution modes:

* **live**  — its SQL is executed on a SQL warehouse on demand (dev).
* **precompute** — a scheduled job runs the same SQL account-wide and writes the
  findings to a Delta table that the app just reads (prod).

A rule contributes two things:

1. ``sql_template()`` — a Jinja2 SQL template that returns *candidate* rows
   (resources that *might* be wasteful) plus the raw metrics needed to score
   them. It must NOT filter by ``workspace_id`` — persona scoping is injected by
   the data layer so the same query serves account-wide precompute and
   workspace-scoped live queries.
2. ``score(row, ctx)`` — pure Python that turns one candidate row into a
   :class:`RuleFinding` (with dollar savings, effort, priority, and the human
   "why"/"how"), or returns ``None`` to suppress it. Returning ``None`` is the
   *investigation-guided* guard: never recommend an action that doesn't apply.

See ``docs/ADDING_A_RULE.md`` for the authoring guide.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2

if TYPE_CHECKING:  # resolver imports nothing from here, but keep the runtime import local
    from .change_tracking import TrackedSetting
    from .resolver import TableResolver

SQL_DIR = Path(__file__).parent / "sql"


class EffortBand(str, Enum):
    """How hard the fix is. Numeric weight (via :data:`EFFORT_SCORES`) feeds the
    Next-Best-Action ranking so effort is comparable across rules."""

    TRIVIAL = "trivial"  # a config toggle, no workload/restart impact
    LOW = "low"          # single-resource config change
    MEDIUM = "medium"    # needs validation / testing
    HIGH = "high"        # migration or code change


#: Effort band -> numeric score. Kept here as the fallback source of truth;
#: ``config/effort.yaml`` may override it at runtime (see config_loader).
EFFORT_SCORES: dict[str, int] = {
    EffortBand.TRIVIAL: 1,
    EffortBand.LOW: 2,
    EffortBand.MEDIUM: 3,
    EffortBand.HIGH: 5,
}


class RulePriority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


#: Engine-wide default for the minimum days of *observed activity* a resource must
#: accumulate before any rule is allowed to recommend an action on it. A cluster
#: that started yesterday afternoon looks statistically identical to a chronically
#: idle one, so acting on that evidence is sampling noise, not a cost finding.
#: Overridable via ``min_observation_days`` in config/thresholds.yaml.
#: See :mod:`finops_rules.history` for the guard itself.
DEFAULT_MIN_OBSERVATION_DAYS = 5


@dataclass
class RuleContext:
    """Everything a rule needs to render its SQL and score a row."""

    thresholds: dict          # this rule's block from config/thresholds.yaml
    lookback_days: int = 30
    catalog: str = "system"   # allows pointing at a mirror of the system schema
    currency: str = "USD"
    #: Engine-wide minimum days of observed activity before a finding may be
    #: emitted (see :mod:`finops_rules.history`). Engine-wide rather than per-rule
    #: because "too new to judge" is a property of the resource, not of the rule.
    min_observation_days: int = DEFAULT_MIN_OBSERVATION_DAYS
    #: Table-name resolver injected into the template as ``tbl('schema','table')``.
    #: Default None -> a resolver bound to ``catalog`` (``{catalog}.schema.table``).
    #: See :mod:`finops_rules.resolver`.
    resolver: TableResolver | None = None
    #: Optional price sources detected for this run (see finops_rules.price_basis).
    #: Rendered into the SQL as the Jinja flags ``contract_prices`` / ``cloud_costs``;
    #: both False reproduces the list-only SQL byte-for-byte.
    contract_prices: bool = False
    cloud_costs: bool = False
    #: Minimum share of a resource's usage that must carry a contract price before
    #: a contract figure is emitted for it (engine-wide ``contract_min_coverage``).
    contract_min_coverage: float = 0.95
    #: Engine-wide ``phasing:`` block of config/thresholds.yaml (``max_step_ratio``,
    #: ``observe_days``) for rules that lower capacity in steps. None -> the defaults in
    #: :mod:`finops_rules.phasing` (3x per step, 7 days between steps).
    phasing: dict | None = None

    def resolver_or_default(self) -> TableResolver:
        from .resolver import TableResolver
        return self.resolver or TableResolver.nested(self.catalog)


@dataclass
class RuleFinding:
    """One actionable finding. Maps 1:1 to a row of the recommendation table."""

    workspace_id: str
    resource_type: str        # "cluster" | "warehouse" | "job" | "serving_endpoint"
    resource_id: str
    resource_name: str
    monthly_waste_usd: float  # current wasteful spend on this resource
    monthly_savings_usd: float  # estimated $ saved if the action is applied
    effort: EffortBand
    priority: RulePriority
    why: str                  # WHY it wastes money (human explanation)
    how: str                  # HOW to fix it (investigation-guided guidance)
    evidence: dict = field(default_factory=dict)  # raw metrics that triggered it
    # Optional ownership, when a rule already reads it (e.g. idle_serving_endpoints
    # projects served_entities.created_by). Left None by most rules: the precompute
    # transform fills owner/creator centrally from billing.usage.identity_metadata
    # (the owner_map), so no rule has to join for it. A value set HERE wins over the
    # map — the rule's own source is more specific than the billing fallback.
    resource_owner: str | None = None
    resource_creator: str | None = None
    # --- Savings-status scaffold --
    # savings_status classifies how much trust the HEADLINE may place in this finding:
    #   bookable  — booked into the headline lower bound (only monthly_savings_low_usd)
    #   estimated — still shown and rankable, but contributes ZERO to the booked total
    #   advisory  — no dollars booked at all
    # None means "inherit the rule's default" (:attr:`Rule.default_savings_status`,
    # itself "bookable" today), so existing rules are unchanged until rules are reclassified
    # reclassifies them. A per-finding value overrides the rule default — e.g. a rule
    # whose zero-traffic tier is bookable but whose low-traffic tier is only estimated.
    savings_status: str | None = None
    # Conservative lower / upper bounds around ``monthly_savings_usd`` (the point
    # estimate). Both default to the point estimate when a rule does not set them, so
    # the invariant ``low <= point <= high`` holds trivially until a rule widens the
    # band. The headline books ONLY ``monthly_savings_low_usd``, and only for bookable
    # findings — the point/high figures never enter the booked total.
    monthly_savings_low_usd: float | None = None
    monthly_savings_high_usd: float | None = None
    # --- action-aware aggregation ---------------------------------------------
    # affected_spend_scope names the underlying spend POOL this action reclaims from, so
    # the headline never banks the same dollar twice. Findings that share a scope compete
    # for the SAME spend (deduped to one bankable figure); findings with different scopes
    # are additive across the portfolio. None means "this resource's own spend" — the
    # aggregator falls back to workspace|resource_type|resource_id, preserving the
    # historical per-resource behavior. Set it COARSER (e.g. the cluster a job runs on)
    # only when two findings genuinely bill the same hourly/SKU segment across resources.
    affected_spend_scope: str | None = None
    # savings_composition says how this finding combines with OTHERS in the same scope:
    #   exclusive — a mutually-exclusive whole-resource alternative (a migration, a
    #               delete, or the historical default): only the single best exclusive
    #               option books, and being an alternative to the tuning stack a larger
    #               migration SUPERSEDES the source tuning.
    #   additive  — a compatible tuning that COMPOSES with other additive options in the
    #               scope (e.g. auto-stop cuts hours while right-size cuts $/hour): applied
    #               highest-first to the remaining counterfactual, bounded by the pool.
    # None inherits the rule's default (:attr:`Rule.default_savings_composition`,
    # "exclusive"), so every existing rule keeps max-per-resource until it opts in.
    savings_composition: str | None = None
    # --- Contract / cloud pricing (finops_rules.price_basis) ---------------------------
    # Filled CENTRALLY by price_basis.apply_price_basis from the per-resource columns the
    # SQL macros emit — rules leave these None, except a ``contract_pricing =
    # "counterpart"`` rule, which sets monthly_savings_contract_usd itself in score().
    # All None when the account has no account_prices / cloud_infra_cost data.
    price_basis: str | None = None                      # list | contract
    contract_price_coverage: float | None = None        # share of usage with a contract price
    monthly_savings_contract_usd: float | None = None
    monthly_savings_contract_low_usd: float | None = None
    monthly_savings_contract_high_usd: float | None = None
    cloud_cost_30d_usd: float | None = None             # classic VM cost (cloud_infra_cost)
    monthly_cloud_savings_usd: float | None = None      # separate from the DBU savings


class Rule(ABC):
    """Base class for every cost-saving rule. Subclasses live one-per-file under
    ``catalog/`` and are auto-discovered by the registry — there is no central
    list to edit, so multiple authors never collide."""

    id: str = ""              # stable slug, e.g. "cluster_autotermination"
    version: str = "0.0.0"    # semver of the rule logic
    title: str = ""
    #: Not enforced as an enum, but the frontend switches on it (icon, deep link,
    #: facts strip) — see "Introducing a NEW resource_type" in docs/ADDING_A_RULE.md
    #: before using a value outside this set.
    resource_type: str = ""   # cluster | warehouse | job | serving_endpoint
    category: str = ""        # compute | sql | jobs | serving
    default_enabled: bool = True

    #: a NET-NEW opportunity rule that is still being validated. It is
    #: computed and STORED like any other rule, but RELOCATED out of the main
    #: recommendation surfaces into the "Labs" (Experimental) tab — so it never counts
    #: toward the headline total, never appears as an option on an optimizable resource,
    #: and never enters the Next Best Actions ranking or the savings leaderboard. This is
    #: the same relocation contract as ``sidecar_categories`` (governance), but keyed on
    #: the rule rather than a category, and self-declared here so adding an experimental
    #: rule needs no shared-list edit (see backend ``config.Settings.experimental_rule_ids``
    #: and the read-time partition in ``data/base.py``). Experimental rules should emit
    #: ``advisory`` findings until the plan promotes them: net-new economics are unproven,
    #: so they carry evidence and an addressable-spend figure, not booked savings.
    experimental: bool = False

    #: Savings-status default for every finding this rule emits, unless the
    #: finding sets its own (see :attr:`RuleFinding.savings_status`). Defaults to
    #: "bookable" so the headline is unchanged until specific rules
    #: are reclassified to "estimated" / "advisory".
    default_savings_status: str = "bookable"

    #: default for how this rule's findings combine with other findings on the
    #: same spend scope, unless a finding sets its own (see
    #: :attr:`RuleFinding.savings_composition`). "exclusive" reproduces the historical
    #: max-per-resource behavior; a rule whose action is a compatible tuning (right-size,
    #: auto-stop) overrides to "additive" so it composes rather than being max'd away.
    default_savings_composition: str = "exclusive"

    #: How the contract-rate figure is derived (see finops_rules.price_basis):
    #: "ratio" — savings are a fraction of this resource's own spend, so they scale by
    #: its effective discount (almost every rule); "counterpart" — savings are a price
    #: gap between two SKUs, so score() sets finding.monthly_savings_contract_usd itself;
    #: "none" — $0 / advisory rules.
    contract_pricing: str = "ratio"

    #: Whether the action also saves classic cloud-VM cost (cloud_infra_cost):
    #: "proportional" (removes running time/nodes in the same proportion as DBUs),
    #: "full" (eliminates the classic VM, e.g. a move to serverless) or "none".
    cloud_savings: str = "none"

    #: "Saved so far" change tracking (finops_rules.change_tracking): the setting this
    #: rule proposes to change, so the daily tracker can tell whether a flagged
    #: resource moved toward the proposal. None (the default) still gets deletion
    #: credit. Declared in the rule's own file; no shared list.
    tracked_setting: TrackedSetting | None = None

    #: Name of the Jinja2 template in ``sql/`` (defaults to ``<id>.sql.j2``).
    sql_file: str = ""

    def sql_template(self) -> str:
        """Return the *rendered-ready* Jinja2 template string for this rule.

        Default implementation loads ``sql/<sql_file or id>.sql.j2``. Override
        only if you build SQL dynamically."""
        name = self.sql_file or f"{self.id}.sql.j2"
        return _load_sql(name)

    def render_sql(self, ctx: RuleContext) -> str:
        """Render the SQL template with this rule's context + thresholds.

        Rendered through an environment with a filesystem loader so templates can
        ``{% import '_macros.sql.j2' as m %}`` — that shared macro file holds the
        canonical ``billing.list_prices`` join, which must stay identical across
        every rule (see the header of ``sql/_macros.sql.j2``)."""
        template = _sql_env().from_string(self.sql_template())
        return template.render(
            catalog=ctx.catalog,
            lookback_days=ctx.lookback_days,
            currency=ctx.currency,
            contract_prices=ctx.contract_prices,
            cloud_costs=ctx.cloud_costs,
            **ctx.resolver_or_default().render_context(),
            **ctx.thresholds,
        )

    @abstractmethod
    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        """Turn one candidate row into a finding, or ``None`` to suppress it."""
        raise NotImplementedError


@lru_cache(maxsize=1)
def _sql_env() -> jinja2.Environment:
    """Jinja environment for rule SQL: strict undefined (a typo'd threshold is an
    error, not a silently-empty predicate) plus a loader rooted at ``sql/`` so
    ``{% import %}`` of the shared macros resolves."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(SQL_DIR)),
        undefined=jinja2.StrictUndefined,
    )


@lru_cache(maxsize=64)
def _load_sql(name: str) -> str:
    path = SQL_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"SQL template not found: {path}")
    return path.read_text()
