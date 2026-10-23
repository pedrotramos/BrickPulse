"""Which system tables a rule needs — derived from its SQL, not hand-listed.

The engine reads 18 system tables across `billing`, `compute`, `lakeflow`,
`query`, `access` and `serving`. Several of those schemas are separately enabled
and separately granted, so in a real customer workspace some of them are simply
not readable by the app's identity. Before this module, that produced a silent
failure: the rule's query raised, ``live_source`` logged and returned ``[]``, and
the UI showed the rule as finding nothing. "No waste found" and "you never had
permission to look" rendered identically — the worst possible outcome for a tool
whose entire value is being trusted about money.

Dependencies are extracted from each rule's RENDERED SQL rather than declared in
a class attribute, because a declaration is a second source of truth that drifts:
someone adds a ``system.access.audit`` join to a template, forgets the attribute,
and the preflight cheerfully reports the rule as healthy. Parsing what the rule
actually asks the warehouse for cannot drift.

Extraction is deliberately shallow — a regex over ``FROM``/``JOIN`` clauses. It
never has to be perfect: a missed table degrades to today's behaviour (an
unexplained empty rule), and a spurious one shows an extra health row. Both beat
a confident lie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contract import Rule, RuleContext

#: ``FROM``/``JOIN <catalog>.<schema>.<table>``. The catalog is whatever
#: ``RuleContext.catalog`` rendered (``system``, or a mirror), so it is captured
#: rather than assumed.
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*\.[A-Za-z_][\w]*\.[A-Za-z_][\w]*)",
    re.IGNORECASE,
)

#: Rules cannot run at all without these. Everything else degrades: the rule that
#: needs it goes UNAVAILABLE while the rest of the app keeps working. Billing is
#: the floor because a FinOps tool with no cost data has nothing to say — and
#: because every rule joins it to price its findings.
CORE_TABLES = frozenset({"billing.usage", "billing.list_prices"})


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` line comments and ``/* */`` blocks.

    Rule templates carry long comment headers that name tables in prose (e.g.
    "joined to billing.usage x list_prices"); parsing those would invent
    dependencies that the SQL never references."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def qualified_tables(sql: str) -> set[str]:
    """Fully-qualified ``catalog.schema.table`` names the SQL reads."""
    return {m.group(1) for m in _TABLE_RE.finditer(_strip_sql_comments(sql))}


def short_name(qualified: str) -> str:
    """``system.billing.usage`` -> ``billing.usage``.

    Health is reported per schema.table because the catalog is deployment
    configuration, not a property of the dependency."""
    parts = qualified.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else qualified


def rule_dependencies(rule: Rule, ctx: RuleContext) -> set[str]:
    """Schema-qualified source tables ``rule`` reads under ``ctx``.

    Rendering can fail for a rule whose thresholds block is missing or malformed;
    that is a config bug rather than a permission problem, so it yields no
    dependencies and surfaces through the normal rule-execution error path."""
    try:
        sql = rule.render_sql(ctx)
    except Exception:
        return set()
    return {short_name(t) for t in qualified_tables(sql)}


@dataclass(frozen=True)
class TableHealth:
    """Availability of one source table, as observed by a probe."""

    name: str            # schema.table, e.g. "compute.node_timeline"
    available: bool
    required: bool       # in CORE_TABLES => the app cannot function without it
    message: str = ""    # the underlying error, when unavailable

    @property
    def status(self) -> str:
        """``PASS`` / ``FAIL`` / ``WARN``.

        A missing OPTIONAL table is a WARN, not a FAIL: the app still works and
        most rules still run. Only the core billing tables escalate to FAIL."""
        if self.available:
            return "PASS"
        return "FAIL" if self.required else "WARN"

    @property
    def severity(self) -> str:
        if self.available:
            return "INFO"
        return "CRITICAL" if self.required else "MEDIUM"


def probe_sql(table: str) -> str:
    """A cheap existence + read-permission probe.

    ``LIMIT 0`` still resolves the table and enforces grants but scans nothing,
    so probing 18 tables stays far cheaper than one rule query. It cannot detect
    an EMPTY table — deliberately: emptiness is a data-recency question, not an
    availability one, and conflating them would report a brand-new workspace's
    empty audit log as a permission failure."""
    return f"SELECT * FROM {table} LIMIT 0"


def unavailable_rules(
    dependencies: dict[str, set[str]],
    healthy: set[str],
) -> dict[str, set[str]]:
    """Map rule_id -> the missing tables blocking it.

    ``dependencies`` is rule_id -> required tables (from :func:`rule_dependencies`);
    ``healthy`` is the set that probed PASS. Rules with no missing dependency are
    omitted, so an all-healthy workspace yields an empty dict and the UI shows no
    warnings at all."""
    blocked: dict[str, set[str]] = {}
    for rule_id, needed in dependencies.items():
        missing = needed - healthy
        if missing:
            blocked[rule_id] = missing
    return blocked
