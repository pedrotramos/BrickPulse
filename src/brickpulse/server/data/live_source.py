"""Live mode: run each enabled rule's SQL on a warehouse, score, rank.

Rules are executed CONCURRENTLY (one connection per rule) so wall-clock time is
roughly the slowest single rule rather than the sum of all of them — important
because several rules scan system.compute.node_timeline. Cheap for a handful of
workspaces (dev/demo); for 100s of workspaces use precompute mode. The precompute
job reuses the exact same rule rendering + scoring.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from brickpulse.rules import (
    DEFAULT_MIN_OBSERVATION_DAYS,
    RuleContext,
    get_enabled_rules,
    load_effort,
    load_thresholds,
)

from brickpulse.rules.price_basis import (
    DEFAULT_CONTRACT_MIN_COVERAGE,
    PricingCapabilities,
    apply_price_basis,
    probe_capabilities,
)

from ..identity import Scope
from ..logging import get_logger
from ..models import Recommendation
from ..services.nba_ranker import build_recommendation
from .base import RecommendationSource
from .scoping import wrap_live

log = get_logger(__name__)

# Optional process-level cache for live mode. Live recomputes all rules per
# request (~tens of seconds), and the recommendation service is NOT a process
# singleton (unlike spend/apps/genie), so without this every page reload pays the
# full cost. Enabled by FINOPS_LIVE_CACHE_TTL (seconds); 0/unset = no caching
# (unchanged behaviour). Keyed by (catalog, scope) so the workspace switcher and
# per-workspace views cache independently.

_LIVE_CACHE_TTL = float(get_env("FINOPS_LIVE_CACHE_TTL", "0") or "0")
_LIVE_CACHE: dict[tuple, tuple[float, list]] = {}


def _plain(value):
    """Arrow-backed ARRAY/STRUCT cells -> plain lists/dicts, the shape rules see in
    precompute (via the JSON bridge). With pyarrow installed the connector returns
    numpy arrays, and ``row.get("x") or []`` on one raises (ambiguous truth value)."""
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class LiveRecommendationSource(RecommendationSource):
    def load(self, scope: Scope) -> list[Recommendation]:
        if _LIVE_CACHE_TTL > 0:
            key = (
                self.settings.catalog,
                "all" if scope.all_workspaces else ",".join(sorted(scope.workspace_ids)),
            )
            hit = _LIVE_CACHE.get(key)
            if hit and _time.monotonic() - hit[0] < _LIVE_CACHE_TTL:
                return hit[1]
            recs = self._load(scope)
            _LIVE_CACHE[key] = (_time.monotonic(), recs)
            return recs
        return self._load(scope)

    def _load(self, scope: Scope) -> list[Recommendation]:
        thresholds = load_thresholds()
        effort_overrides = load_effort()
        rules = get_enabled_rules(thresholds)
        # Live mode truncates the window (scans billing.usage synchronously per
        # request); precompute uses the full thresholds window. Savings are
        # extrapolated to a month via to_monthly(), so short windows are noisier
        # but keep the request fast — acceptable for the live/dev stopgap.
        #
        # The window must still be at least min_observation_days, or the guard in
        # finops_rules.history would clamp down to the window and demand activity
        # on EVERY day of it — suppressing normal weekday-only resources. Live mode
        # is therefore floored at the observation minimum rather than being allowed
        # to run a window too short to judge anything on.
        min_obs = int(thresholds.get("min_observation_days", DEFAULT_MIN_OBSERVATION_DAYS))
        lookback = max(self.settings.live_lookback_days, min_obs)
        catalog = thresholds.get("catalog", self.settings.catalog)
        ws_map = self._workspace_map(catalog)
        owner_map = self._owner_map(catalog, lookback)
        caps = self._capabilities(catalog, lookback)
        min_cov = float(thresholds.get("contract_min_coverage", DEFAULT_CONTRACT_MIN_COVERAGE))

        def run_rule(rule) -> list[Recommendation]:
            ctx = RuleContext(
                thresholds=thresholds.get(rule.id, {}),
                phasing=thresholds.get("phasing"),  # engine-wide phased-downsizing block
                lookback_days=lookback,
                catalog=catalog,
                currency=self.settings.currency,
                min_observation_days=min_obs,
                contract_prices=caps.contract_prices,
                cloud_costs=caps.cloud_costs,
                contract_min_coverage=min_cov,
            )
            sql = wrap_live(rule.render_sql(ctx), scope)
            try:
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(sql)
                    columns = [d[0] for d in cur.description]
                    rows = [
                        {c: _plain(v) for c, v in zip(columns, r)} for r in cur.fetchall()
                    ]
            except Exception:  # one bad/slow rule shouldn't sink the whole page
                log.exception("Rule %s failed to execute; skipping", rule.id)
                return []
            out: list[Recommendation] = []
            for row in rows:
                finding = rule.score(row, ctx)
                if finding is not None:
                    apply_price_basis(rule, row, finding, ctx)
                    ws_name, ws_url = ws_map.get(str(finding.workspace_id), (None, None))
                    owner, creator = owner_map.get(
                        (str(finding.workspace_id), str(finding.resource_id)), (None, None)
                    )
                    out.append(
                        build_recommendation(
                            rule, finding, effort_overrides=effort_overrides,
                            workspace_name=ws_name, workspace_url=ws_url,
                            resource_owner=owner, resource_creator=creator,
                        )
                    )
            return out

        recs: list[Recommendation] = []
        if rules:
            with ThreadPoolExecutor(max_workers=min(8, len(rules))) as pool:
                for future in as_completed(pool.submit(run_rule, r) for r in rules):
                    recs.extend(future.result())

        recs.sort(key=lambda r: r.nba_score, reverse=True)
        return recs

    def _capabilities(self, catalog: str, lookback: int) -> PricingCapabilities:
        """Which optional price sources (contract rates, cloud VM cost) exist.

        Two LIMIT 1 probes; any failure means "absent" so the rules render the
        list-only SQL."""

        def run(sql):
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()

        return probe_capabilities(
            run, catalog, self.settings.currency, lookback,
            pricing_mode=self.settings.pricing_mode,
        )

    def _workspace_map(self, catalog: str) -> dict[str, tuple]:
        """workspace_id -> (name, url) from system.access.workspaces_latest.

        Authoritative host for cross-workspace deep links. Degrades to empty
        (links fall back client-side) if the schema isn't accessible to the
        caller's identity."""
        sql = (
            f"SELECT workspace_id, workspace_name, workspace_url "
            f"FROM {catalog}.access.workspaces_latest"
        )
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                return {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}
        except Exception:
            log.warning("workspaces_latest unavailable; deep links may fall back")
            return {}

    def _owner_map(self, catalog: str, lookback: int) -> dict[tuple[str, str], tuple]:
        """(workspace_id, resource_id) -> (owner, creator) from billing.usage.

        Same source as the precompute owner_map (see owner_sql); resolves who owns
        each flagged resource so live/dev mode surfaces owners like precompute does.
        Degrades to empty (owners null) if billing.usage is unreadable."""
        from .owner_sql import owner_map_sql

        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(owner_map_sql(catalog, lookback))
                return {(str(r[0]), str(r[1])): (r[2], r[3]) for r in cur.fetchall()}
        except Exception:
            log.warning("owner_map unavailable; owners will be null")
            return {}
