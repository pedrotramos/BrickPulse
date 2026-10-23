"""Small pricing / normalization helpers shared by rules.

Rules attribute real dollars to a resource by joining ``system.billing.usage``
to ``system.billing.list_prices`` inside their SQL (so the heavy lifting is
pushed to the warehouse). These helpers cover the light Python-side math:
turning a lookback-window cost into a monthly figure, and effort-band lookups.
"""

from __future__ import annotations

from .contract import EFFORT_SCORES, EffortBand

# A calendar-month normalization constant. We report savings per 30-day month.
DAYS_PER_MONTH = 30.0

# The list-price estimate is always retained as a comparable baseline. The mode
# controls whether contract-rate estimates are also calculated.
PRICING_MODE_LIST = "list"
PRICING_MODE_AUTO = "auto"
PRICING_MODE_ACCOUNT = "account"
PRICING_MODE_EFFECTIVE = "effective"

_LIST_PRICE_NOTE = (
    " Savings are estimated at LIST price (system.billing.list_prices); under a "
    "committed-discount contract the actual reclaimed amount is proportionally lower."
)


#: Retired env var. It used to name a net-cost source for a global ``effective``
#: mode; contract rates are now read PER FINDING from system.billing.account_prices
#: (see finops_rules.price_basis). Kept only so old imports resolve.
EFFECTIVE_PRICE_SOURCE_ENV = "FINOPS_EFFECTIVE_PRICE_SOURCE"


def validate_pricing_mode(pricing_mode: str | None, *, effective_source: str | None = None) -> str:
    """Normalize and validate the engine-wide pricing mode; fail fast on a misconfig.

    ``auto`` adds contract estimates when the table is available; ``list`` never
    reads it; ``account`` requires it. The retired ``effective`` mode cannot be
    used because it silently relabelled list-priced findings.

    Returns the normalized mode. Raises ``ValueError`` on anything else."""
    mode = (pricing_mode or PRICING_MODE_AUTO).strip().lower()
    if mode in (PRICING_MODE_AUTO, PRICING_MODE_LIST, PRICING_MODE_ACCOUNT):
        return mode
    if mode == PRICING_MODE_EFFECTIVE:
        raise ValueError(
            "pricing_mode='effective' is retired. Use 'auto', 'list', or 'account'; "
            "contract estimates remain separate from list-price estimates."
        )
    raise ValueError(
        f"Unknown pricing_mode {pricing_mode!r}. Valid: 'auto', 'list', 'account'."
    )


def pricing_mode_note(pricing_mode: str | None) -> str:
    """List-price disclaimer appended to a $-booking finding's data_basis. Applied
    only to findings WITHOUT a contract figure — contract-priced findings get the
    two-basis note from brickpulse.rules.price_basis instead (per finding, not global)."""
    return _LIST_PRICE_NOTE


def to_monthly(window_cost: float, lookback_days: int) -> float:
    """Scale a cost measured over ``lookback_days`` to a 30-day month."""
    if not window_cost or lookback_days <= 0:
        return 0.0
    return float(window_cost) * (DAYS_PER_MONTH / lookback_days)


def effort_score(effort: EffortBand, overrides: dict[str, int] | None = None) -> int:
    """Numeric effort weight, allowing config overrides from effort.yaml."""
    if overrides and effort.value in overrides:
        return int(overrides[effort.value])
    return EFFORT_SCORES[effort]
