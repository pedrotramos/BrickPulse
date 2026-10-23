"""Price-coverage reporting: say so when some usage could not be priced.

Every rule prices a resource by joining ``system.billing.usage`` to
``system.billing.list_prices``. A usage row whose SKU has no matching price
produces no dollars. The old templates wrapped the rate in ``COALESCE(..., 0)``,
which turned a pricing gap into a confident $0 — the cost silently shrank and
nothing anywhere recorded that it had happened.

The shared macro now leaves an unmatched rate NULL (so ``SUM`` skips it, leaving
the dollar total unchanged) and separately sums the unpriced ``usage_quantity``
into ``unpriced_dbus``. This module turns that number into something a reader can
act on.

Unpriced usage is not automatically a bug. ``GENIE_FREE_USAGE`` is free-tier and
carries no list price by design, so a non-zero ``unpriced_dbus`` is expected in
any account using Genie. What matters is the *share*: a resource whose spend is
mostly unpriced has a cost figure too incomplete to base a recommendation on.

Rules use it in two places::

    if underpriced(row):            # too little of the spend is priced to judge
        return None
    ...
    evidence=...(**coverage_evidence(row), data_basis=... + coverage_note(row))

Kept separate from :mod:`finops_rules.history` because the two guards answer
different questions: history asks "have we watched this resource long enough?",
coverage asks "do we know what it actually costs?".
"""

from __future__ import annotations

#: Minimum share of a resource's DBUs that must be priced before a rule may
#: emit a finding. At 0.80, up to a fifth of usage may be unpriced (free-tier
#: SKUs, a price list that lags a brand-new SKU) without blocking a
#: recommendation, but a resource we can barely price is suppressed rather than
#: reported at a fraction of its true cost.
MIN_PRICED_FRACTION = 0.80


def _f(row: dict, key: str) -> float:
    """Best-effort float read; treats missing/unparseable as 0.0."""
    try:
        value = row.get(key)
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def priced_fraction(
    row: dict, dbu_key: str = "dbus_30d", unpriced_key: str = "unpriced_dbus"
) -> float | None:
    """Share of this resource's DBUs that carried a list price (0.0-1.0).

    ``None`` when the row predates the coverage columns (an older precompute
    snapshot, or a template not yet migrated to the shared macro) — unknown
    coverage must not be read as bad coverage, or a schema lag would empty the
    app. Also ``None`` when total DBUs are 0: there is no usage to have priced,
    so the ratio is undefined rather than 0%.

    ``dbu_key`` / ``unpriced_key`` name the total/unpriced columns, so a rule that
    aggregates over its own window (idle_serving_endpoints emits
    ``dbus_window`` / ``unpriced_window``) can be measured with the same math.

    NOTE (per-unit coverage): the fraction is a DBU share, which is only
    meaningful for DBU-priced resources. A rule priced in tokens, storage-bytes,
    or network-bytes must not reuse this ratio; when such a rule becomes bookable
    it needs its own per-unit coverage. Every rule that books today is DBU-priced,
    so a single DBU fraction is correct for them."""
    if unpriced_key not in row:
        return None
    unpriced = _f(row, unpriced_key)
    total = _f(row, dbu_key)
    if total <= 0:
        # No DBU denominator on this row (not every rule selects the total). Fall
        # back to the only honest statement available: unpriced usage exists or
        # it doesn't.
        return None if unpriced > 0 else 1.0
    return max(0.0, min(1.0, (total - unpriced) / total))


def underpriced(row: dict, minimum: float = MIN_PRICED_FRACTION) -> bool:
    """True when too little of the resource's usage could be priced to support a
    recommendation. False when coverage is unknown (see :func:`priced_fraction`).

    This is the "should the finding be SHOWN at all" filter — it fails OPEN on
    unknown coverage on purpose, so a schema lag cannot empty the app. The
    separate, fail-CLOSED :func:`coverage_supports_booking` decides whether a
    shown finding may enter the BOOKED headline."""
    fraction = priced_fraction(row)
    if fraction is None:
        return False
    return fraction < minimum


def coverage_supports_booking(
    row: dict,
    minimum: float = MIN_PRICED_FRACTION,
    *,
    dbu_key: str = "dbus_30d",
    unpriced_key: str = "unpriced_dbus",
) -> bool:
    """may this finding's dollars enter the BOOKED headline?

    Fails CLOSED — booking requires coverage that is *known* and at least
    ``minimum``. Unknown coverage (the columns are absent) returns ``False``, so a
    schema omission or an un-migrated template downgrades a bookable finding to
    ``estimated`` (still shown, contributing $0) instead of booking a
    confident-looking partial dollar. This is the accounting invariant "absent
    price/telemetry is unknown, not zero" applied to the headline: a rule keeps
    the finding visible but must not book money it cannot price.

    Contrast :func:`underpriced`, which fails open and only governs visibility."""
    fraction = priced_fraction(row, dbu_key=dbu_key, unpriced_key=unpriced_key)
    if fraction is None:
        return False
    return fraction >= minimum


def coverage_note(row: dict) -> str:
    """One sentence for ``data_basis`` when some usage went unpriced, so the
    reader learns the cost is a floor rather than the whole story. Empty string
    when everything was priced — the common case should add no noise."""
    fraction = priced_fraction(row)
    if fraction is None or fraction >= 1.0:
        return ""
    return (
        f" Note: {(1.0 - fraction) * 100:.0f}% of this resource's DBUs had no list "
        f"price (e.g. free-tier SKUs) and contribute no cost, so the dollar figure "
        f"is a lower bound."
    )


def coverage_evidence(row: dict) -> dict:
    """Coverage keys for a finding's ``evidence`` dict. Emits nothing when all
    usage was priced, keeping the payload (and the UI's raw table) clean."""
    fraction = priced_fraction(row)
    if fraction is None or fraction >= 1.0:
        return {}
    return {
        "unpriced_dbus": round(_f(row, "unpriced_dbus"), 2),
        "priced_fraction": round(fraction, 4),
    }
