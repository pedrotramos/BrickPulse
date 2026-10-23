"""The ``billing.list_prices`` join, for the backend's own (non-rule) SQL.

The rule engine gets this from ``rules/finops_rules/sql/_macros.sql.j2``; the
four hand-written backend queries (workspace spend, Genie costs, Apps inventory,
fleet facts) had their own copies of the same join and so carried the same two
defects. Both sides now render the same predicate, which matters because the
Total Spend KPI and a rule's ``monthly_waste_usd`` are read side by side — if
they priced usage differently, savings-as-a-share-of-spend would be quietly wrong.

See the header of ``sql/_macros.sql.j2`` for the full reasoning. In short:

* join on all four keys (``sku_name``, ``cloud``, ``usage_unit``,
  ``currency_code``) plus the validity window, not ``sku_name`` alone — anything
  less fans out in a multi-cloud or multi-currency account and inflates dollars;
* read ``pricing.effective_list.default``, which resolves promotional pricing,
  not ``pricing.default`` — measured on a real account as a 5.54% (~$37k/30d)
  overstatement.

One deliberate difference from the rule macro: these are **spend** queries, so an
unpriced row contributes $0 rather than NULL (:func:`price_expr` keeps the
COALESCE). A spend total must still account for the usage even when its SKU has
no list price — dropping it would understate the total, and a total that quietly
omits rows is worse than one that shows them at zero. Rules take the opposite
choice because they must never *overstate* a saving. Callers that want to report
the gap can add :func:`unpriced_dbus_expr`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brickpulse.rules.resolver import TableResolver


def price_join(
    resolver: "TableResolver", currency: str = "USD", *, alias: str = "lp", usage: str = "u"
) -> str:
    """The canonical ``LEFT JOIN`` onto ``list_prices``, fully keyed.

    Takes a :class:`~finops_rules.resolver.TableResolver` (``{catalog}.billing.list_prices``).
    This mirrors the rule macro (``sql/_macros.sql.j2``), so the Total Spend KPI and a
    rule's ``monthly_waste_usd`` keep pricing usage identically."""
    table = resolver("billing", "list_prices")
    return f"""LEFT JOIN {table} {alias}
    ON {usage}.sku_name = {alias}.sku_name
    AND {usage}.cloud = {alias}.cloud
    AND {usage}.usage_unit = {alias}.usage_unit
    AND {alias}.currency_code = '{currency}'
    AND {usage}.usage_end_time >= {alias}.price_start_time
    AND ({alias}.price_end_time IS NULL OR {usage}.usage_end_time < {alias}.price_end_time)"""


def price_expr(alias: str = "lp") -> str:
    """Per-DBU rate, 0 when the SKU has no list price (see module docstring)."""
    return f"COALESCE(CAST({alias}.pricing.effective_list.default AS DOUBLE), 0)"


def unpriced_dbus_expr(usage: str = "u", alias: str = "lp") -> str:
    """Usage quantity that found no price. Wrap in ``SUM()`` to report coverage."""
    return (
        f"CASE WHEN {alias}.sku_name IS NULL "
        f"THEN CAST({usage}.usage_quantity AS DOUBLE) ELSE 0 END"
    )


def contract_price_join(
    resolver: "TableResolver", currency: str = "USD", *, alias: str = "ap", usage: str = "u"
) -> str:
    """``LEFT JOIN`` onto ``billing.account_prices`` (contract rates), keyed exactly
    like :func:`price_join`. Only rendered when the capability probe found contract
    data (see ``finops_rules.price_basis``)."""
    table = resolver("billing", "account_prices")
    return f"""LEFT JOIN {table} {alias}
    ON {usage}.sku_name = {alias}.sku_name
    AND {usage}.cloud = {alias}.cloud
    AND {usage}.usage_unit = {alias}.usage_unit
    AND {alias}.currency_code = '{currency}'
    AND {usage}.usage_end_time >= {alias}.price_start_time
    AND ({alias}.price_end_time IS NULL OR {usage}.usage_end_time < {alias}.price_end_time)"""


def contract_rate_expr(alias: str = "ap") -> str:
    """Contract per-unit rate (``pricing.default``; account_prices has no
    effective_list). NULL when the SKU has no contract price."""
    return f"CAST({alias}.pricing.default AS DOUBLE)"
