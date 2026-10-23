"""Databricks Apps inventory reads: snapshot first, live query as fallback.

Structurally a sibling of :mod:`genie_source` — same read order, same honest
``unavailable`` result, same short cache, same concurrent live path under one
wall-clock budget. Read that module first; the differences here are:

* **Three aggregates, not four** (inventory / by-size / daily).
* **The inventory query joins the audit log**, which is materially slower than
  ``billing.usage`` on a delta-sharing-backed workspace. It is the one most likely
  to miss the live budget, which is exactly why ``missing_sections`` exists: losing
  lifecycle context must not blank the whole tab.
* **No scoring, and no recommendations at all** — see :mod:`.apps_sql` for why the
  data cannot support them.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from ..config import Settings
from ..identity import Scope
from ..logging import get_logger
from ..models.apps import AppDay, AppInfo, AppsInventory, AppSizeGroup
from .apps_sql import apps_daily_sql, apps_inventory_sql, apps_size_summary_sql
from .bounded_cache import INCOMPLETE_TTL_S, MISS, BoundedCache
from .scoping import where_clause
from .sql_client import qualified_table

log = get_logger(__name__)

#: Cache TTL (seconds). The pipeline recomputes daily, so minutes of staleness are
#: irrelevant. Overridable via FINOPS_APPS_CACHE_TTL, like spend.

_CACHE_TTL_S = int(get_env("FINOPS_APPS_CACHE_TTL", "300") or "300")
_CACHE_MAX_APPS = 5000  # LRU bound by total apps cached

#: Wall-clock budget for the whole live read. Above the connection's 40s socket
#: timeout for the same reason as genie_source: the queries run concurrently so the
#: socket timeout already bounds each one, and a lower budget would only cancel
#: queries that were about to succeed.
_LIVE_BUDGET_S = 45

#: Rows shipped to the browser. The tab paginates, but there is no reason to send
#: thousands of apps — the aggregates carry the totals.
_TOP_APPS = 500

_SNAPSHOT_KINDS = ("inventory", "size", "daily")

_ANALYSIS_NOTE = (
    "No optimization recommendations are shown for apps, and that is a limit of the "
    "available data rather than an omission. system.access.audit records app "
    "lifecycle events only (create, deploy, start, stop, update, delete) — there is "
    "no request or page-view log for Databricks Apps in system tables. So whether "
    "anyone actually uses an app cannot be determined, and an \"idle app\" or "
    "\"rightsize this app\" recommendation would be a guess rather than a finding. "
    "The configuration and cost facts below are shown so you can make that call "
    "yourself: compare compute size against monthly cost, and check when an app was "
    "last deployed. If Databricks adds an app usage log, the rule would go in "
    "rules/finops_rules/catalog/ and gate on it the way idle_serving_endpoints gates "
    "on entity_matched — absence of traffic data must mean \"unknown\", never \"idle\"."
)

_DATA_BASIS = (
    "system.billing.usage (billing_origin_product = 'APPS') priced with "
    "system.billing.list_prices at the rate in effect at time of use. Apps bill "
    "under the generic PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_<REGION> SKU, so they "
    "cannot be selected by SKU name. Configuration is "
    "product_features.apps.compute_size; lifecycle is system.access.audit "
    "(service_name = 'apps'). Apps are always-on while running — there is no "
    "scale-to-zero — so days billed is a lifecycle signal, not an idleness one."
)


class AppsSource:
    """Reads the Databricks Apps inventory for the caller's scope."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache = BoundedCache(_CACHE_TTL_S, _CACHE_MAX_APPS, lambda inv: len(inv.apps) + 1)

    # --- public ---------------------------------------------------------------
    def apps_inventory(self, scope: Scope) -> AppsInventory:
        # Keyed on the scope, so the account-wide view and each per-workspace
        # drill-in cache separately and can never serve each other's rows.
        if scope.all_workspaces:
            key = "all"
        elif not scope.workspace_ids:
            key = "none"
        else:
            key = ",".join(sorted(scope.workspace_ids))
        hit = self._cache.get(key, MISS)
        if hit is not MISS:
            return hit

        data = self._from_snapshot(scope)
        lookback = self.settings.lookback_days
        if data is None:
            data, lookback = self._from_live(scope)
        inv = _rollup(data or {}, lookback_days=lookback, unavailable=data is None)
        # A section that missed the live budget is absent from `data`: retry it soon.
        incomplete = data is None or any(k not in data for k in _SNAPSHOT_KINDS)
        self._cache.put(key, inv, INCOMPLETE_TTL_S if incomplete else None)
        return inv

    # --- reads ----------------------------------------------------------------
    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        """Latest snapshot rows per kind, or ``None`` if the table is unusable."""
        if self.settings.compute_mode != "precompute":
            return None
        table = qualified_table(self.settings.apps_table)
        clause = where_clause(scope)
        scoped = ("AND " + clause[len("WHERE "):]) if clause else ""
        # workspace_id/name/url are REAL columns, deliberately kept out of the JSON
        # payload so scoping never parses JSON — and they must therefore be selected
        # explicitly and merged back in. Reading only (kind, payload) silently drops
        # every workspace attribution; that exact bug shipped on the Genie tab.
        sql = (
            f"SELECT kind, workspace_id, workspace_name, workspace_url, payload "
            f"FROM {table} "
            f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {table}) {scoped}"
        )
        try:
            rows = self._query(sql)
        except Exception:
            log.info("apps snapshot unavailable (%s); falling back to live", table)
            return None
        if not rows:
            return None
        out: dict[str, list[dict]] = {k: [] for k in _SNAPSHOT_KINDS}
        for r in rows:
            kind = str(r.get("kind") or "")
            if kind not in out:
                continue
            parsed = _loads(r.get("payload"))
            if isinstance(parsed, dict):
                for col in ("workspace_id", "workspace_name", "workspace_url"):
                    if r.get(col) is not None:
                        parsed[col] = r[col]
                out[kind].append(parsed)
        return out if any(out.values()) else None

    def _from_live(self, scope: Scope) -> tuple[dict[str, list[dict]] | None, int]:
        """Run the three aggregates concurrently under one budget.

        Returns ``(data, lookback_days)`` — the window is truncated here and the
        caller needs the real value to label the page and normalize to a month.

        A query that fails or misses the budget drops only its own section. The
        inventory query is the slow one (it joins the audit log), so this is not a
        theoretical concern: losing it should cost the table, not the whole tab.

        The window is truncated to ``live_lookback_days``, like the Genie live read.
        """
        lookback = max(1, min(self.settings.lookback_days, self.settings.live_lookback_days))
        builders = {
            "inventory": apps_inventory_sql,
            "size": apps_size_summary_sql,
            "daily": apps_daily_sql,
        }
        if not scope.all_workspaces and not scope.workspace_ids:
            return {k: [] for k in builders}, lookback

        out: dict[str, list[dict]] = {}
        # Not a `with` block: exiting the context manager joins every worker, so a
        # straggler would hold the response open long past the budget. Same pattern
        # and same hard-won reason as genie_source / fleet_source.
        pool = ThreadPoolExecutor(max_workers=len(builders))
        try:
            futures = {
                kind: pool.submit(
                    self._query,
                    self._scoped(
                        fn(
                            self.settings.catalog,
                            lookback,
                            self.settings.currency,
                        ),
                        scope,
                    ),
                )
                for kind, fn in builders.items()
            }
            deadline = time.monotonic() + _LIVE_BUDGET_S
            for kind, fut in futures.items():
                try:
                    out[kind] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
                except FutureTimeout:
                    log.warning(
                        "apps live read: %s exceeded the %ss budget; dropping",
                        kind, _LIVE_BUDGET_S,
                    )
                except Exception as exc:
                    log.warning("apps live read failed for %s (%s); dropping", kind, exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if not out:
            return None, lookback
        return out, lookback

    def _scoped(self, sql: str, scope: Scope) -> str:
        """Apply the caller's scope to an account-wide query, as the rules do."""
        clause = where_clause(scope)
        return f"SELECT * FROM (\n{sql}\n) _apps {clause}".strip()

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


class DemoAppsSource(AppsSource):
    """Offline apps inventory for demo mode.

    Derived from the shared demo workspace list so the figures reconcile with the
    rest of the demo data, and shaped like a real account: mostly MEDIUM, a handful
    of LARGE, most apps always-on, a minority with recent deploys.
    """

    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        return None

    def _from_live(self, scope: Scope) -> tuple[dict[str, list[dict]] | None, int]:
        import random
        from datetime import date, timedelta

        from .demo_source import _WORKSPACES

        rng = random.Random(20260806)
        allowed = None if scope.all_workspaces else set(scope.workspace_ids)
        workspaces = [w for w in _WORKSPACES if allowed is None or w[0] in allowed]
        lookback = self.settings.lookback_days
        if not workspaces:
            return {k: [] for k in _SNAPSHOT_KINDS}, lookback

        names = [
            "sales-cockpit", "exec-dashboard", "churn-explorer", "inventory-monitor",
            "support-triage", "pricing-simulator", "data-catalog-ui", "ops-runbook",
            "forecast-review", "quality-scorecard",
        ]
        # One event stream, three aggregates derived from it — the same discipline the
        # Genie demo source uses, so the KPI can't contradict the chart beside it.
        apps: list[dict] = []
        # _WORKSPACES entries are (id, name) — no URL, so the demo leaves
        # workspace_url unset and the UI falls back to its own host, exactly as it
        # does for demo recommendations.
        for wid, wname in workspaces[:6]:
            for i in range(rng.randint(1, 4)):
                size = "LARGE" if rng.random() < 0.15 else "MEDIUM"
                rate = 1.0 if size == "MEDIUM" else 2.0
                days = lookback if rng.random() < 0.55 else rng.randint(3, lookback - 1)
                dbus = round(days * 23.5 * rate, 2)
                deployed = rng.random() < 0.4
                apps.append({
                    "workspace_id": wid, "workspace_name": wname,
                    "app_id": f"demo-{wid}-{i}",
                    "app_name": f"{names[(int(wid) + i) % len(names)]}",
                    "compute_size": size, "size_changes": 1,
                    "days_billed": days, "observed_days": days,
                    "first_seen_date": str(date.today() - timedelta(days=days)),
                    "last_billed_date": str(date.today() - timedelta(days=1)),
                    "dbus": dbus, "spend_usd": dbus,
                    "monthly_spend_usd": round(dbus * 30.0 / lookback, 2),
                    "last_deploy_time": (
                        str(date.today() - timedelta(days=rng.randint(1, 40)))
                        if deployed else None
                    ),
                    "last_start_time": None, "last_stop_time": None,
                    "deploys": rng.randint(1, 20) if deployed else 0,
                    "last_deployed_by": "demo.user@example.com" if deployed else None,
                })

        sizes: dict[str, dict] = {}
        for a in apps:
            g = sizes.setdefault(a["compute_size"], {
                "compute_size": a["compute_size"], "apps": 0, "dbus": 0.0,
                "spend_usd": 0.0, "monthly_spend_usd": 0.0,
            })
            g["apps"] += 1
            g["dbus"] += a["dbus"]
            g["spend_usd"] += a["spend_usd"]
            g["monthly_spend_usd"] += a["monthly_spend_usd"]

        daily = []
        for d in range(lookback, 0, -1):
            day = date.today() - timedelta(days=d)
            active = [a for a in apps if a["days_billed"] >= d]
            daily.append({
                "usage_date": str(day),
                "apps": len(active),
                "dbus": round(sum(23.5 * (2.0 if a["compute_size"] == "LARGE" else 1.0)
                                  for a in active), 2),
                "spend_usd": round(sum(23.5 * (2.0 if a["compute_size"] == "LARGE" else 1.0)
                                       for a in active), 2),
            })

        return {"inventory": apps, "size": list(sizes.values()), "daily": daily}, lookback


# --- helpers ------------------------------------------------------------------
def _loads(raw):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _rollup(
    data: dict[str, list[dict]], *, lookback_days: int, unavailable: bool
) -> AppsInventory:
    """Turn raw aggregate rows into the tab's model. No scoring, no verdicts."""
    notes = dict(
        analysis_note=_ANALYSIS_NOTE,
        data_basis=_DATA_BASIS,
        lookback_days=lookback_days,
    )
    if unavailable:
        return AppsInventory(unavailable=True, **notes)

    apps = [
        AppInfo(
            app_id=str(r.get("app_id") or ""),
            app_name=r.get("app_name"),
            workspace_id=(str(r["workspace_id"]) if r.get("workspace_id") else None),
            workspace_name=r.get("workspace_name"),
            workspace_url=r.get("workspace_url"),
            compute_size=r.get("compute_size"),
            size_changes=_i(r.get("size_changes")) or 1,
            dbus=_f(r.get("dbus")),
            spend_usd=_f(r.get("spend_usd")),
            monthly_spend_usd=_f(r.get("monthly_spend_usd")),
            days_billed=_i(r.get("days_billed")),
            observed_days=_i(r.get("observed_days")),
            first_seen_date=_s(r.get("first_seen_date")),
            last_billed_date=_s(r.get("last_billed_date")),
            last_deploy_time=_s(r.get("last_deploy_time")),
            last_start_time=_s(r.get("last_start_time")),
            last_stop_time=_s(r.get("last_stop_time")),
            deploys=_i(r.get("deploys")),
            last_deployed_by=r.get("last_deployed_by"),
        )
        for r in (data.get("inventory") or [])
        if r.get("app_id")
    ]
    apps.sort(key=lambda a: a.monthly_spend_usd, reverse=True)

    by_size = [
        AppSizeGroup(
            compute_size=str(r.get("compute_size") or "UNKNOWN"),
            apps=_i(r.get("apps")),
            dbus=_f(r.get("dbus")),
            spend_usd=_f(r.get("spend_usd")),
            monthly_spend_usd=_f(r.get("monthly_spend_usd")),
        )
        for r in (data.get("size") or [])
    ]
    # The size aggregate is per (workspace, size); merge to one row per size so the
    # account view shows the estate's configuration mix rather than 300 rows.
    merged: dict[str, AppSizeGroup] = {}
    for g in by_size:
        m = merged.get(g.compute_size)
        if m is None:
            merged[g.compute_size] = g
            continue
        m.apps += g.apps
        m.dbus += g.dbus
        m.spend_usd += g.spend_usd
        m.monthly_spend_usd += g.monthly_spend_usd
    by_size = sorted(merged.values(), key=lambda g: g.spend_usd, reverse=True)

    # Daily arrives per (workspace, date); sum across workspaces per date.
    per_day: dict[str, AppDay] = {}
    for r in (data.get("daily") or []):
        day = _s(r.get("usage_date"))
        if not day:
            continue
        d = per_day.get(day)
        if d is None:
            per_day[day] = AppDay(
                usage_date=day, apps=_i(r.get("apps")),
                dbus=_f(r.get("dbus")), spend_usd=_f(r.get("spend_usd")),
            )
            continue
        # App counts per workspace are disjoint (an app lives in one workspace), so
        # summing them across workspaces for a date is exact.
        d.apps += _i(r.get("apps"))
        d.dbus += _f(r.get("dbus"))
        d.spend_usd += _f(r.get("spend_usd"))
    daily = [per_day[k] for k in sorted(per_day)]

    # The window total comes from the daily series when we have it (it covers every
    # app, including any trimmed from the table), else from the inventory rows.
    if daily:
        total_spend = sum(d.spend_usd for d in daily)
        total_dbus = sum(d.dbus for d in daily)
    else:
        total_spend = sum(a.spend_usd for a in apps)
        total_dbus = sum(a.dbus for a in apps)

    workspace_ids = {a.workspace_id for a in apps if a.workspace_id}
    app_count = len(apps)
    if by_size and not app_count:
        # The inventory query missed the budget but the size rollup arrived; the
        # count is still knowable and more useful than 0.
        app_count = sum(g.apps for g in by_size)

    missing = [k for k in _SNAPSHOT_KINDS if not (data.get(k) or [])]

    return AppsInventory(
        total_spend_usd=round(total_spend, 2),
        monthly_spend_usd=round(total_spend * 30.0 / max(1, lookback_days), 2),
        total_dbus=round(total_dbus, 2),
        app_count=app_count,
        workspace_count=len(workspace_ids),
        always_on_count=sum(1 for a in apps if a.days_billed >= lookback_days),
        no_usage=not apps and not by_size and not daily,
        missing_sections=missing,
        apps=apps[:_TOP_APPS],
        by_size=by_size,
        daily=daily,
        **notes,
    )


def _s(v) -> str | None:
    """Stringify a date/timestamp for the API without inventing a format."""
    if v is None:
        return None
    text = str(v).strip()
    return text or None
