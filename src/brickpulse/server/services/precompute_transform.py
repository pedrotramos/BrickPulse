"""Pure, framework-agnostic transform: rule candidate rows -> contract records.

This is the single source of truth for the precompute logic. Both execution
backends call it and MUST NOT reimplement any of it:

* ``precompute/job.py``      — the legacy imperative Spark job (spark.sql + writer)
* ``precompute/pipeline.py`` — the Spark Declarative Pipeline (@dlt.table per rule
                               + a mapInPandas scoring sink)

It lives inside the ``finops`` package (not ``precompute/``) on purpose: the SDP
sink runs the scoring inside a ``mapInPandas`` UDF on the EXECUTORS, which only
have the installed wheel on their path. A module sitting next to ``pipeline.py``
would import fine on the driver but raise ``ModuleNotFoundError`` when cloudpickle
deserializes the closure on an executor.

Because both backends share this function verbatim, "did the SDP rewrite change
the logic?" reduces to "does this function still produce the same records?" —
which ``tests/test_sdp_parity.py`` pins against a golden snapshot.

The transform is deliberately free of Spark: it takes plain dict rows (already
``.collect()``-ed / ``fetchall()``-ed) and returns plain dict records shaped
exactly like the ``finops_recommendations`` contract. ``computed_at`` is NOT set
here — it is the one non-deterministic column and is stamped by the backend at
write time (see ``stamp_computed_at``), so equivalence tests can normalize it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from brickpulse.rules import DEFAULT_MIN_OBSERVATION_DAYS, RuleContext
from brickpulse.rules.price_basis import (
    DEFAULT_CONTRACT_MIN_COVERAGE,
    PRICE_BASIS_CONTRACT,
    PricingCapabilities,
    apply_price_basis,
)
from brickpulse.rules.pricing import PRICING_MODE_LIST, pricing_mode_note, validate_pricing_mode

from brickpulse.server.services.nba_ranker import build_recommendation


def _json_safe(value) -> str:
    """Serialize an evidence blob to JSON, coercing Spark Decimals to float.

    Identical semantics to the legacy job's inline helper — kept here so both
    backends serialize evidence the same way."""
    import json
    from decimal import Decimal

    def _default(o):
        if isinstance(o, Decimal):
            return float(o)
        return str(o)

    return json.dumps(value or {}, default=_default)


def records_for_rule(
    rule,
    rows: list[dict],
    *,
    thresholds: dict,
    lookback_days: int,
    catalog: str,
    snapshot: date,
    effort_overrides: dict | None = None,
    ws_map: dict[str, tuple] | None = None,
    owner_map: dict[tuple[str, str], tuple] | None = None,
    currency: str | None = None,
    capabilities: PricingCapabilities | None = None,
) -> list[dict]:
    """Score one rule's candidate rows into contract records (no ``computed_at``).

    Mirrors the legacy job's inner loop exactly: build the per-rule
    ``RuleContext``, run ``rule.score`` (which suppresses non-applicable rows by
    returning ``None``), then ``build_recommendation`` for NBA/effort/ids, then
    serialize ``evidence`` to a JSON string. Row and record shapes are unchanged.

    ``owner_map`` maps ``(workspace_id, resource_id)`` -> ``(owner, creator)``,
    resolved once from ``billing.usage.identity_metadata`` by the backend (the same
    way ``ws_map`` resolves workspace names). It is optional and defaults empty, so
    a caller that cannot read identity — or the parity fixtures — simply gets NULL
    owners rather than a different code path. Absent owners never suppress a finding.

    ``capabilities`` says which optional price sources (contract rates, cloud VM
    cost) the SQL was rendered with — it MUST be the same value the caller rendered
    the rule's SQL with, since the contract/cloud columns only exist in the rows
    when the flags were on. Default: both off (list only, the historical output).
    """
    effort_overrides = effort_overrides or {}
    ws_map = ws_map or {}
    owner_map = owner_map or {}
    # Engine-wide pricing mode. In LIST mode we append a disclaimer
    # to every $-booking finding so the reader knows savings are at list price, not
    # the customer's contract rate. Applied centrally here so all rules inherit it
    # without each restating it.
    # Fail-fast: refuse to score in 'effective' mode with no net-cost source
    # rather than relabel list price as effective. This is the chokepoint every backend
    # (legacy job, SDP pipeline, live) funnels through, so the guard covers them all.
    pricing_mode = validate_pricing_mode(thresholds.get("pricing_mode", PRICING_MODE_LIST))
    price_note = pricing_mode_note(pricing_mode)
    ctx_kwargs = dict(
        thresholds=thresholds.get(rule.id, {}),
        phasing=thresholds.get("phasing"),  # engine-wide phased-downsizing block
        lookback_days=lookback_days,
        catalog=catalog,
        # Engine-wide (top-level) setting, not part of the rule's own block: every
        # rule inherits the same "too new to judge" bar. See finops_rules.history.
        min_observation_days=int(
            thresholds.get("min_observation_days", DEFAULT_MIN_OBSERVATION_DAYS)
        ),
    )
    if currency is not None:
        ctx_kwargs["currency"] = currency
    caps = capabilities or PricingCapabilities.off()
    ctx_kwargs.update(
        contract_prices=caps.contract_prices,
        cloud_costs=caps.cloud_costs,
        contract_min_coverage=float(
            thresholds.get("contract_min_coverage", DEFAULT_CONTRACT_MIN_COVERAGE)
        ),
    )
    ctx = RuleContext(**ctx_kwargs)

    out: list[dict] = []
    for row in rows:
        finding = rule.score(row, ctx)
        if finding is None:
            continue
        # Contract-rate + cloud-VM figures, derived centrally (no-op with flags off).
        apply_price_basis(rule, row, finding, ctx)
        # Disclaim list-price savings on findings that actually book dollars ($0
        # rules like untagged_spend / scale-out carry no dollar figure to disclaim).
        # A contract-priced finding carries its own two-basis note instead.
        if (price_note and finding.monthly_savings_usd and isinstance(finding.evidence, dict)
                and finding.price_basis != PRICE_BASIS_CONTRACT):
            finding.evidence["pricing_mode"] = pricing_mode
            if finding.evidence.get("data_basis"):
                finding.evidence["data_basis"] = finding.evidence["data_basis"] + price_note
        ws_name, ws_url = ws_map.get(str(finding.workspace_id), (None, None))
        owner, creator = owner_map.get(
            (str(finding.workspace_id), str(finding.resource_id)), (None, None)
        )
        rec = build_recommendation(
            rule,
            finding,
            snapshot=snapshot,
            effort_overrides=effort_overrides,
            workspace_name=ws_name,
            workspace_url=ws_url,
            resource_owner=owner,
            resource_creator=creator,
        )
        payload = rec.model_dump()
        payload["evidence"] = _json_safe(payload["evidence"])
        # computed_at intentionally omitted here; the backend stamps it.
        payload.pop("computed_at", None)
        out.append(payload)
    return out


def stamp_computed_at(records: list[dict], now: datetime | None = None) -> list[dict]:
    """Attach the non-deterministic ``computed_at`` column at write time.

    Kept separate from :func:`records_for_rule` so equivalence tests can compare
    everything *except* this timestamp deterministically."""
    ts = now or datetime.now(timezone.utc)
    for rec in records:
        rec["computed_at"] = ts
    return records
