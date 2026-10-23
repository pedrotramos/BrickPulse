"""Workspace total-spend reads: precomputed snapshot first, live query as fallback.

Total spend is a different question from "what should I fix", so it has its own
source rather than riding on the recommendation table.

Read order, and why:

1. **The snapshot table** (``finops_workspace_spend``, written by the precompute
   job). One cheap scoped SELECT, scales to hundreds of workspaces — the
   production path.
2. **A live aggregate** over ``system.billing.usage`` when the snapshot table is
   missing or empty. That happens in exactly two situations we care about: a fresh
   deployment whose first precompute run hasn't happened yet, and live/dev mode.
   Without the fallback the Total Spend KPI would read $0 on a brand-new install,
   which looks like a bug rather than a missing snapshot.
3. **Unavailable**, if both fail (e.g. the caller has no grant on
   ``system.billing``). Reported honestly as ``unavailable`` so the UI omits the
   figure instead of showing a confident zero.

Results are cached briefly: the KPI appears on several pages, and the live path
scans billing data, so re-querying per request would be wasteful for a number that
changes daily at best.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
from collections.abc import Callable
from datetime import date, timedelta

from brickpulse.rules.resolver import TableResolver
from brickpulse.rules.price_basis import probe_capabilities

from ..config import Settings
from ..identity import Scope
from ..logging import get_logger
from ..models.spend import AccountSpend, SkuSpend, SpendDay, WorkspaceSpend
from .bounded_cache import MISS, BoundedCache
from .region import out_of_scope_count_sql
from .scoping import where_clause
from .spend_sql import workspace_spend_sql
from .sql_client import qualified_table

log = get_logger(__name__)

#: Cache TTL (seconds). Spend is recomputed daily by the job, so a few minutes of
#: staleness is irrelevant, while re-scanning billing.usage on every page view is not
#: free. Overridable via FINOPS_SPEND_CACHE_TTL.

_CACHE_TTL_S = int(get_env("FINOPS_SPEND_CACHE_TTL", "300") or "300")
# LRU bound by workspaces (~17 KB each cached), the bulk of an entry.
_CACHE_MAX_WORKSPACES = int(get_env("FINOPS_SPEND_CACHE_MAX_WORKSPACES") or "20000")

_SNAPSHOT_COLUMNS = [
    "workspace_id", "workspace_name", "lookback_days", "total_spend_usd",
    "monthly_spend_usd", "total_dbus", "serverless_spend_usd", "classic_spend_usd",
    "by_sku", "by_day",
]

#: Contract / cloud columns — OPTIONAL on read: a snapshot written before they
#: existed lacks them, so they're selected only when present.
_OPTIONAL_SNAPSHOT_COLUMNS = [
    "total_spend_contract_usd", "contract_priced_share", "total_cloud_cost_usd",
]
_SNAPSHOT_TABLE_COLUMNS: dict[str, frozenset[str]] = {}


class SpendSource:
    """Reads per-workspace spend for the caller's scope."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache = BoundedCache(
            _CACHE_TTL_S, _CACHE_MAX_WORKSPACES, lambda sp: len(sp.workspaces) + 1
        )

    # --- public ---------------------------------------------------------------
    def account_spend(self, scope: Scope) -> AccountSpend:
        """Total spend for every workspace in scope, plus account rollups."""
        scope_key = "all" if scope.all_workspaces else ",".join(sorted(scope.workspace_ids))
        hit = self._cache.get(scope_key, MISS)
        if hit is not MISS:
            return hit

        rows = self._from_snapshot(scope)
        if rows is None:
            rows = self._from_live(scope, TableResolver.nested(self.settings.catalog))
        out_of_scope = 0 if rows is None else (self._out_of_scope_count(scope) or 0)
        spend = _rollup(
            rows or [],
            self.settings.lookback_days,
            unavailable=rows is None,
            out_of_scope_workspaces=out_of_scope,
        )
        self._cache.put(scope_key, spend)
        return spend

    def workspace_spend(self, scope: Scope, workspace_id: str) -> WorkspaceSpend | None:
        for ws in self.account_spend(scope).workspaces:
            if ws.workspace_id == workspace_id:
                return ws
        return None

    # --- reads ----------------------------------------------------------------
    def _from_snapshot(self, scope: Scope) -> list[dict] | None:
        """Latest snapshot from the precompute table, or ``None`` if unusable."""
        if self.settings.compute_mode != "precompute":
            return None
        table = qualified_table(self.settings.spend_table)
        clause = where_clause(scope)
        present = _SNAPSHOT_TABLE_COLUMNS.get(table)
        if present is None:
            try:
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {table} LIMIT 0")
                    present = frozenset(d[0] for d in cur.description)
            except Exception:
                present = frozenset()
            _SNAPSHOT_TABLE_COLUMNS[table] = present
        cols = ", ".join(_SNAPSHOT_COLUMNS + [c for c in _OPTIONAL_SNAPSHOT_COLUMNS if c in present])
        sql = (
            f"SELECT {cols} FROM {table} "
            f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {table}) "
            f"{('AND ' + clause[len('WHERE '):]) if clause else ''}"
        )
        try:
            rows = self._query(sql)
        except Exception:
            # A missing table is the expected case before the first job run — fall
            # through to the live query rather than failing the page.
            log.info("spend snapshot unavailable (%s); falling back to live", table)
            return None
        return rows or None

    def _from_live(self, scope: Scope, resolver: TableResolver) -> list[dict] | None:
        caps = probe_capabilities(
            self._query, self.settings.catalog, self.settings.currency,
            self.settings.lookback_days, pricing_mode=self.settings.pricing_mode,
        )
        sql = workspace_spend_sql(
            resolver,
            lookback_days=self.settings.lookback_days,
            currency=self.settings.currency,
            contract_prices=caps.contract_prices,
            cloud_costs=caps.cloud_costs,
        )
        if not scope.all_workspaces:
            if not scope.workspace_ids:
                return []
            ids = ", ".join(f"'{w}'" for w in scope.workspace_ids)
            sql = f"SELECT * FROM ({sql}) WHERE workspace_id IN ({ids})"
        try:
            return self._query(sql)
        except Exception:
            log.warning("live workspace-spend query failed; total spend unavailable")
            return None

    def _out_of_scope_count(self, scope: Scope) -> int | None:
        """Workspaces that billed in the window but fall outside this region.

        Account-level context only: a workspace admin is already scoped to their own
        workspaces, so a region-exclusion count would be meaningless noise for them —
        return ``None`` (the UI omits the caption). Computed live because the
        snapshot holds only in-scope workspaces, so the global count is not in it;
        best-effort, so any failure (e.g. no billing grant) just hides the caption
        rather than failing the KPI.
        """
        if not scope.all_workspaces:
            return None
        try:
            rows = self._query(
                out_of_scope_count_sql(self.settings.catalog, self.settings.lookback_days)
            )
        except Exception:
            log.info("out-of-scope workspace count unavailable; omitting the caption")
            return None
        if not rows:
            return None
        return int(rows[0].get("out_of_scope_workspaces") or 0)

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class DemoSpendSource(SpendSource):
    """Offline spend data for demo mode — never opens a connection.

    Mirrors the demo recommendation source: deterministic, plausible spend per
    fictional workspace, built so the Total Spend KPI and its SKU breakdown render
    exactly as they do in production."""

    def __init__(self, settings: Settings, connect: Callable):
        super().__init__(settings, connect)

    def _from_snapshot(self, scope: Scope) -> list[dict] | None:
        return None  # demo has no table; go straight to the synthetic path

    def _out_of_scope_count(self, scope: Scope) -> int | None:
        # Demo is a self-contained single-region estate and never opens a
        # connection, so there is nothing out of scope to count.
        return None

    def _from_live(self, scope: Scope, resolver: TableResolver | None = None) -> list[dict] | None:
        # Demo synthesizes rows and never queries, so the resolver is unused here.
        import random

        from .demo_source import _WORKSPACES

        from .demo_source import DemoRecommendationSource, _unit, demo_pricing_state

        pricing_mode, pricing_cloud = demo_pricing_state()

        # A workspace can't bill less than the resources the demo findings are
        # about, so floor each workspace's spend at a multiple of its flagged
        # resources' own spend — otherwise savings could exceed the whole bill.
        resource_spend: dict[str, dict[str, float]] = {}
        for r in DemoRecommendationSource(self.settings, None)._all():
            ws = resource_spend.setdefault(r.workspace_id, {})
            key = f"{r.resource_type}|{r.resource_id}"
            ws[key] = max(ws.get(key, 0.0), r.monthly_waste_usd)

        rng = random.Random(20260804)  # deterministic, like the rec demo source
        allowed = None if scope.all_workspaces else set(scope.workspace_ids)
        rows: list[dict] = []
        for wid, name in _WORKSPACES:
            if allowed is not None and wid not in allowed:
                continue
            # A realistic spread: a couple of big production workspaces, a long
            # tail of small ones.
            flagged = sum(resource_spend.get(wid, {}).values())
            total = round(max(rng.uniform(4_000, 180_000), flagged * rng.uniform(2.5, 4.0)), 2)
            serverless_share = rng.uniform(0.05, 0.55)
            serverless = round(total * serverless_share, 2)
            splits = [
                ("All-purpose compute", rng.uniform(0.15, 0.40)),
                ("Jobs compute", rng.uniform(0.15, 0.35)),
                ("SQL Warehouse (pro)", rng.uniform(0.10, 0.25)),
                ("SQL Warehouse (serverless)", rng.uniform(0.05, 0.20)),
                ("Pipelines (DLT)", rng.uniform(0.03, 0.12)),
                ("Model serving", rng.uniform(0.01, 0.06)),
            ]
            scale = sum(w for _, w in splits)
            by_sku = [
                {
                    "sku_group": g,
                    "spend_usd": round(total * w / scale, 2),
                    "dbus": round(total * w / scale / 0.55, 2),
                }
                for g, w in splits
            ]
            by_sku.sort(key=lambda s: s["spend_usd"], reverse=True)
            # A daily series with a mild upward drift and weekday/weekend shape, so
            # the trend chart in demo mode looks like a real platform rather than a
            # flat line. Sums to `total` so the chart and the KPI agree.
            days = max(1, int(self.settings.lookback_days))
            weights = []
            for i in range(days):
                day = date.today() - timedelta(days=days - 1 - i)
                weekend = 0.55 if day.weekday() >= 5 else 1.0
                drift = 1.0 + 0.4 * (i / max(1, days - 1))  # gentle growth
                weights.append(weekend * drift * rng.uniform(0.85, 1.15))
            wsum = sum(weights) or 1.0
            by_day = [
                {
                    "usage_date": (
                        date.today() - timedelta(days=days - 1 - i)
                    ).isoformat(),
                    "spend_usd": round(total * w / wsum, 2),
                    "dbus": round(total * w / wsum / 0.55, 2),
                }
                for i, w in enumerate(weights)
            ]
            row_pricing: dict = {}
            if pricing_mode != "list":
                # Blended contract total: ~22 % off, less where part of the spend has
                # no contract price (contract_partial) — that part stays at list.
                # Hash-derived, NOT rng: drawing from rng would shift every later workspace.
                share = 1.0 if pricing_mode == "contract" else round(0.82 + _unit(wid) * 0.15, 4)
                row_pricing["total_spend_contract_usd"] = round(
                    total * share * 0.78 + total * (1 - share), 2
                )
                row_pricing["contract_priced_share"] = share
            if pricing_cloud:
                row_pricing["total_cloud_cost_usd"] = round((total - serverless) * 0.42, 2)
            rows.append({
                **row_pricing,
                "workspace_id": wid,
                "workspace_name": name,
                "lookback_days": self.settings.lookback_days,
                "total_spend_usd": total,
                "monthly_spend_usd": total,  # demo window == a month
                "total_dbus": round(total / 0.55, 2),
                "serverless_spend_usd": serverless,
                "classic_spend_usd": round(total - serverless, 2),
                "by_sku": by_sku,
                "by_day": by_day,
            })
        return rows


def _parse_sku(raw) -> list[SkuSpend]:
    """Parse the ``by_sku`` JSON column, tolerating nulls and bad rows."""
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    out: list[SkuSpend] = []
    for item in data or []:
        try:
            out.append(
                SkuSpend(
                    sku_group=str(item.get("sku_group") or "Other"),
                    spend_usd=float(item.get("spend_usd") or 0.0),
                    dbus=float(item.get("dbus") or 0.0),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _parse_days(raw) -> list[SpendDay]:
    """Parse the ``by_day`` JSON column, tolerating nulls and bad rows.

    Returns ``[]`` when the column is absent, which is the expected state for a
    snapshot written before the column existed — the UI then simply omits the
    trend rather than rendering a flat line at zero."""
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    out: list[SpendDay] = []
    for item in data or []:
        try:
            day = str(item.get("usage_date") or "")
            if not day:
                continue  # a point with no date cannot be placed on an axis
            out.append(
                SpendDay(
                    usage_date=day,
                    spend_usd=float(item.get("spend_usd") or 0.0),
                    dbus=float(item.get("dbus") or 0.0),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    out.sort(key=lambda d: d.usage_date)
    return out


def _opt_float(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _monthly(window_total, row: dict, lookback_days: int) -> float | None:
    """Window total -> 30-day month (same normalization as monthly_spend_usd)."""
    v = _opt_float(window_total)
    if v is None:
        return None
    days = int(row.get("lookback_days") or lookback_days) or 30
    return round(v * 30.0 / days, 2)


def _rollup(
    rows: list[dict],
    lookback_days: int,
    *,
    unavailable: bool,
    out_of_scope_workspaces: int = 0,
) -> AccountSpend:
    """Turn spend rows into per-workspace models + account totals."""
    workspaces: list[WorkspaceSpend] = []
    by_sku: dict[str, SkuSpend] = {}
    #: date -> summed spend/dbus across every workspace, for the account trend.
    by_day: dict[str, SpendDay] = {}
    for r in rows:
        skus = _parse_sku(r.get("by_sku"))
        days = _parse_days(r.get("by_day"))
        workspaces.append(
            WorkspaceSpend(
                workspace_id=str(r.get("workspace_id")),
                workspace_name=r.get("workspace_name"),
                lookback_days=int(r.get("lookback_days") or lookback_days),
                total_spend_usd=float(r.get("total_spend_usd") or 0.0),
                monthly_spend_usd=float(r.get("monthly_spend_usd") or 0.0),
                total_dbus=float(r.get("total_dbus") or 0.0),
                serverless_spend_usd=float(r.get("serverless_spend_usd") or 0.0),
                classic_spend_usd=float(r.get("classic_spend_usd") or 0.0),
                monthly_spend_contract_usd=_monthly(r.get("total_spend_contract_usd"), r, lookback_days),
                contract_priced_share=_opt_float(r.get("contract_priced_share")),
                monthly_cloud_cost_usd=_monthly(r.get("total_cloud_cost_usd"), r, lookback_days),
                by_sku=skus,
                by_day=days,
            )
        )
        for d in days:
            agg_day = by_day.get(d.usage_date)
            if agg_day is None:
                by_day[d.usage_date] = SpendDay(
                    usage_date=d.usage_date, spend_usd=d.spend_usd, dbus=d.dbus
                )
            else:
                agg_day.spend_usd += d.spend_usd
                agg_day.dbus += d.dbus
        for s in skus:
            agg = by_sku.get(s.sku_group)
            if agg is None:
                by_sku[s.sku_group] = SkuSpend(
                    sku_group=s.sku_group, spend_usd=s.spend_usd, dbus=s.dbus
                )
            else:
                agg.spend_usd += s.spend_usd
                agg.dbus += s.dbus

    workspaces.sort(key=lambda w: w.monthly_spend_usd, reverse=True)
    # Contract rollup: blended total over the workspaces that carry it, and the
    # list-$-weighted coverage share. Only when EVERY workspace carries it — a mix
    # would sum contract figures for some workspaces with nothing for others.
    contract_total = contract_share = cloud_total = None
    if workspaces and all(w.monthly_spend_contract_usd is not None for w in workspaces):
        contract_total = round(sum(w.monthly_spend_contract_usd for w in workspaces), 2)
        list_total = sum(w.monthly_spend_usd for w in workspaces)
        covered = sum(w.monthly_spend_usd * (w.contract_priced_share or 0.0) for w in workspaces)
        contract_share = round(covered / list_total, 4) if list_total > 0 else None
    cloud_ws = [w.monthly_cloud_cost_usd for w in workspaces if w.monthly_cloud_cost_usd is not None]
    if cloud_ws:
        cloud_total = round(sum(cloud_ws), 2)
    for s in by_sku.values():
        s.spend_usd = round(s.spend_usd, 2)
        s.dbus = round(s.dbus, 2)
    return AccountSpend(
        total_monthly_spend_usd=round(sum(w.monthly_spend_usd for w in workspaces), 2),
        total_serverless_spend_usd=round(sum(w.serverless_spend_usd for w in workspaces), 2),
        total_classic_spend_usd=round(sum(w.classic_spend_usd for w in workspaces), 2),
        total_monthly_spend_contract_usd=contract_total,
        contract_priced_share=contract_share,
        total_monthly_cloud_cost_usd=cloud_total,
        workspace_count=len(workspaces),
        out_of_scope_workspaces=out_of_scope_workspaces,
        lookback_days=lookback_days,
        unavailable=unavailable,
        workspaces=workspaces,
        by_sku=sorted(by_sku.values(), key=lambda s: s.spend_usd, reverse=True),
        by_day=[
            SpendDay(
                usage_date=d.usage_date,
                spend_usd=round(d.spend_usd, 2),
                dbus=round(d.dbus, 2),
            )
            for d in sorted(by_day.values(), key=lambda d: d.usage_date)
        ],
    )
