"""In-memory roll-ups for the account overview and workspace drill-down.

Operates on a list of :class:`Recommendation` (whatever the active source
returned, already scoped). Fine for a PoC; a later optimization can push these
aggregations into SQL for very large accounts.

Overlapping fixes on ONE resource (e.g. a warehouse that could be auto-stopped
OR moved to serverless OR right-sized) must NOT be summed — you can only bank
one. So every dollar total here is computed over *resources*, each contributing
its single best option. This is the same collapse the grouped overview does; see
``services.recommendation_service._build_group``. Counts and breakdowns follow
the same one-per-resource basis so they reconcile with the headline totals.

Savings-status scaffold:
the SAVINGS headline is a *booked lower bound* — per spend scope, the bookable
``monthly_savings_low_usd`` its options support; ``estimated`` / ``advisory``
options contribute exactly zero. Today every rule defaults to ``bookable`` with
``low == point``, so the booked total equals the old max-per-resource savings sum
(i.e. this change is inert). It only drops once specific rules are reclassified as
rules. Waste, counts, and the "top opportunity" labels stay on the point estimate
— they describe the opportunity surface, not the booked floor, and after 0.3 the
per-category breakdown may exceed the booked headline by design (the difference is
the estimated/advisory opportunity).

action-aware aggregation: the savings total is booked per SPEND
SCOPE, not naively per resource, so the same dollar is never banked twice. Every
finding names the spend pool it reclaims from via ``affected_spend_scope`` (default:
the resource's own identity). Within a scope, options combine by ``savings_composition``:
``additive`` tunings compose onto the remaining counterfactual (auto-stop cuts hours
while right-size cuts $/hour — a *combined* figure, bounded by the pool), while
``exclusive`` options are mutually-exclusive whole-resource alternatives (only the
best books, so a bigger migration supersedes the source tuning). See :func:`book_scope`.
With everything defaulting to ``exclusive`` + a per-resource scope this reduces to the
historical max-per-resource behavior. The naive "sum of all options" survives only as
the labeled, non-booked ``options_savings_sum_usd`` on each group.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from ..models import (
    AccountOverview,
    CategoryBreakdown,
    PricedTotals,
    Recommendation,
    SavingsSlice,
    SavingsTier,
    WorkspaceDetail,
    WorkspaceSummary,
)


def _collapse_to_resources(recs: list[Recommendation]) -> list[Recommendation]:
    """One recommendation per (workspace, resource_type, resource_id): its
    single best (max-savings) option. Overlapping options on the same resource
    are mutually exclusive, so this is what's actually bankable and what the
    grouped overview shows as the resource's headline."""
    best: dict[tuple[str, str, str], Recommendation] = {}
    for r in recs:
        key = (r.workspace_id, r.resource_type, r.resource_id)
        cur = best.get(key)
        # Ties (typically several $0 advisory options) keep the one with the most spend
        # behind it, so the representative row doesn't depend on read order.
        if cur is None or (r.monthly_savings_usd, r.monthly_waste_usd) > (
            cur.monthly_savings_usd, cur.monthly_waste_usd
        ):
            best[key] = r
    return list(best.values())


def _scope_key(r: Recommendation) -> tuple[str, str]:
    """The spend pool a finding reclaims from. Findings that share a scope
    compete for the SAME dollars and are deduped to one bankable figure; findings with
    different scopes are additive across the portfolio. ``affected_spend_scope`` lets a
    rule name a pool shared across resources (a job billing on a cluster); absent it, the
    scope is the resource's own identity — the historical per-resource basis. Keyed with
    ``workspace_id`` so an explicit scope string never bleeds across workspaces."""
    scope = getattr(r, "affected_spend_scope", None)
    if scope:
        return (r.workspace_id, scope)
    return (r.workspace_id, f"{r.resource_type}|{r.resource_id}")


#: Public name for the spend-pool key, shared with the "Saved so far" tracker
#: (precompute/change_tracker.py), which books realized changes per scope per day with
#: the same :func:`book_scope` dedup as the headline. Both functions are duck-typed:
#: they read only ``workspace_id``, ``affected_spend_scope``, ``resource_type``,
#: ``resource_id``, ``monthly_waste_usd`` and ``savings_composition``.
scope_key = _scope_key


def _is_additive(r: Recommendation) -> bool:
    return getattr(r, "savings_composition", "exclusive") == "additive"


def book_scope(recs: list[Recommendation], *, value, eligible=lambda r: True) -> float:
    """Action-aware savings for the options on ONE spend scope.

    * ``additive`` (compatible) options *compose*: sorted highest-first and applied to
      the REMAINING counterfactual, so two tunings on one resource combine into a bigger
      figure but can never together exceed the pool (auto-stop cuts idle hours, right-size
      cuts $/hour — genuinely stackable).
    * every other option is a mutually-exclusive whole-resource alternative (a migration,
      a delete, or the historical default): only the single best one books, and being an
      alternative to the additive stack a larger migration SUPERSEDES the source tuning.

    The scope books the better of the two paths, bounded by the scope's corrected cost
    (the largest waste among its findings — the pool can't give back more than it costs).
    ``value`` selects the metric (booked ``low`` vs. addressable ``point``); ``eligible``
    filters by status (bookable-only for the booked headline)."""
    cost = max((float(r.monthly_waste_usd or 0.0) for r in recs), default=0.0)
    additive = sorted(
        (value(r) for r in recs if eligible(r) and _is_additive(r)), reverse=True
    )
    remaining, stacked = cost, 0.0
    for v in additive:
        take = min(v, remaining)
        stacked += take
        remaining -= take
    exclusive = max(
        (value(r) for r in recs if eligible(r) and not _is_additive(r)), default=0.0
    )
    return min(max(stacked, exclusive), cost)


def _sum_by_scope(recs: list[Recommendation], *, value, eligible) -> float:
    """Book each spend scope once (:func:`book_scope`) and sum across scopes."""
    by_scope: dict[tuple[str, str], list[Recommendation]] = defaultdict(list)
    for r in recs:
        by_scope[_scope_key(r)].append(r)
    return round(
        sum(book_scope(rs, value=value, eligible=eligible) for rs in by_scope.values()), 2
    )


def waste_total(recs: list[Recommendation]) -> float:
    """Spend behind the findings, each spend pool counted ONCE: the largest
    ``monthly_waste_usd`` per scope (:func:`_scope_key`), summed across scopes. Several
    rules on one warehouse — or an alert / dashboard whose dollars are that warehouse's
    compute (``affected_spend_scope``) — describe the same spend; summing them would
    count it once per rule."""
    by_scope: dict[tuple[str, str], float] = defaultdict(float)
    for r in recs:
        k = _scope_key(r)
        by_scope[k] = max(by_scope[k], float(r.monthly_waste_usd or 0.0))
    return round(sum(by_scope.values()), 2)


def resource_count(recs: list[Recommendation]) -> int:
    """Distinct resources with at least one finding — what a findings count shows, so
    three rules on one warehouse count as one thing to look at."""
    return len({(r.workspace_id, r.resource_type, r.resource_id) for r in recs})


# Confidence tiers, worst→best, so the breakdown always renders in a stable order and a
# reader sees the least-certain money last. "booked" is its own tier (a bookable finding
# is banked regardless of its data-confidence label); estimated findings fall into the
# high/medium/low tier their evidence carries.
SAVINGS_TIER_ORDER = ("booked", "high", "medium", "low")


def _savings_tier(r: Recommendation) -> str:
    """Which headline tier this finding's savings belong to: ``booked`` for a bookable
    finding, else the ``evidence.confidence`` tier of an estimated finding (defaulting to
    ``low`` when a rule left it unset — unknown certainty is treated as least certain)."""
    if r.savings_status == "bookable":
        return "booked"
    conf = (r.evidence or {}).get("confidence")
    return conf if conf in ("high", "medium", "low") else "low"


def _book_scope_attribution(
    recs: list[Recommendation], *, value, eligible
) -> list[tuple[Recommendation, float]]:
    """The finding(s) :func:`book_scope` actually books on ONE spend scope, each with the
    amount credited to it, so any attribution sums EXACTLY to ``book_scope``.

    Composition is identical: additive options stack (each option's actually-taken amount
    is credited to that option), otherwise the single best exclusive option wins and the
    whole (cost-bounded) amount is credited to it."""
    cost = max((float(r.monthly_waste_usd or 0.0) for r in recs), default=0.0)
    credited: list[tuple[Recommendation, float]] = []
    remaining, stacked = cost, 0.0
    for r in sorted(
        (r for r in recs if eligible(r) and _is_additive(r)), key=value, reverse=True
    ):
        take = min(value(r), remaining)
        if take > 0:
            credited.append((r, take))
            stacked += take
            remaining -= take
    best = max(
        (r for r in recs if eligible(r) and not _is_additive(r)), key=value, default=None
    )
    if best is not None and value(best) > stacked:
        # The exclusive alternative supersedes the additive stack; credit it alone.
        return [(best, min(value(best), cost))]
    return credited


def book_scope_breakdown(
    recs: list[Recommendation], *, value, eligible, key=_savings_tier
) -> dict[str, float]:
    """Like :func:`book_scope`, but returns the composed total ATTRIBUTED by ``key``
    (the trust tier by default; a rule or category for the overview breakdowns), so the
    parts sum EXACTLY to ``book_scope`` for the same scope. This is what lets the account
    headline show "Booked + High + Medium + Low = the estimate" without double counting
    a dollar two options both claim."""
    alloc: dict[str, float] = defaultdict(float)
    for r, amount in _book_scope_attribution(recs, value=value, eligible=eligible):
        alloc[key(r)] += amount
    return dict(alloc)


def _savings_tier_breakdown(recs: list[Recommendation]) -> dict[str, float]:
    """The account/workspace headline estimate split by tier, composed per spend scope so
    the tiers sum to :func:`_addressable_savings_total`. Point estimate, non-advisory."""
    by_scope: dict[tuple[str, str], list[Recommendation]] = defaultdict(list)
    for r in recs:
        by_scope[_scope_key(r)].append(r)
    agg: dict[str, float] = defaultdict(float)
    for rs in by_scope.values():
        for tier, amt in book_scope_breakdown(
            rs,
            value=lambda r: float(r.monthly_savings_usd or 0.0),
            eligible=lambda r: r.savings_status != "advisory",
        ).items():
            agg[tier] += amt
    return {t: round(agg[t], 2) for t in SAVINGS_TIER_ORDER if agg.get(t, 0.0) > 0.005}


def _savings_breakdown(recs: list[Recommendation], key) -> list[SavingsSlice]:
    """The headline estimate split by ``key`` (rule or category), composed per spend
    scope exactly like :func:`_savings_tier_breakdown`, so the slices sum to
    :func:`_addressable_savings_total`. ``resource_count`` is the distinct resources whose
    finding was credited. Point estimate, non-advisory, largest slice first."""
    by_scope: dict[tuple[str, str], list[Recommendation]] = defaultdict(list)
    for r in recs:
        by_scope[_scope_key(r)].append(r)
    amounts: dict[str, float] = defaultdict(float)
    resources: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for rs in by_scope.values():
        for r, amount in _book_scope_attribution(
            rs,
            value=lambda r: float(r.monthly_savings_usd or 0.0),
            eligible=lambda r: r.savings_status != "advisory",
        ):
            amounts[key(r)] += amount
            resources[key(r)].add((r.workspace_id, r.resource_type, r.resource_id))
    return [
        SavingsSlice(key=k, monthly_savings_usd=round(v, 2), resource_count=len(resources[k]))
        for k, v in sorted(amounts.items(), key=lambda kv: kv[1], reverse=True)
        if v > 0.005
    ]


def _savings_range(recs: list[Recommendation]) -> tuple[float, float]:
    """The (low, high) band around the non-advisory headline estimate: the composed
    conservative low and upper-scenario high, summed per spend scope. The estimate point
    (``_addressable_savings_total``) sits inside this band."""
    low = _sum_by_scope(
        recs,
        value=lambda r: float(r.monthly_savings_low_usd or 0.0),
        eligible=lambda r: r.savings_status != "advisory",
    )
    high = _sum_by_scope(
        recs,
        value=lambda r: float(r.monthly_savings_high_usd or 0.0),
        eligible=lambda r: r.savings_status != "advisory",
    )
    return low, high


def _booked_savings_total(recs: list[Recommendation]) -> float:
    """The booked headline lower bound: per spend scope, the bookable ``low`` its options
    support (additive tunings composed, else the best exclusive alternative), bounded by
    the scope cost; estimated/advisory options contribute zero. Summed across scopes.

    Computed from the RAW (un-collapsed) recs on purpose: a resource's best *point* option
    may be ``estimated`` while a smaller ``bookable`` one exists, and the floor must credit
    the bookable option rather than zero. With everything defaulting to ``bookable``,
    ``exclusive`` and ``low == point`` this equals the old max-per-resource savings sum, so
    the change is inert until rules opt into estimated/advisory or additive."""
    return _sum_by_scope(
        recs,
        value=lambda r: float(r.monthly_savings_low_usd or 0.0),
        eligible=lambda r: r.savings_status == "bookable",
    )


def _addressable_savings_total(recs: list[Recommendation]) -> float:
    """The fuller, ESTIMATED opportunity: per spend scope, its non-advisory options'
    composed point estimate (bookable or estimated), summed across scopes. The booked
    total is the conservative floor of this; advisory findings (no dollars) are excluded.
    Surfaced next to the booked headline so the estimate is visible but never booked."""
    return _sum_by_scope(
        recs,
        value=lambda r: float(r.monthly_savings_usd or 0.0),
        eligible=lambda r: r.savings_status != "advisory",
    )


def _contract_or_list(r: Recommendation, which: str) -> float:
    """A finding's figure at "Your rates": its contract figure when it has one
    (price_basis='contract'), else its list figure. ``which`` = point | low | high."""
    if r.price_basis == "contract" and r.monthly_savings_contract_usd is not None:
        v = {"point": r.monthly_savings_contract_usd,
             "low": r.monthly_savings_contract_low_usd,
             "high": r.monthly_savings_contract_high_usd}[which]
        if v is not None:
            return float(v)
    return float({"point": r.monthly_savings_usd,
                  "low": r.monthly_savings_low_usd,
                  "high": r.monthly_savings_high_usd}[which] or 0.0)


def priced_totals(recs: list[Recommendation]) -> PricedTotals | None:
    """The contract-basis (blended) headline + cloud-VM layer, or None when the recs
    carry no contract/cloud data. Composed per spend scope exactly like the list
    headline, so a resource's overlapping options are still counted once."""
    contract_available = any(r.price_basis for r in recs)
    cloud_available = any(r.cloud_cost_30d_usd is not None for r in recs)
    if not (contract_available or cloud_available):
        return None
    non_adv = lambda r: r.savings_status != "advisory"  # noqa: E731
    out = PricedTotals(contract_available=contract_available, cloud_available=cloud_available)
    if contract_available:
        out.monthly_savings_usd = _sum_by_scope(
            recs, value=lambda r: _contract_or_list(r, "low"),
            eligible=lambda r: r.savings_status == "bookable")
        out.monthly_savings_addressable_usd = _sum_by_scope(
            recs, value=lambda r: _contract_or_list(r, "point"), eligible=non_adv)
        out.monthly_savings_low_usd = _sum_by_scope(
            recs, value=lambda r: _contract_or_list(r, "low"), eligible=non_adv)
        out.monthly_savings_high_usd = _sum_by_scope(
            recs, value=lambda r: _contract_or_list(r, "high"), eligible=non_adv)
        # The still-at-list share of the estimate; the rest is at contract rates.
        list_part = _sum_by_scope(
            recs, value=lambda r: float(r.monthly_savings_usd or 0.0),
            eligible=lambda r: non_adv(r) and r.price_basis != "contract")
        out.list_part_usd = round(min(list_part, out.monthly_savings_addressable_usd), 2)
        out.contract_part_usd = round(out.monthly_savings_addressable_usd - out.list_part_usd, 2)
        dollar = [r for r in recs if non_adv(r) and r.monthly_savings_usd > 0]
        out.contract_findings = sum(1 for r in dollar if r.price_basis == "contract")
        out.list_findings = len(dollar) - out.contract_findings
    if cloud_available:
        out.monthly_cloud_savings_usd = _sum_by_scope(
            recs, value=lambda r: float(r.monthly_cloud_savings_usd or 0.0),
            eligible=lambda r: r.monthly_cloud_savings_usd is not None)
        out.cloud_findings = sum(1 for r in recs if r.cloud_cost_30d_usd is not None)
        out.classic_findings = sum(
            1 for r in recs
            if r.cloud_cost_30d_usd is not None or r.monthly_cloud_savings_usd is not None
            or r.rule_id in _CLOUD_SAVING_RULES())
    return out


def _CLOUD_SAVING_RULES() -> set[str]:
    """Rules whose action can save cloud VM cost (Rule.cloud_savings != 'none')."""
    from brickpulse.rules import list_rules

    return {r.id for r in list_rules() if getattr(r, "cloud_savings", "none") != "none"}


def account_overview(recs: list[Recommendation]) -> AccountOverview:
    # Collapse for the waste + count basis (overlapping fixes on one resource counted
    # once). Savings uses the booked lower bound, computed from the raw recs.
    resources = _collapse_to_resources(recs)
    by_ws_raw: dict[str, list[Recommendation]] = defaultdict(list)
    for r in recs:
        by_ws_raw[r.workspace_id].append(r)

    summaries = [_workspace_summary(wid, rs) for wid, rs in by_ws_raw.items()]
    # Rank by the fuller estimate (booked + estimated), not the booked floor, so a
    # workspace whose opportunity is mostly high/medium-confidence estimate ranks by
    # its real size rather than being buried. Ties break on the booked floor.
    summaries.sort(
        key=lambda s: (s.monthly_savings_addressable_usd, s.monthly_savings_usd),
        reverse=True,
    )

    low, high = _savings_range(recs)
    return AccountOverview(
        total_monthly_savings_usd=_booked_savings_total(recs),
        total_monthly_savings_addressable_usd=_addressable_savings_total(recs),
        total_monthly_savings_low_usd=low,
        total_monthly_savings_high_usd=high,
        savings_tiers=[
            SavingsTier(tier=t, monthly_savings_usd=v)
            for t, v in _savings_tier_breakdown(recs).items()
        ],
        savings_by_category=_savings_breakdown(recs, key=lambda r: r.category),
        savings_by_rule=_savings_breakdown(recs, key=lambda r: r.rule_id),
        total_monthly_waste_usd=waste_total(recs),
        recommendation_count=len(resources),
        workspace_count=len(by_ws_raw),
        workspaces=summaries,
        priced=priced_totals(recs),
    )


def _workspace_summary(workspace_id: str, raw_recs: list[Recommendation]) -> WorkspaceSummary:
    """``raw_recs`` are this workspace's un-collapsed findings. Waste/count collapse
    to one-per-resource; the savings headline is the booked lower bound. ``top_rule``
    / ``top_category`` rank on the point estimate — they name the biggest OPPORTUNITY,
    which is meaningful even when that opportunity is currently only ``estimated``."""
    resources = _collapse_to_resources(raw_recs)
    savings_by_rule: dict[str, float] = defaultdict(float)
    savings_by_cat: dict[str, float] = defaultdict(float)
    name = None
    for r in resources:
        savings_by_rule[r.rule_id] += r.monthly_savings_usd
        savings_by_cat[r.category] += r.monthly_savings_usd
        name = name or r.workspace_name
    top_rule = max(savings_by_rule, key=savings_by_rule.get) if savings_by_rule else None
    top_cat = max(savings_by_cat, key=savings_by_cat.get) if savings_by_cat else None
    return WorkspaceSummary(
        workspace_id=workspace_id,
        workspace_name=name,
        monthly_savings_usd=_booked_savings_total(raw_recs),
        monthly_savings_addressable_usd=_addressable_savings_total(raw_recs),
        monthly_waste_usd=waste_total(raw_recs),
        recommendation_count=len(resources),
        top_rule_id=top_rule,
        top_category=top_cat,
        monthly_savings_addressable_contract_usd=(
            _sum_by_scope(raw_recs, value=lambda r: _contract_or_list(r, "point"),
                          eligible=lambda r: r.savings_status != "advisory")
            if any(r.price_basis for r in raw_recs) else None),
    )


def workspace_detail(workspace_id: str, recs: list[Recommendation]) -> WorkspaceDetail:
    ws_recs = [r for r in recs if r.workspace_id == workspace_id]
    resources = _collapse_to_resources(ws_recs)
    # by_category stays on the point estimate: it is the opportunity breakdown, not the
    # booked floor, so once rules are reclassified its sum may exceed the booked headline (the
    # difference being the estimated/advisory opportunity). Intentional.
    by_cat_savings: dict[str, float] = defaultdict(float)
    by_cat_count: dict[str, int] = defaultdict(int)
    name = None
    for r in resources:
        by_cat_savings[r.category] += r.monthly_savings_usd
        by_cat_count[r.category] += 1
        name = name or r.workspace_name
    breakdown = [
        CategoryBreakdown(
            category=c,
            monthly_savings_usd=round(by_cat_savings[c], 2),
            recommendation_count=by_cat_count[c],
        )
        for c in sorted(by_cat_savings, key=by_cat_savings.get, reverse=True)
    ]
    low, high = _savings_range(ws_recs)
    return WorkspaceDetail(
        workspace_id=workspace_id,
        workspace_name=name,
        monthly_savings_usd=_booked_savings_total(ws_recs),
        monthly_savings_addressable_usd=_addressable_savings_total(ws_recs),
        monthly_savings_low_usd=low,
        monthly_savings_high_usd=high,
        savings_tiers=[
            SavingsTier(tier=t, monthly_savings_usd=v)
            for t, v in _savings_tier_breakdown(ws_recs).items()
        ],
        monthly_waste_usd=waste_total(ws_recs),
        recommendation_count=len(resources),
        by_category=breakdown,
        priced=priced_totals(ws_recs),
    )


# ---------------------------------------------------------------- "Saved so far"
# The tracker (precompute/change_tracker.py) already ran the day loop and this module's own book_scope dedup
# per spend pool and day, so finops_savings_daily holds the booked figure. This only
# SUMS it over a window and explains it from finops_change_events. Rows are plain dicts
# (from the snapshot read or the demo source). Every key is read with .get, so a table
# written by an older or newer tracker still reads.

#: Maps change_kind to its tooltip bucket. An unknown kind keeps its own name, so a kind
#: a newer tracker adds still shows up (the UI labels it with the raw key).
REALIZED_KIND_BUCKET: dict[str, str] = {
    "reached": "full",
    "beyond": "full",
    "toward": "partial",
    "deleted": "deleted",
    "paused": "deleted",
    "away": "away",
}

#: Kinds that book credit (mirrors finops_rules.change_tracking.CREDIT_KINDS).
_CREDIT_KINDS = frozenset({"toward", "reached", "beyond", "deleted", "paused"})


def _as_date(v) -> date | None:
    """A DATE / TIMESTAMP / ISO string as a date. ``None`` for anything unreadable."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _as_list(v) -> list:
    """An ARRAY column as a list: a list, a numpy array, or (connector without
    pyarrow) a JSON string. ``None`` or garbage gives []."""
    if v is None:
        return []
    if isinstance(v, str):
        import json

        try:
            parsed = json.loads(v)
        except ValueError:
            return [v] if v else []
        return list(parsed) if isinstance(parsed, list) else []
    try:
        return list(v)
    except TypeError:
        return []


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _event_order(e: dict) -> tuple[str, str]:
    return (str(e.get("changed_at")), str(e.get("recorded_at") or ""))


def realized_totals(
    daily_rows: list[dict],
    events: list[dict],
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    default_end: date | None = None,
    tracking_since: date | None = None,
    data_through: date | None = None,
    excluded_rule_ids: frozenset[str] = frozenset(),
) -> dict:
    """The "Saved so far" figure for one scope and window, as the fields of
    :class:`~finops.models.realized.RealizedSavings`.

    * ``saved_usd`` = ``SUM(saved_usd)`` of the daily rows with ``window_start <= day
      <= window_end``. The window defaults to ``tracking_since`` through the last
      booked day, i.e. cumulative since deployment.
    * ``run_rate_usd`` = the summed ``run_rate_monthly_usd`` on the run-rate day,
      ``min(window_end, data_through)``. ``data_through`` is the tracker's last booked
      day across the WHOLE table (not just this scope), so a scope whose bookings
      ended (the setting moved back, or the workspace was retired) reads 0 instead of
      its last value.
    * ``default_end`` is the end used when no ``window_end`` is given (the source
      passes yesterday). It is clamped to the start, so on day 0 (``tracking_since`` =
      today) the window is ``today..today`` and empty, not inverted.
    * ``by_kind`` / ``change_count`` / ``partial_example`` describe the findings booked
      in the window, each by its latest event on or before ``window_end``. A finding
      whose latest event is ``away`` booked earlier in the window but has since moved
      back, so it goes in ``by_kind["away"]`` and not in ``change_count``. The per-kind
      ``monthly_usd`` is the run-rate split across the kinds in effect on the run-rate
      day, so the kinds sum to ``run_rate_usd``.
    * ``not_counted`` groups the credit events the tracker did not book, by
      ``not_counted_reason``. It takes the latest event per finding, changed on or
      before ``window_end`` (and on or after ``window_start`` when one was passed).

    ``excluded_rule_ids`` (hidden + experimental rules) drops those rules' events. It
    drops a daily row only when EVERY rule on it is excluded. A row that mixes rules is
    kept whole, because the tracker already deduped that pool and its figure can't be
    split back into per-rule parts. The tracker excludes hidden rules itself, so this
    only matters when the app's settings differ from the pipeline's.
    """
    rows: list[tuple[date, dict]] = []
    for r in daily_rows:
        day = _as_date(r.get("day"))
        if day is None:
            continue
        rule_ids = [str(x) for x in _as_list(r.get("rule_ids"))]
        if rule_ids and all(x in excluded_rule_ids for x in rule_ids):
            continue
        rows.append((day, r))

    all_days = [d for d, _ in rows]
    tracking_since = tracking_since or (min(all_days) if all_days else None)
    explicit_start = window_start is not None
    start = window_start or tracking_since
    last_booked = data_through or (max(all_days) if all_days else None)
    if window_end is not None:
        end = window_end
    elif default_end is not None:
        # The default window ends yesterday (the tracker books through yesterday). On
        # day 0 tracking_since is today, so clamp the end to the start: an empty window
        # (start == end, nothing booked), never one whose end precedes its start.
        end = max(default_end, start) if start else default_end
    else:
        end = last_booked or start

    in_window = [
        (d, r) for d, r in rows
        if (start is None or d >= start) and (end is None or d <= end)
    ]
    saved = sum(_f(r.get("saved_usd")) for _, r in in_window)

    month_totals: dict[str, float] = defaultdict(float)
    for d, r in in_window:
        month_totals[f"{d.year:04d}-{d.month:02d}"] += _f(r.get("saved_usd"))
    by_month = (
        [{"month": m, "saved_usd": round(month_totals.get(m, 0.0), 2)}
         for m in _months(start, end)]
        if start and end and start <= end else []
    )

    rate_day = min(end, last_booked) if end and last_booked else None
    run_rate = sum(
        _f(r.get("run_rate_monthly_usd")) for d, r in rows if rate_day and d == rate_day
    )

    booked_keys = {str(k) for _, r in in_window for k in _as_list(r.get("finding_keys"))}

    # The latest event per finding on or before the window end, counted and uncounted
    # kept apart (the tracker re-derives `counted` per event every run).
    latest: dict[str, dict] = {}
    latest_uncounted: dict[str, dict] = {}
    counted_by_finding: dict[str, list[tuple[date, dict]]] = defaultdict(list)
    for e in events:
        if str(e.get("rule_id")) in excluded_rule_ids:
            continue
        key, changed = e.get("finding_key"), _as_date(e.get("changed_at"))
        if key is None or changed is None or (end is not None and changed > end):
            continue
        bucket = latest if e.get("counted") else latest_uncounted
        prev = bucket.get(str(key))
        if prev is None or _event_order(e) >= _event_order(prev):
            bucket[str(key)] = e
        if e.get("counted"):
            counted_by_finding[str(key)].append((changed, e))

    def _state_on(key: str, day: date) -> dict | None:
        """The finding's latest counted event on or before ``day``."""
        best = None
        for changed, e in counted_by_finding.get(key, ()):
            if changed <= day and (best is None or _event_order(e) >= _event_order(best)):
                best = e
        return best

    by_kind: dict[str, dict] = {
        k: {"count": 0, "monthly_usd": 0.0} for k in ("full", "partial", "deleted")
    }
    partial_best: dict | None = None
    for key in sorted(booked_keys):
        e = latest.get(key)
        if e is None:
            continue
        kind = str(e.get("change_kind") or "")
        bucket = REALIZED_KIND_BUCKET.get(kind, kind or "other")
        slot = by_kind.setdefault(bucket, {"count": 0, "monthly_usd": 0.0})
        slot["count"] += 1
        value = 0.0 if kind == "away" else _f(e.get("valued_monthly_usd"))
        if bucket == "partial" and (
            partial_best is None or value > _f(partial_best.get("valued_monthly_usd"))
        ):
            partial_best = e

    # The per-kind dollars reconcile with the run-rate on the tile: every daily row on
    # the run-rate day (already deduped per pool by the tracker) splits its booked value
    # across the kinds of the findings in effect in it, in proportion to their own
    # valuations. So sum(by_kind monthly_usd) == run_rate_usd. A finding no longer in
    # effect that day (moved back, or its workspace retired) keeps its count and adds $0.
    # The not-counted lines are outside that sum. A row whose findings have no event
    # (the events table is unreadable) stays unattributed.
    for d, r in rows:
        if rate_day is None or d != rate_day:
            continue
        rate = _f(r.get("run_rate_monthly_usd"))
        parts: list[tuple[str, float]] = []
        for key in _as_list(r.get("finding_keys")):
            e = _state_on(str(key), rate_day)
            kind = str(e.get("change_kind") or "") if e else ""
            if e is None or (kind not in _CREDIT_KINDS and kind in REALIZED_KIND_BUCKET):
                continue
            parts.append((REALIZED_KIND_BUCKET.get(kind, kind or "other"),
                          _f(e.get("valued_monthly_usd"))))
        total = sum(v for _, v in parts)
        for bucket, v in parts:
            share = rate * (v / total) if total > 0 else rate / len(parts)
            slot = by_kind.setdefault(bucket, {"count": 0, "monthly_usd": 0.0})
            slot["monthly_usd"] += share
    for slot in by_kind.values():
        slot["monthly_usd"] = round(slot["monthly_usd"], 2)
    change_count = sum(v["count"] for k, v in by_kind.items() if k != "away")

    not_counted: dict[str, dict] = {}
    nc_workspaces: dict[str, set] = defaultdict(set)
    for key, e in sorted(latest_uncounted.items()):
        counted = latest.get(key)
        if counted is not None and _event_order(counted) >= _event_order(e):
            continue  # a later counted event for the same finding supersedes it
        if str(e.get("change_kind") or "") not in _CREDIT_KINDS:
            continue
        changed = _as_date(e.get("changed_at"))
        if explicit_start and changed is not None and changed < start:
            continue
        reason = str(e.get("not_counted_reason") or "other")
        slot = not_counted.setdefault(
            reason, {"count": 0, "workspace_count": 0, "monthly_usd": 0.0}
        )
        slot["count"] += 1
        slot["monthly_usd"] += _f(e.get("valued_monthly_usd"))
        if e.get("workspace_id") is not None:
            nc_workspaces[reason].add(str(e.get("workspace_id")))
    for reason, slot in not_counted.items():
        slot["workspace_count"] = len(nc_workspaces[reason])
        slot["monthly_usd"] = round(slot["monthly_usd"], 2)

    example = None
    if partial_best is not None:
        def _s(k):
            v = partial_best.get(k)
            return None if v is None else str(v)

        fraction = partial_best.get("fraction")
        estimate = partial_best.get("estimate_before_usd")
        example = {
            "finding_key": str(partial_best.get("finding_key")),
            "rule_id": _s("rule_id"),
            "resource_name": _s("resource_name"),
            "setting": _s("setting"),
            "baseline_value": _s("baseline_value"),
            "after_value": _s("after_value"),
            "proposed_value": _s("proposed_value"),
            "fraction": None if fraction is None else round(_f(fraction), 4),
            "estimate_before_usd": None if estimate is None else round(_f(estimate), 2),
            "valued_monthly_usd": round(_f(partial_best.get("valued_monthly_usd")), 2),
        }

    return {
        "saved_usd": round(saved, 2),
        "run_rate_usd": round(run_rate, 2),
        "change_count": change_count,
        "tracking_since": tracking_since,
        "window_start": start,
        "window_end": end,
        "data_through": last_booked,
        "by_month": by_month,
        "by_kind": by_kind,
        "not_counted": not_counted,
        "partial_example": example,
    }
