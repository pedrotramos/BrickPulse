"""Fleet-wide serverless evidence: the facts that aren't per-resource findings.

The rule engine answers "which resource should I change?". The About Serverless
page also needs account-shaped facts that no single rule owns:

* **DBR version spread** — how many distinct runtimes are in flight, and how much
  spend sits on old ones. The argument isn't "upgrade these clusters", it's "you
  are running N runtime versions and somebody has to keep all of them current".
* **Query start-up latency** — time queries spent waiting for compute to be
  available (``system.query.history.waiting_for_compute_duration_ms``). This is the user-
  visible cost of cold classic compute, and it is the thing admins get complaints
  about rather than a line on a bill.
* **Autoscaling lag** — how often clusters were resizing, i.e. how much time went
  into scaling rather than working.

Each metric degrades independently: if a schema isn't enabled or granted, that
metric is simply absent from the payload and the UI omits that card. A partial
picture is fine; a fabricated one is not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from brickpulse.rules.resolver import TableResolver

from ..config import Settings
from .pricing_sql import price_expr, price_join
from ..identity import Scope
from ..logging import get_logger

log = get_logger(__name__)

#: Fleet facts change slowly (daily at most) and the queries scan wide tables, so
#: cache longer than the spend source.
_CACHE_TTL_S = 900

#: Wall-clock budget for the whole fleet section. Kept comfortably under the SQL
#: connection's socket timeout (40s, see data/sql_client.py) so a slow account
#: loses a card or two instead of hanging the About Serverless page — which is
#: exactly what happened on the first deploy, when three ~10s queries ran serially.
_FLEET_BUDGET_S = 25.0


def _dbr_spread_sql(catalog: str, lookback_days: int, currency: str = "USD") -> str:
    """Distinct DBR versions in use + spend on each, newest-looking first.

    Uses the latest config row per cluster (like the rule SQL) joined to billed
    spend, so "old runtime" is weighted by money rather than by cluster count."""
    return f"""
WITH latest AS (
  SELECT workspace_id, cluster_id, dbr_version, cluster_source
  FROM {catalog}.compute.clusters
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
cost AS (
  SELECT u.workspace_id,
         u.usage_metadata.cluster_id AS cluster_id,
         SUM(u.usage_quantity * {price_expr()}) AS spend_usd
  FROM {catalog}.billing.usage u
  {price_join(TableResolver.nested(catalog), currency)}
  WHERE u.usage_date >= date_sub(current_date(), {lookback_days})
    AND u.usage_date < current_date()  -- closed-open window
    AND u.usage_metadata.cluster_id IS NOT NULL
  GROUP BY 1, 2
)
SELECT
  COALESCE(l.dbr_version, 'unknown') AS dbr_version,
  COUNT(*) AS cluster_count,
  round(SUM(COALESCE(c.spend_usd, 0)), 2) AS spend_usd
FROM latest l
LEFT JOIN cost c USING (workspace_id, cluster_id)
WHERE l.cluster_source IN ('UI', 'API', 'JOB')
GROUP BY 1
ORDER BY spend_usd DESC
"""


def _startup_latency_sql(catalog: str, lookback_days: int) -> str:
    """How long queries waited for compute to come up, and how often.

    ``waiting_for_compute_duration_ms`` is exactly the cold-start tax serverless removes;
    reporting p50/p95 alongside the affected query count keeps it honest (a big
    p95 on 3 queries is not an argument)."""
    return f"""
SELECT
  COUNT(*) AS total_queries,
  COUNT_IF(COALESCE(waiting_for_compute_duration_ms, 0) > 0) AS queries_waiting,
  round(SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 60000.0, 1) AS total_wait_minutes,
  round(percentile_approx(
    CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN waiting_for_compute_duration_ms END,
    0.5) / 1000.0, 1) AS p50_wait_s,
  round(percentile_approx(
    CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN waiting_for_compute_duration_ms END,
    0.95) / 1000.0, 1) AS p95_wait_s,
  round(MAX(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0, 1) AS max_wait_s
FROM {catalog}.query.history
WHERE start_time >= date_sub(current_date(), {lookback_days})
  AND start_time < current_date()  -- closed-open window
"""


def _autoscale_sql(catalog: str, lookback_days: int) -> str:
    """Minutes clusters spent with a changing node count — i.e. scaling, not working.

    Derived from node-count changes per cluster-minute in the node timeline: a
    proxy, but a directly-measured one, for how much autoscaling churn the estate
    carries today."""
    return f"""
WITH per_min AS (
  SELECT workspace_id, cluster_id,
         date_trunc('MINUTE', start_time) AS tm,
         COUNT(DISTINCT instance_id) AS nodes
  FROM {catalog}.compute.node_timeline
  WHERE start_time >= date_sub(current_date(), {lookback_days})
    AND start_time < current_date()  -- closed-open window
  GROUP BY 1, 2, 3
),
changes AS (
  SELECT workspace_id, cluster_id, tm, nodes,
         LAG(nodes) OVER (PARTITION BY workspace_id, cluster_id ORDER BY tm) AS prev
  FROM per_min
)
SELECT
  COUNT(DISTINCT cluster_id) AS clusters_scaling,
  COUNT_IF(prev IS NOT NULL AND nodes <> prev) AS resize_events,
  round(COUNT_IF(prev IS NOT NULL AND nodes <> prev) / 60.0, 1) AS resize_hours
FROM changes
"""


class FleetSource:
    """Account-level serverless evidence, each metric independently optional."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache: tuple[float, dict] | None = None

    def fleet_facts(self, scope: Scope) -> dict:
        """Fleet metrics for the caller's scope. Missing metrics are omitted.

        Note these are account/fleet-shaped aggregates; we do not attempt to scope
        them per workspace, because the argument they support ("you maintain N
        runtimes across the estate") is inherently fleet-level. Workspace admins
        therefore see the same fleet context as platform admins, which contains no
        per-resource detail.

        The three queries run CONCURRENTLY, one connection each. Each scans a wide
        system table and takes ~10s on a real account; run sequentially they add up
        past the SQL connection's socket timeout and the page hangs on
        "Analysing your estate…" (observed on the first deploy). Concurrency makes
        the wall-clock cost the slowest single query instead of their sum — the same
        pattern ``live_source`` already uses for rules."""
        now = time.monotonic()
        if self._cache and now - self._cache[0] < _CACHE_TTL_S:
            return self._cache[1]

        catalog = self.settings.catalog
        days = self.settings.lookback_days
        facts: dict = {"lookback_days": days}

        queries = {
            "dbr_spread": _dbr_spread_sql(catalog, days, self.settings.currency),
            "startup_latency": _startup_latency_sql(catalog, days),
            "autoscale": _autoscale_sql(catalog, days),
        }
        # Hard budget on the whole fleet section. These facts SUPPORT the serverless
        # case; they are not the case itself, so a slow account must degrade to
        # "fewer cards" rather than hanging the page. Anything not back in time is
        # dropped and picked up on a later request (the result is cached, so the
        # next visit after a warm-up usually has everything).
        results: dict[str, list[dict] | None] = dict.fromkeys(queries, None)
        pool = ThreadPoolExecutor(max_workers=len(queries))
        try:
            futures = {
                name: pool.submit(self._try, name, sql) for name, sql in queries.items()
            }
            deadline = time.monotonic() + _FLEET_BUDGET_S
            for name, fut in futures.items():
                remaining = deadline - time.monotonic()
                try:
                    results[name] = fut.result(timeout=max(0.0, remaining))
                except FuturesTimeout:
                    log.info(
                        "fleet metric %s exceeded the %ss budget; omitting it",
                        name, _FLEET_BUDGET_S,
                    )
        finally:
            # Don't block the response on stragglers; they finish in the background.
            pool.shutdown(wait=False, cancel_futures=True)

        dbr = results["dbr_spread"]
        if dbr:
            facts["dbr_versions"] = [
                {
                    "version": str(r.get("dbr_version")),
                    "cluster_count": int(r.get("cluster_count") or 0),
                    "spend_usd": float(r.get("spend_usd") or 0.0),
                }
                for r in dbr
            ]
            facts["distinct_dbr_versions"] = len(dbr)

        latency = results["startup_latency"]
        if latency:
            row = latency[0]
            total = int(row.get("total_queries") or 0)
            waiting = int(row.get("queries_waiting") or 0)
            facts["startup"] = {
                "total_queries": total,
                "queries_waiting": waiting,
                "waiting_share": round(waiting / total, 4) if total else 0.0,
                "total_wait_minutes": float(row.get("total_wait_minutes") or 0.0),
                "p50_wait_s": float(row.get("p50_wait_s") or 0.0),
                "p95_wait_s": float(row.get("p95_wait_s") or 0.0),
                "max_wait_s": float(row.get("max_wait_s") or 0.0),
            }

        autoscale = results["autoscale"]
        if autoscale:
            row = autoscale[0]
            facts["autoscale"] = {
                "clusters_scaling": int(row.get("clusters_scaling") or 0),
                "resize_events": int(row.get("resize_events") or 0),
                "resize_hours": float(row.get("resize_hours") or 0.0),
            }

        self._cache = (now, facts)
        return facts

    def _try(self, name: str, sql: str) -> list[dict] | None:
        """Run one metric query; a failure drops that metric, not the page."""
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            log.info("fleet metric %s unavailable; omitting from the serverless case", name)
            return None


class DemoFleetSource(FleetSource):
    """Deterministic fleet facts for demo mode — no connection, same shape."""

    def fleet_facts(self, scope: Scope) -> dict:
        return {
            "lookback_days": self.settings.lookback_days,
            "distinct_dbr_versions": 6,
            "dbr_versions": [
                {"version": "14.3.x-scala2.12", "cluster_count": 34, "spend_usd": 148_900.0},
                {"version": "13.3.x-scala2.12", "cluster_count": 21, "spend_usd": 92_400.0},
                {"version": "12.2.x-scala2.12", "cluster_count": 12, "spend_usd": 41_250.0},
                {"version": "11.3.x-scala2.12", "cluster_count": 7, "spend_usd": 18_700.0},
                {"version": "10.4.x-scala2.12", "cluster_count": 4, "spend_usd": 9_320.0},
                {"version": "custom:custom-image", "cluster_count": 2, "spend_usd": 3_100.0},
            ],
            "startup": {
                "total_queries": 412_508,
                "queries_waiting": 38_142,
                "waiting_share": 0.0925,
                "total_wait_minutes": 21_430.0,
                "p50_wait_s": 12.4,
                "p95_wait_s": 96.7,
                "max_wait_s": 402.1,
            },
            "autoscale": {
                "clusters_scaling": 46,
                "resize_events": 8_942,
                "resize_hours": 149.0,
            },
        }
