"""Probe the system tables the engine reads, and explain what is missing.

One ``SELECT * FROM <table> LIMIT 0`` per source table. That resolves the table
and enforces grants without scanning data, so probing all 18 costs far less than
a single rule query.

Two design points worth stating, because both were tempting to get wrong:

* **Probes run CONCURRENTLY, under a wall-clock budget.** 18 serial round-trips
  to a cold warehouse would blow past the SQL connection's 40s socket timeout and
  hang the page — the same failure that already bit ``fleet_source`` when three
  ~10s queries ran serially. Anything not back in time is reported as UNKNOWN
  rather than as broken.
* **The result is cached.** Grants change on the order of weeks, so re-probing on
  every page load would add latency for information that almost never moves.

The report says nothing about whether a table has *rows*. Emptiness is a
data-recency question; treating it as an availability failure would flag every
freshly-created workspace as broken.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone

from brickpulse.rules import get_enabled_rules, load_thresholds
from brickpulse.rules.contract import RuleContext
from brickpulse.rules.sources import (
    CORE_TABLES,
    TableHealth,
    probe_sql,
    rule_dependencies,
    unavailable_rules,
)

from ..config import Settings
from ..logging import get_logger
from ..data.sql_client import qualified_table
from ..models.health import HealthReport, LastRun, RuleAvailability, SourceHealth

log = get_logger(__name__)

#: Health changes when an admin changes a grant — weeks, not minutes.
_CACHE_TTL_S = 900

#: Wall-clock budget for the whole probe sweep, kept under the connection's 40s
#: socket timeout (see data/sql_client.py) so a cold warehouse degrades to
#: "unknown" instead of hanging the request.
_BUDGET_S = 20.0

#: Probes are tiny and independent; the cap keeps us from opening 18 warehouse
#: connections at once, which is its own kind of rude.
_MAX_WORKERS = 6


class HealthService:
    """Builds a :class:`HealthReport` for the configured catalog."""

    def __init__(self, settings: Settings, connect):
        self.settings = settings
        self.connect = connect
        self._cache: tuple[float, HealthReport] | None = None

    # --- dependency graph ---------------------------------------------------

    def _dependencies(self) -> dict[str, set[str]]:
        """rule_id -> the schema-qualified tables it reads.

        Derived from each rule's rendered SQL (see ``finops_rules.sources``) so it
        cannot drift from what the rules actually query."""
        thresholds = load_thresholds()
        catalog = thresholds.get("catalog", self.settings.catalog)
        deps: dict[str, set[str]] = {}
        for rule in get_enabled_rules(thresholds):
            ctx = RuleContext(
                thresholds=thresholds.get(rule.id, {}),
                phasing=thresholds.get("phasing"),  # engine-wide phased-downsizing block
                lookback_days=self.settings.lookback_days,
                catalog=catalog,
                currency=self.settings.currency,
            )
            deps[rule.id] = rule_dependencies(rule, ctx)
        return deps

    # --- probing ------------------------------------------------------------

    def _probe(self, catalog: str, table: str) -> TableHealth:
        """Probe one table. A failed QUERY is the finding, not an error to
        propagate: "cannot read this" is exactly what we came to establish.

        A failure to CONNECT is different in kind and is re-raised. If the
        warehouse is unreachable or the credentials are wrong, every probe fails
        for one shared reason that has nothing to do with grants — reporting that
        as 14 separate unreadable tables would send an admin hunting for
        permissions they already have. :meth:`report` catches it and says
        "unknown" once instead."""
        required = table in CORE_TABLES
        conn_cm = self.connect()  # outside the try: a connect failure must escape
        try:
            with conn_cm as conn, conn.cursor() as cur:
                cur.execute(probe_sql(f"{catalog}.{table}"))
                cur.fetchall()
            return TableHealth(name=table, available=True, required=required)
        except Exception as exc:  # noqa: BLE001 - the message is the payload
            return TableHealth(
                name=table,
                available=False,
                required=required,
                message=str(exc)[:500],
            )

    def _last_run(self) -> LastRun | None:
        """Summarize the most recent precompute update from the pipeline event log.

        Only meaningful in precompute mode — live mode computes on demand, so there
        is no run to report and this returns ``None`` rather than an empty card.

        The event log is a generic event stream, so the shape is pinned here:
        ``flow_progress`` events carry a terminal ``status`` (COMPLETED / FAILED)
        per flow, and the recommendations flow's event carries the row count. We
        scope to the LATEST ``origin.update_id`` so a previous night's failures
        cannot be reported as today's.

        Any read failure is reported as ``unavailable`` rather than raised: no
        run-log table is the expected state on a fresh install, and the health
        endpoint must not fail because the optional extra is missing.
        """
        if self.settings.compute_mode != "precompute":
            return None
        table = qualified_table(self.settings.run_log_table)
        sql = f"""
        WITH latest AS (
          SELECT origin.update_id AS update_id
          FROM {table}
          WHERE origin.update_id IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (ORDER BY timestamp DESC) = 1
        ),
        flows AS (
          SELECT
            e.origin.flow_name AS flow_name,
            get_json_object(e.details, '$.flow_progress.status') AS status,
            CAST(get_json_object(
              e.details, '$.flow_progress.metrics.num_output_rows'
            ) AS BIGINT) AS rows_out,
            e.timestamp AS ts
          FROM {table} e
          JOIN latest l ON e.origin.update_id = l.update_id
          WHERE e.event_type = 'flow_progress'
            AND get_json_object(e.details, '$.flow_progress.status')
                IN ('COMPLETED', 'FAILED')
        )
        SELECT
          MAX(ts) AS finished_at,
          SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS flows_ok,
          SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS flows_failed,
          -- Names of the failed flows only; the reader needs to know WHICH rule is
          -- missing from the numbers on screen.
          array_join(array_compact(collect_set(
            CASE WHEN status = 'FAILED' THEN flow_name END
          )), ',') AS failed_flows,
          MAX(CASE WHEN flow_name LIKE '%finops_recommendations' THEN rows_out END)
            AS rows_written
        FROM flows
        """
        try:
            rows = self._query(sql)
        except Exception:
            log.info("run log unavailable (%s); omitting last-run summary", table)
            return LastRun(unavailable=True)
        if not rows:
            return LastRun(unavailable=True)

        row = rows[0]
        failed = int(row.get("flows_failed") or 0)
        raw_failed = row.get("failed_flows") or ""
        return LastRun(
            finished_at=str(row["finished_at"]) if row.get("finished_at") else None,
            ok=failed == 0,
            flows_ok=int(row.get("flows_ok") or 0),
            flows_failed=failed,
            # Strip the catalog.schema prefix: the flow's own name is what maps to a
            # rule, and the qualifier is the same on every row.
            failed_flows=sorted(
                f.split(".")[-1] for f in raw_failed.split(",") if f
            ),
            rows_written=(
                int(row["rows_written"]) if row.get("rows_written") is not None else None
            ),
        )

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def report(self) -> HealthReport:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < _CACHE_TTL_S:
            return self._cache[1]

        thresholds = load_thresholds()
        catalog = thresholds.get("catalog", self.settings.catalog)
        deps = self._dependencies()
        tables = sorted({t for needed in deps.values() for t in needed} | set(CORE_TABLES))

        checked_at = datetime.now(timezone.utc).isoformat()
        base = HealthReport(
            checked_at=checked_at,
            catalog=catalog,
            compute_mode=self.settings.compute_mode,
        )

        # Demo mode has no warehouse at all, and reporting 18 FAILs for a mode that
        # is working exactly as intended would be a lie in the other direction.
        if self.settings.compute_mode == "demo":
            base.message = "Demo mode: sample data, no system tables are read."
            self._cache = (now, base)
            return base

        results: dict[str, TableHealth] = {}
        connect_error: Exception | None = None
        pool = ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(1, len(tables))))
        try:
            futures = {t: pool.submit(self._probe, catalog, t) for t in tables}
            deadline = time.monotonic() + _BUDGET_S
            for table, fut in futures.items():
                try:
                    results[table] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
                except FuturesTimeout:
                    log.warning("health probe for %s exceeded the budget", table)
                except Exception as exc:  # could not even open a connection
                    connect_error = exc
        finally:
            # Do not wait: a straggling probe must not hold the response open past
            # the budget. Same reason as genie_source / fleet_source.
            pool.shutdown(wait=False)

        if not results:
            # Nothing came back. Either the connection itself is unusable or every
            # probe timed out; both mean we know nothing, and claiming the sources
            # are broken would send an admin chasing grants they already have.
            base.unknown = True
            detail = f" ({connect_error})" if connect_error else ""
            base.message = (
                f"Could not check data-source access{detail}. Findings may be incomplete."
            )
            self._cache = (now, base)
            return base

        healthy = {name for name, h in results.items() if h.available}
        blocked = unavailable_rules(deps, healthy)

        sources = []
        for table in tables:
            health = results.get(table)
            if health is None:
                # Probe timed out: unknown, not failed. Reported as WARN with an
                # explicit message so it is never mistaken for a grant problem.
                sources.append(
                    SourceHealth(
                        name=table, status="WARN", severity="INFO",
                        required=table in CORE_TABLES,
                        message="Access check timed out; status unknown.",
                    )
                )
                continue
            sources.append(
                SourceHealth(
                    name=table,
                    status=health.status,
                    severity=health.severity,
                    required=health.required,
                    message=health.message,
                    blocks_rules=sorted(
                        rid for rid, missing in blocked.items() if table in missing
                    ),
                )
            )

        base.last_run = self._last_run()
        base.sources = sources
        base.unavailable_rules = [
            RuleAvailability(rule_id=rid, missing_sources=sorted(missing))
            for rid, missing in sorted(blocked.items())
        ]
        base.degraded = any(s.status in ("FAIL", "WARN") and s.message for s in sources)
        if base.failing_required:
            base.message = (
                "Core billing tables are unreadable: "
                + ", ".join(base.failing_required)
                + ". Grant SELECT on system.billing to see any findings."
            )
        elif blocked:
            base.message = (
                f"{len(blocked)} rule(s) cannot run because some system tables are "
                "not readable. Findings below are incomplete."
            )

        self._cache = (now, base)
        return base
