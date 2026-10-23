"""The ``finops_recommendations`` contract schema, as code.

The SDP pipeline needs the contract's column names and types *before* it can
declare its own output table: ``mapInPandas`` requires an explicit schema, and
the Arrow bridge needs to know which columns are DECIMAL so it can hand Spark
real ``Decimal`` objects (see ``writer.py`` for the same coercion on the legacy
path).

Previously the pipeline read that schema off an existing Delta table named by the
``finops.target_table`` pipeline conf, which pointed at
``finops_recommendations_golden`` — a table no part of this repo creates. On a
fresh workspace the pipeline therefore failed at import time with
``TABLE_OR_VIEW_NOT_FOUND`` before declaring a single table. Borrowing the shape
of the thing you are about to define is also circular: the pipeline OWNS
``finops_recommendations`` under SDP, so it cannot depend on that table already
existing.

Kept as plain ``(name, sql_type)`` pairs rather than a ``pyspark`` ``StructType``
so this module imports with no Spark on the path — the backend app imports it too
(it runs in a plain uvicorn container), and ``mapInPandas`` accepts a DDL string
just as happily as a ``StructType``.

Must stay in sync (additively only) with the other three faces of this contract:
  * ``precompute/ddl/finops_recommendations.sql``  (the Delta table)
  * ``backend/finops/models/recommendation.py``    (pydantic)
  * ``frontend/src/api/types.ts``                  (TypeScript)
``precompute/tests/test_contract_schema.py`` pins this module against the DDL, so
a column added to one and not the other fails the build rather than silently
dropping a field at write time.
"""

from __future__ import annotations

#: The contract columns, in DDL order. Order matters: the scoring UDF builds its
#: pandas DataFrame with ``columns=CONTRACT_COLUMNS`` so the Arrow batch lines up
#: positionally with the declared schema.
CONTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("recommendation_id", "string"),
    ("snapshot_date", "date"),
    ("rule_id", "string"),
    ("rule_version", "string"),
    ("rule_title", "string"),
    ("category", "string"),
    ("workspace_id", "string"),
    ("workspace_name", "string"),
    ("workspace_url", "string"),
    ("resource_type", "string"),
    ("resource_id", "string"),
    ("resource_name", "string"),
    # Who owns / created the resource, resolved from billing.usage.identity_metadata
    # (see precompute owner_map). Both nullable: several resource types carry no
    # identity on their usage rows, and a missing owner must never suppress a
    # finding — it just means "owner unknown", which the UI renders as such.
    ("resource_owner", "string"),
    ("resource_creator", "string"),
    ("why", "string"),
    ("how", "string"),
    ("monthly_waste_usd", "decimal(12,2)"),
    ("monthly_savings_usd", "decimal(12,2)"),
    # Savings-status scaffold.
    # low/high bound the point estimate; savings_status is bookable|estimated|advisory.
    # The headline books only monthly_savings_low_usd of bookable findings.
    ("monthly_savings_low_usd", "decimal(12,2)"),
    ("monthly_savings_high_usd", "decimal(12,2)"),
    ("savings_status", "string"),
    # action-aware aggregation. affected_spend_scope dedups findings that
    # bill the same spend pool; savings_composition (exclusive|additive) says whether an
    # option is a mutually-exclusive alternative or composes with other tunings.
    ("affected_spend_scope", "string"),
    ("savings_composition", "string"),
    ("effort_band", "string"),
    ("effort_score", "int"),
    ("priority", "string"),
    ("nba_score", "double"),
    ("evidence", "string"),
    ("computed_at", "timestamp"),
    # Contract rates + cloud VM cost (finops_rules.price_basis). Appended AFTER
    # computed_at so the pre-existing column positions are unchanged; all nullable
    # (NULL when the account has no account_prices / cloud_infra_cost data).
    ("price_basis", "string"),
    ("contract_price_coverage", "double"),
    ("monthly_savings_contract_usd", "decimal(12,2)"),
    ("monthly_savings_contract_low_usd", "decimal(12,2)"),
    ("monthly_savings_contract_high_usd", "decimal(12,2)"),
    ("cloud_cost_30d_usd", "decimal(12,2)"),
    ("monthly_cloud_savings_usd", "decimal(12,2)"),
    # Snapshot-independent finding identifier (a link from yesterday still resolves).
    ("finding_key", "string"),
)

#: Column names in contract order.
CONTRACT_COLUMNS: tuple[str, ...] = tuple(name for name, _ in CONTRACT_FIELDS)

#: Columns Spark types as DECIMAL. The Arrow bridge will not coerce a Python float
#: into a Spark decimal, so these must be handed real ``Decimal`` objects.
DECIMAL_COLUMNS: frozenset[str] = frozenset(
    name for name, typ in CONTRACT_FIELDS if typ.startswith("decimal")
)


def contract_ddl() -> str:
    """The contract as a Spark DDL string, e.g. ``"a string, b decimal(12,2)"``.

    Accepted anywhere a ``StructType`` is (``applyInPandas``, ``createDataFrame``)
    without importing ``pyspark`` here.
    """
    return ", ".join(f"{name} {typ}" for name, typ in CONTRACT_FIELDS)


def coerce_record(rec: dict, columns=CONTRACT_COLUMNS) -> dict:
    """Shape one scored record for the Arrow bridge: DECIMALs as ``Decimal``.

    Rounds to 2dp exactly as ``precompute/writer.py`` does on the legacy path, so
    both backends persist identical values rather than differing in the last cent.
    """
    from decimal import Decimal

    out = {}
    for col in columns:
        val = rec.get(col)
        if val is not None and col in DECIMAL_COLUMNS and not isinstance(val, Decimal):
            val = Decimal(str(round(float(val), 2)))
        out[col] = val
    return out
