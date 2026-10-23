"""RecommendationSource abstraction shared by live and precompute modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ..config import Settings
from ..identity import Scope
from ..models import Recommendation


class RecommendationSource(ABC):
    """Returns scoped recommendations. Both concrete sources return identical
    :class:`Recommendation` objects, so every endpoint is mode-agnostic.

    ``fetch`` is a template method: subclasses implement :meth:`load` and the base
    class applies three cross-cutting view filters to everything they return, at the
    single boundary every read path crosses — so none can leak into a list, a
    group, a chart, a savings total or the NBA ranking, including the aggregation
    endpoints that read ``service.source`` directly rather than going through
    :class:`RecommendationService`:

    * the **hidden-rule filter** (a presentation decision — the precompute job still
      computes and stores these rules, see ``config.Settings.hidden_rule_ids``),
    * the **sidecar-category filter** — categories relocated to their own tab
      (``config.Settings.sidecar_category_set``, e.g. ``governance``): still fully
      computed and visible, but served ONLY by :meth:`fetch_sidecar`, never by
      :meth:`fetch`, so they don't count toward the recommendation total, don't
      appear as an option on an optimizable resource, and don't enter the NBA
      ranking or savings leaderboard, and
    * the **experimental-rule filter** — net-new opportunity rules
      (``config.Settings.experimental_rule_ids``): the same relocation contract as the
      sidecar, but keyed on the RULE rather than the category. Served ONLY by
      :meth:`fetch_experimental` (the Labs tab), never by :meth:`fetch` or
      :meth:`fetch_sidecar`, so an unvalidated experimental finding can never leak into
      the headline total, an optimizable resource's options, or the NBA ranking.
    """

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect  # zero-arg callable -> open SQL connection

    @abstractmethod
    def load(self, scope: Scope) -> list[Recommendation]:
        """Mode-specific read (warehouse query / Delta snapshot / demo data)."""
        raise NotImplementedError

    def _prepared(self, scope: Scope) -> list[Recommendation]:
        """Scoped recs with hidden rules removed, BEFORE the sidecar-category split.
        Shared by :meth:`fetch` and :meth:`fetch_sidecar` so both see the identical
        scoped/hidden set and differ only in which side of the category partition
        they keep."""
        recs = self.load(scope)
        hidden = self.settings.hidden_rule_ids
        if hidden:
            recs = [r for r in recs if r.rule_id not in hidden]
        return recs

    def fetch(self, scope: Scope) -> list[Recommendation]:
        """The MAIN recommendation set: scoped recs with hidden rules and sidecar
        categories removed. This is what every optimization surface reads (lists,
        groups, NBA, savings totals), so a sidecar category can never leak in."""
        recs = self._prepared(scope)
        sidecar = self.settings.sidecar_category_set
        if sidecar:
            recs = [r for r in recs if r.category not in sidecar]
        experimental = self.settings.experimental_rule_ids
        if experimental:
            recs = [r for r in recs if r.rule_id not in experimental]
        return recs

    def fetch_sidecar(self, scope: Scope) -> list[Recommendation]:
        """ONLY the sidecar-category findings (e.g. ``governance``) — the mirror of
        :meth:`fetch`. Same scoping and hidden-rule filter; it just keeps the
        categories :meth:`fetch` drops. Serves the Tagging & Chargeback tab. Empty
        when no sidecar category is configured. Experimental rules are excluded here
        too, so they surface only on the Labs tab even if a future one carried a
        sidecar category."""
        sidecar = self.settings.sidecar_category_set
        if not sidecar:
            return []
        experimental = self.settings.experimental_rule_ids
        recs = self._prepared(scope)
        return [
            r for r in recs
            if r.category in sidecar and r.rule_id not in experimental
        ]

    def fetch_experimental(self, scope: Scope) -> list[Recommendation]:
        """ONLY the experimental findings — the mirror of :meth:`fetch` for the Labs
        tab. Same scoping and hidden-rule filter; it keeps the rule ids :meth:`fetch`
        drops. Empty when no experimental rule is configured."""
        experimental = self.settings.experimental_rule_ids
        if not experimental:
            return []
        recs = self._prepared(scope)
        return [r for r in recs if r.rule_id in experimental]
