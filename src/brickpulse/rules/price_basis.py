"""Contract-rate and cloud-VM figures, derived CENTRALLY from each candidate row.

Rules compute savings at LIST price (``system.billing.list_prices``) exactly as
before. This module derives the two extra views from per-resource columns the
shared SQL macros emit, so no rule author ever writes contract-pricing logic and
every existing rule picks it up for free:

* **Contract rates** (``system.billing.account_prices``). The macros emit two
  scale-invariant ratios per resource (see ``contract_cost_cols`` in
  ``sql/_macros.sql.j2``):

  - ``contract_ratio``          = Σ qty × contract rate / Σ qty × list rate, over
                                   the usage rows that HAVE a contract price;
  - ``contract_price_coverage`` = share of the resource's usage quantity that
                                   found a contract price.

  Ratios rather than dollar sums because many templates scale the cost CTE
  downstream (interactive_to_job multiplies by a job-activity share); a ratio
  survives that unchanged, a dollar sum would not.

  A contract figure is emitted only when coverage ≥ ``contract_min_coverage``
  (default 0.95). Below that the finding stays ``price_basis='list'`` — a partly
  contract-priced figure is exactly the "strange sum" the pricing design forbids.

* **Cloud VM cost** (``system.billing.cloud_infra_cost``). Classic compute only,
  shown as its own line and NEVER folded into the Databricks dollars.

Rule opt-ins (class attributes on :class:`~finops_rules.contract.Rule`):

* ``contract_pricing``: ``"ratio"`` (default — savings are a fraction of the
  resource's own spend, so they scale by the same discount), ``"counterpart"``
  (savings are a price GAP between two SKUs; the rule sets
  ``finding.monthly_savings_contract_usd`` itself in ``score()``, or leaves it
  None → list only), or ``"none"`` ($0 / advisory rules).
* ``cloud_savings``: ``"none"`` (default), ``"proportional"`` (the action removes
  running time/nodes in the same proportion as DBUs), or ``"full"`` (the action
  eliminates the classic VM entirely, e.g. a move to serverless).

Everything here is a no-op when the capability flags are off, so a deployment
without contract/cloud data produces byte-identical findings.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import PRICING_MODE_ACCOUNT, PRICING_MODE_LIST, to_monthly, validate_pricing_mode

PRICE_BASIS_LIST = "list"
PRICE_BASIS_CONTRACT = "contract"

CONTRACT_PRICING_MODES = ("ratio", "counterpart", "none")
CLOUD_SAVINGS_MODES = ("proportional", "full", "none")

#: Engine-wide default for the coverage gate (overridable as
#: ``contract_min_coverage`` in the top level of thresholds.yaml).
DEFAULT_CONTRACT_MIN_COVERAGE = 0.95

_CONTRACT_NOTE = (
    " Savings are shown at LIST price (system.billing.list_prices) and at your contract "
    "rates (system.billing.account_prices, preview)."
)


@dataclass(frozen=True)
class PricingCapabilities:
    """Which optional price sources are present for this run.

    Detected once per pipeline update / live request by :func:`probe_capabilities`
    and threaded into :class:`~finops_rules.contract.RuleContext`, which renders
    them into the SQL as the Jinja flags ``contract_prices`` / ``cloud_costs``."""

    contract_prices: bool = False
    cloud_costs: bool = False

    @classmethod
    def off(cls) -> "PricingCapabilities":
        return cls()


def capability_probe_sqls(catalog: str, currency: str, lookback_days: int) -> dict[str, str]:
    """One cheap existence query per optional source.

    Separate queries so one missing table cannot mask the other; a table that is
    absent, ungranted, or EMPTY all mean "not available" (unlike the sources.py
    preflight, emptiness matters here: an empty account_prices would gate every
    finding to list anyway, so switching the SQL over buys nothing)."""
    return {
        "contract_prices": (
            f"SELECT 1 FROM {catalog}.billing.account_prices "
            f"WHERE currency_code = '{currency}' "
            f"AND price_start_time < current_timestamp() "
            f"AND (price_end_time IS NULL OR price_end_time > date_sub(current_date(), {int(lookback_days)})) LIMIT 1"
        ),
        "cloud_costs": (
            f"SELECT 1 FROM {catalog}.billing.cloud_infra_cost "
            f"WHERE currency_code = '{currency}' "
            f"AND usage_date >= date_sub(current_date(), {int(lookback_days)}) LIMIT 1"
        ),
    }


def probe_capabilities(run_query, catalog: str, currency: str,
                       lookback_days: int, pricing_mode: str = "auto") -> PricingCapabilities:
    """Detect optional sources, honoring the configured contract-price mode.

    Auto degrades to list when the table is unavailable. Account fails closed so
    an inaccessible contract table cannot be mistaken for a valid list result.
    """
    mode = validate_pricing_mode(pricing_mode)
    found: dict[str, bool] = {}
    for key, sql in capability_probe_sqls(catalog, currency, lookback_days).items():
        if key == "contract_prices" and mode == PRICING_MODE_LIST:
            found[key] = False
            continue
        try:
            found[key] = len(list(run_query(sql))) > 0
        except Exception as exc:
            if key == "contract_prices" and mode == PRICING_MODE_ACCOUNT:
                raise RuntimeError(
                    f"pricing_mode='account' requires SELECT on {catalog}.billing.account_prices"
                ) from exc
            found[key] = False
    if mode == PRICING_MODE_ACCOUNT and not found["contract_prices"]:
        raise RuntimeError(
            f"pricing_mode='account' requires non-empty {catalog}.billing.account_prices "
            f"with {currency} rates covering the analysis window"
        )
    return PricingCapabilities(**found)


def _num(row: dict, key: str) -> float | None:
    val = row.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _scaled(value: float | None, ratio: float) -> float | None:
    return None if value is None else round(float(value) * ratio, 2)


def apply_price_basis(rule, row: dict, finding, ctx) -> None:
    """Fill the contract / cloud fields on ``finding`` (mutates it).

    Called by every backend right after ``rule.score`` returns a finding, before
    ``build_recommendation``. With both capability flags off it touches nothing."""
    if ctx.contract_prices:
        _apply_contract(rule, row, finding, ctx)
    if ctx.cloud_costs:
        _apply_cloud(rule, row, finding, ctx)


def _apply_contract(rule, row: dict, finding, ctx) -> None:
    mode = getattr(rule, "contract_pricing", "ratio")
    if mode == "none":
        return
    coverage = _num(row, "contract_price_coverage")
    if finding.contract_price_coverage is None:
        finding.contract_price_coverage = None if coverage is None else round(coverage, 4)
    coverage = finding.contract_price_coverage
    covered = coverage is not None and coverage >= ctx.contract_min_coverage

    if mode == "counterpart":
        # The rule priced both sides itself (or left it None). Enforce the gate.
        if not covered or finding.monthly_savings_contract_usd is None:
            _clear_contract(finding)
            return
    else:  # ratio
        ratio = _num(row, "contract_ratio")
        if not covered or ratio is None or ratio < 0:
            _clear_contract(finding)
            return
        point = finding.monthly_savings_usd
        low = finding.monthly_savings_low_usd if finding.monthly_savings_low_usd is not None else point
        high = finding.monthly_savings_high_usd if finding.monthly_savings_high_usd is not None else point
        finding.monthly_savings_contract_usd = _scaled(point, ratio)
        finding.monthly_savings_contract_low_usd = _scaled(low, ratio)
        finding.monthly_savings_contract_high_usd = _scaled(high, ratio)
        if isinstance(finding.evidence, dict):
            finding.evidence["effective_discount"] = round(1.0 - ratio, 4)

    # Counterpart rules may set only the point; bounds default to it.
    if finding.monthly_savings_contract_low_usd is None:
        finding.monthly_savings_contract_low_usd = finding.monthly_savings_contract_usd
    if finding.monthly_savings_contract_high_usd is None:
        finding.monthly_savings_contract_high_usd = finding.monthly_savings_contract_usd
    finding.price_basis = PRICE_BASIS_CONTRACT
    if isinstance(finding.evidence, dict) and finding.evidence.get("data_basis"):
        finding.evidence["data_basis"] = finding.evidence["data_basis"] + _CONTRACT_NOTE


def _clear_contract(finding) -> None:
    """Fall back to list. ``price_basis='list'`` marks a MEASURED fallback (the row
    carried a coverage figure that missed the gate); a row with no coverage column
    at all (e.g. a template that doesn't price) stays None — nothing was measured."""
    finding.monthly_savings_contract_usd = None
    finding.monthly_savings_contract_low_usd = None
    finding.monthly_savings_contract_high_usd = None
    finding.price_basis = PRICE_BASIS_LIST if finding.contract_price_coverage is not None else None


def _apply_cloud(rule, row: dict, finding, ctx) -> None:
    cloud_30d = _num(row, "cloud_cost_30d_usd")
    if cloud_30d is None:
        return
    finding.cloud_cost_30d_usd = round(cloud_30d, 2)
    mode = getattr(rule, "cloud_savings", "none")
    cloud_monthly = to_monthly(cloud_30d, ctx.lookback_days)
    if mode == "full":
        finding.monthly_cloud_savings_usd = round(cloud_monthly, 2)
    elif mode == "proportional":
        list_monthly = to_monthly(_num(row, "cost_30d_usd") or 0.0, ctx.lookback_days)
        if list_monthly > 0 and finding.monthly_savings_usd:
            share = min(max(finding.monthly_savings_usd / list_monthly, 0.0), 1.0)
            finding.monthly_cloud_savings_usd = round(cloud_monthly * share, 2)
