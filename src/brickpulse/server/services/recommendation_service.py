"""Orchestrates a scoped fetch + filtering + sorting for the API layer.

Mode-agnostic: it only talks to a :class:`RecommendationSource`, so live and
precompute behave identically from here up.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data import RecommendationSource
from ..identity import Scope
from ..models import (
    ExperimentalOverview,
    GroupViewCounts,
    QuickWinsSummary,
    Recommendation,
    RecommendationGroup,
    RecommendationGroupsSummary,
    TaggingOverview,
)
from ..models.group import group_id
from .aggregation import (
    _addressable_savings_total,
    _collapse_to_resources,
    resource_count,
    waste_total,
)
from .serverless_fit import evaluate as evaluate_serverless_fit

#: Sort keys the API accepts. There is deliberately no "waste" sort: the GUI no
#: longer surfaces a wasteful-spend figure to sort by (it was an estimate summed
#: across rules, easily misread as a bill). ``monthly_waste_usd`` survives on the
#: model as the resource's own billed spend, which the detail page labels honestly.
_SORTS = {
    "nba": lambda r: r.nba_score,
    "savings": lambda r: r.monthly_savings_usd,
}

# Effort bands from easiest to hardest — used to pick a group's "easiest win".
_EFFORT_ORDER = {"trivial": 0, "low": 1, "medium": 2, "high": 3}
_PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

#: Group sort keys for ``list_grouped``. ``nba`` / ``savings`` sort descending, with
#: savings then spend as tie-breaks so equal scores (every $0 advisory group has NBA 0)
#: come out in a stable, meaningful order. ``effort`` sorts the easiest first (the
#: group's effort band is its easiest option's), then the biggest saving.
_GROUP_SORTS = {
    "nba": lambda g: (-g.nba_score, -g.monthly_savings_usd, -g.monthly_waste_usd),
    "savings": lambda g: (-g.monthly_savings_usd, -g.nba_score, -g.monthly_waste_usd),
    "effort": lambda g: (_EFFORT_ORDER.get(g.effort_band, 99), -g.monthly_savings_usd),
}

#: Quick-view presets of the Recommendations page. Each is a set of extra filters
#: intersected with the explicit ones: ``effort`` / ``status`` filter findings before
#: grouping, ``priority`` filters the group (as the explicit priority filter does).
VIEW_PRESETS: dict[str, dict[str, set[str]]] = {
    "all": {},
    "quick_wins": {"effort": {"trivial", "low"}, "status": {"bookable", "estimated"}},
    "booked": {"status": {"bookable"}},
    "high_priority": {"priority": {"HIGH"}},
    "advisory": {"status": {"advisory"}},
}


class RecommendationService:
    def __init__(self, source: RecommendationSource):
        self.source = source

    # --- reads ----------------------------------------------------------------
    def list(
        self,
        scope: Scope,
        *,
        workspace_id: str | None = None,
        rule_id: str | None = None,
        resource_type: str | None = None,
        priority: str | None = None,
        min_savings: float | None = None,
        sort: str = "nba",
    ) -> list[Recommendation]:
        recs = self.source.fetch(scope)
        recs = self._filter(
            recs,
            workspace_id=workspace_id,
            rule_id=rule_id,
            resource_type=resource_type,
            priority=priority,
            min_savings=min_savings,
        )
        key = _SORTS.get(sort, _SORTS["nba"])
        recs.sort(key=key, reverse=True)
        return recs

    def list_grouped(
        self,
        scope: Scope,
        *,
        workspace_id: str | None = None,
        resource_type: str | None = None,
        priority: str | None = None,
        min_savings: float | None = None,
        rule_ids: list[str] | None = None,
        sort: str = "nba",
        effort: list[str] | None = None,
        status: list[str] | None = None,
        q: str | None = None,
        view: str | None = None,
        _recs: list[Recommendation] | None = None,
    ) -> list[RecommendationGroup]:
        """Overview rollup: one group per (workspace, resource) with its options.

        Collapses the several rules that may flag the same resource into a single
        row whose headline savings is the MAX of the options (they overlap, so
        the sum would double-count). Filters that make sense pre-grouping
        (workspace/resource_type) are applied to the members; savings/priority
        filters are applied to the resulting group's representative figure so the
        overview stays consistent with what the row shows.

        ``rule_ids`` filters by which recommendation TYPES apply to a resource,
        with ALL (conjunctive) semantics: a group is kept only when every
        requested rule is among its options — i.e. "resources where all of these
        opportunities apply at once". It is applied AFTER grouping (never as a
        member filter) precisely because a group spans rules: filtering members to
        one rule first would destroy the very overlap this filter selects on.

        ``effort`` / ``status`` (bands / savings_status values) filter the FINDINGS
        before grouping, so a "Quick wins" card shows the quick-win option and the
        group figures are recomputed on the matching options only. ``q`` keeps a
        group when any of its options matches the search (resource name/id,
        workspace name, owner). ``view`` is a quick-view preset
        (:data:`VIEW_PRESETS`) intersected with the explicit filters; an unknown
        view is ignored, so an older client and a newer server never 400."""
        recs = self.source.fetch(scope) if _recs is None else _recs
        recs = self._filter(
            recs,
            workspace_id=workspace_id,
            rule_id=None,
            resource_type=resource_type,
            priority=None,
            min_savings=None,
        )
        groups = _filter_groups(
            recs,
            _GroupFilters.of(
                priority=priority, min_savings=min_savings, rule_ids=rule_ids,
                effort=effort, status=status, q=q, view=view,
            ),
        )
        return _sort_groups(groups, sort)

    def grouped_summary(
        self,
        scope: Scope,
        *,
        workspace_id: str | None = None,
        resource_type: str | None = None,
        priority: str | None = None,
        min_savings: float | None = None,
        rule_ids: list[str] | None = None,
        effort: list[str] | None = None,
        status: list[str] | None = None,
        q: str | None = None,
        view: str | None = None,
        _recs: list[Recommendation] | None = None,
    ) -> RecommendationGroupsSummary:
        """The summary strip for :meth:`list_grouped` with the same params (one
        fetch): totals for the current view, plus a group count per quick-view
        preset under every OTHER current filter."""
        recs = self._filter(
            self.source.fetch(scope) if _recs is None else _recs,
            workspace_id=workspace_id,
            rule_id=None,
            resource_type=resource_type,
            priority=None,
            min_savings=None,
        )

        def groups_for(v: str | None) -> list[RecommendationGroup]:
            return _filter_groups(
                recs,
                _GroupFilters.of(
                    priority=priority, min_savings=min_savings, rule_ids=rule_ids,
                    effort=effort, status=status, q=q, view=v,
                ),
            )

        current = groups_for(view)
        options = [o for g in current for o in g.options]
        counted = [o for o in options if o.savings_status != "advisory"]
        best = _collapse_to_resources(counted)
        quick = [o for o in best if o.effort_band in ("trivial", "low")]
        return RecommendationGroupsSummary(
            resources=len(current),
            potential_usd=_addressable_savings_total(options),
            quick_wins=QuickWinsSummary(
                count=len(quick),
                value_usd=round(sum(o.monthly_savings_usd for o in quick), 2),
            ),
            advisory_only=sum(
                1 for g in current if all(o.savings_status == "advisory" for o in g.options)
            ),
            views=GroupViewCounts(**{v: len(groups_for(v)) for v in VIEW_PRESETS}),
        )

    def grouped_page(self, scope: Scope, *, page: int = 1, page_size: int = 25, **filters) -> dict:
        """One scoped snapshot for the rows AND summary; no evidence on list rows."""
        recs = self.source.fetch(scope)
        groups = self.list_grouped(scope, _recs=recs, **filters)
        summary_filters = {k: v for k, v in filters.items() if k != "sort"}
        summary = self.grouped_summary(scope, _recs=recs, **summary_filters)
        offset = (page - 1) * page_size
        return {
            "items": [g.model_dump(exclude={"options", "serverless_fit"}) for g in groups[offset:offset + page_size]],
            "total": len(groups),
            "pagination": {"page": page, "page_size": page_size, "pages": (len(groups) + page_size - 1) // page_size},
            "summary": summary.model_dump(),
        }

    def get(self, scope: Scope, recommendation_id: str) -> Recommendation | None:
        # Searches the sidecar set (governance) and the experimental set (Labs) too,
        # since neither lives in fetch(), so a detail link opened from the Tagging &
        # Chargeback or Labs tab still resolves. Accepts a finding_key as well as a
        # recommendation_id: the latter changes every day, the former doesn't.
        for r in self.source._prepared(scope):
            if recommendation_id in (r.recommendation_id, r.finding_key):
                return r
        return None

    def get_group(self, scope: Scope, gid: str) -> RecommendationGroup | None:
        """Load one resource group by its stable id (for the detail page)."""
        members = [
            r
            for r in self.source.fetch(scope)
            if group_id(r.workspace_id, r.resource_type, r.resource_id) == gid
        ]
        if not members:
            return None
        return _build_group(members)

    def next_best_actions(self, scope: Scope, limit: int = 10) -> list[Recommendation]:
        recs = self.source.fetch(scope)
        recs.sort(key=lambda r: r.nba_score, reverse=True)
        return recs[:limit]

    # --- sidecar categories (Tagging & Chargeback tab) ------------------------
    def list_sidecar(
        self,
        scope: Scope,
        *,
        workspace_id: str | None = None,
        rule_id: str | None = None,
        resource_type: str | None = None,
        priority: str | None = None,
    ) -> list[Recommendation]:
        """The relocated sidecar-category findings (governance/tagging), scoped
        like the main list. Sorted by unattributable spend (``monthly_waste_usd``)
        rather than savings — these carry $0 savings by design, so the size of the
        governance gap is the meaningful ranking."""
        recs = self.source.fetch_sidecar(scope)
        recs = self._filter(
            recs,
            workspace_id=workspace_id,
            rule_id=rule_id,
            resource_type=resource_type,
            priority=priority,
            min_savings=None,
        )
        recs.sort(key=lambda r: r.monthly_waste_usd, reverse=True)
        return recs

    # --- experimental rules (Labs tab) -------------------------------
    def list_experimental(
        self,
        scope: Scope,
        *,
        workspace_id: str | None = None,
        rule_id: str | None = None,
        resource_type: str | None = None,
        priority: str | None = None,
    ) -> list[Recommendation]:
        """The relocated experimental findings, scoped like the main list.
        Sorted by addressable spend (``monthly_waste_usd``) rather than savings —
        experimental findings are advisory and carry $0 booked savings, so the size
        of the addressable spend is the meaningful ranking."""
        recs = self.source.fetch_experimental(scope)
        recs = self._filter(
            recs,
            workspace_id=workspace_id,
            rule_id=rule_id,
            resource_type=resource_type,
            priority=priority,
            min_savings=None,
        )
        recs.sort(key=lambda r: r.monthly_waste_usd, reverse=True)
        return recs

    def experimental_overview(self, scope: Scope) -> ExperimentalOverview:
        """Headline + items for the Labs tab: the total addressable monthly spend the
        experimental findings identify (never a booked saving — these are advisory),
        plus counts and the findings themselves. The counts are intentionally separate
        from the main recommendation total, which excludes these rules entirely."""
        items = self.list_experimental(scope)
        return ExperimentalOverview(
            # One figure per spend pool, not per finding: overlapping rules on one
            # resource (or on the warehouse an alert bills to) would double it.
            total_addressable_monthly_usd=waste_total(items),
            recommendation_count=len(items),
            resource_count=resource_count(items),
            rule_count=len({r.rule_id for r in items}),
            workspace_count=len({r.workspace_id for r in items}),
            items=items,
        )

    def tagging_overview(self, scope: Scope) -> TaggingOverview:
        """Headline + items for the Tagging & Chargeback tab: the total
        unattributable monthly spend (the size of the governance gap), a count and
        the findings themselves. The count is intentionally separate from the main
        recommendation total, which excludes this category entirely."""
        items = self.list_sidecar(scope)
        return TaggingOverview(
            total_unattributable_monthly_usd=round(
                sum(r.monthly_waste_usd for r in items), 2
            ),
            recommendation_count=len(items),
            workspace_count=len({r.workspace_id for r in items}),
            items=items,
        )

    # --- internals ------------------------------------------------------------
    @staticmethod
    def _filter(
        recs: list[Recommendation],
        *,
        workspace_id: str | None,
        rule_id: str | None,
        resource_type: str | None,
        priority: str | None,
        min_savings: float | None,
    ) -> list[Recommendation]:
        out = recs
        if workspace_id:
            out = [r for r in out if r.workspace_id == workspace_id]
        if rule_id:
            out = [r for r in out if r.rule_id == rule_id]
        if resource_type:
            out = [r for r in out if r.resource_type == resource_type]
        if priority:
            out = [r for r in out if r.priority == priority.upper()]
        if min_savings is not None:
            out = [r for r in out if r.monthly_savings_usd >= min_savings]
        return out


def _intersect(*sets: set[str] | None) -> set[str] | None:
    """Intersection of the given constraint sets; None = unconstrained."""
    out: set[str] | None = None
    for s in sets:
        if s is None:
            continue
        out = set(s) if out is None else out & s
    return out


def _csv_set(values: list[str] | None) -> set[str] | None:
    """Lower-cased, de-blanked constraint set (None when nothing was given)."""
    vals = {v.strip().lower() for v in (values or []) if v and v.strip()}
    return vals or None


@dataclass(frozen=True)
class _GroupFilters:
    """The list_grouped filters with the quick-view preset folded in. ``None`` means
    unconstrained; an empty set means nothing can match (e.g. "Booked only" combined
    with an explicit status=estimated)."""

    effort: set[str] | None
    status: set[str] | None
    priority: set[str] | None
    rule_ids: frozenset[str]
    min_savings: float | None
    q: str

    @classmethod
    def of(
        cls,
        *,
        priority: str | None,
        min_savings: float | None,
        rule_ids: list[str] | None,
        effort: list[str] | None,
        status: list[str] | None,
        q: str | None,
        view: str | None,
    ) -> _GroupFilters:
        preset = VIEW_PRESETS.get((view or "all").strip().lower(), {})
        return cls(
            effort=_intersect(_csv_set(effort), preset.get("effort")),
            status=_intersect(_csv_set(status), preset.get("status")),
            priority=_intersect(
                {priority.strip().upper()} if priority and priority.strip() else None,
                preset.get("priority"),
            ),
            rule_ids=frozenset(r for r in (rule_ids or []) if r),
            min_savings=min_savings,
            q=(q or "").strip().lower(),
        )


def _matches_search(r: Recommendation, q: str) -> bool:
    fields = (r.resource_name, r.resource_id, r.workspace_name, r.resource_owner)
    return any(q in f.lower() for f in fields if f)


def _filter_groups(recs: list[Recommendation], f: _GroupFilters) -> list[RecommendationGroup]:
    """Finding-level filters (effort, status), then grouping, then the group-level
    filters (search, rule types, priority, min savings) exactly as before."""
    if f.effort is not None:
        recs = [r for r in recs if r.effort_band in f.effort]
    if f.status is not None:
        recs = [r for r in recs if r.savings_status in f.status]
    groups = _group_recommendations(recs)
    if f.q:
        groups = [g for g in groups if any(_matches_search(o, f.q) for o in g.options)]
    if f.rule_ids:
        groups = [g for g in groups if f.rule_ids <= {opt.rule_id for opt in g.options}]
    if f.priority is not None:
        groups = [g for g in groups if g.priority in f.priority]
    if f.min_savings is not None:
        groups = [g for g in groups if g.monthly_savings_usd >= f.min_savings]
    return groups


def _sort_groups(groups: list[RecommendationGroup], sort: str) -> list[RecommendationGroup]:
    return sorted(groups, key=_GROUP_SORTS.get(sort, _GROUP_SORTS["nba"]))


def _group_recommendations(recs: list[Recommendation]) -> list[RecommendationGroup]:
    """Collapse recs sharing a (workspace, resource_type, resource_id) into groups."""
    from collections import defaultdict

    buckets: dict[str, list[Recommendation]] = defaultdict(list)
    for r in recs:
        buckets[group_id(r.workspace_id, r.resource_type, r.resource_id)].append(r)
    return [_build_group(members) for members in buckets.values()]


def _build_group(members: list[Recommendation]) -> RecommendationGroup:
    """Build one group from its member findings.

    Representative savings is the action-aware combination of the options on this
    resource: mutually-exclusive alternatives collapse to the best one,
    compatible tunings (auto-stop + right-size) compose onto the remaining
    counterfactual — bounded by the resource's cost, so it is never the naive sum
    (that survives only as ``options_savings_sum_usd``). Defaults reproduce the old
    MAX-across-options. Waste is the MAX (the resource's own monthly spend). Effort is
    the easiest option; priority the most urgent.

    Options are ordered by **monthly savings, highest first**: a resource usually
    matches several rules at once (an oversized cluster can often be downsized
    *and* moved to serverless), and the detail page lists them all in sequence, so
    the biggest-dollar analysis must lead. The group's ``nba_score`` stays the max
    across options so this ordering never changes where the resource ranks in the
    overview."""
    options = sorted(
        members, key=lambda r: (r.monthly_savings_usd, r.nba_score), reverse=True
    )
    top = options[0]
    # options on one resource share a scope, so combine them action-aware
    # (compatible tunings compose, exclusive alternatives collapse to the best) rather
    # than summing. Defaults to the historical max across options. The naive sum stays
    # available as options_savings_sum_usd below.
    from .aggregation import _contract_or_list, book_scope
    savings = book_scope(options, value=lambda r: float(r.monthly_savings_usd or 0.0))
    contract_savings = basis = cloud_savings = None
    if any(r.price_basis for r in options):
        contract_savings = round(book_scope(options, value=lambda r: _contract_or_list(r, "point")), 2)
        dollar = [r for r in options if r.monthly_savings_usd > 0] or options
        basis = "contract" if all(r.price_basis == "contract" for r in dollar) else "list"
    if any(r.monthly_cloud_savings_usd is not None for r in options):
        cloud_savings = round(book_scope(
            options, value=lambda r: float(r.monthly_cloud_savings_usd or 0.0)), 2)
    waste = max(r.monthly_waste_usd for r in options)
    savings_sum = round(sum(r.monthly_savings_usd for r in options), 2)
    easiest = min(options, key=lambda r: _EFFORT_ORDER.get(r.effort_band, 99))
    top_priority = min(
        (r.priority for r in options), key=lambda p: _PRIORITY_ORDER.get(p, 99)
    )
    n = len(options)
    action_label = top.rule_title if n == 1 else _multi_action_label(top.resource_type, n)
    return RecommendationGroup(
        group_id=group_id(top.workspace_id, top.resource_type, top.resource_id),
        workspace_id=top.workspace_id,
        workspace_name=top.workspace_name,
        workspace_url=top.workspace_url,
        resource_type=top.resource_type,
        resource_id=top.resource_id,
        resource_name=top.resource_name,
        action_label=action_label,
        category=top.category,
        option_count=n,
        top_rule_id=top.rule_id,
        monthly_savings_usd=round(savings, 2),
        monthly_waste_usd=round(waste, 2),
        options_savings_sum_usd=savings_sum,
        monthly_savings_contract_usd=contract_savings,
        price_basis=basis,
        monthly_cloud_savings_usd=cloud_savings,
        effort_band=easiest.effort_band,
        priority=top_priority,
        # Max across options, not the (savings-ordered) first one's — the group's
        # rank in the overview must not depend on how options are sorted.
        nba_score=max(r.nba_score for r in options),
        # Serverless-fit verdict for the detail page (warehouses/jobs only). Built
        # from the options so it never contradicts the recommendations shown.
        serverless_fit=evaluate_serverless_fit(top.resource_type, options),
        options=options,
    )


def _multi_action_label(resource_type: str, n: int) -> str:
    noun = {
        "warehouse": "warehouse",
        "cluster": "cluster",
        "job": "job",
        # Slug -> what an admin calls it; "serving_endpoint" would read as a typo.
        "serving_endpoint": "serving endpoint",
    }.get(resource_type, "resource")
    return f"{n} ways to optimize this {noun}"
