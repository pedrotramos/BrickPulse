"""Cost anomaly reads: snapshot first, live query as fallback.

Structurally a sibling of :mod:`apps_source` — same read order, same honest
``unavailable`` result, same short cache, same concurrent live path under one
wall-clock budget. Read that module first; the differences here are:

* **Two aggregates** (``anomaly`` rows at both grains, and the ``daily`` trend).
* **The window is the method's, not the app's.** Detection needs 28 baseline days +
  7 evaluation days, so the live path does NOT truncate to ``live_lookback_days``
  the way the other tabs do — a 3-day window can't have a 28-day baseline. On a
  large account the anomaly query therefore takes longer than the live budget
  (~1.5 min measured on a ~4k-workspace account); it then drops, and the tab says
  the snapshot is needed rather than reporting "no anomalies".
* **No scoring, no recommendations** — an anomaly is not a saving; see
  :mod:`.anomaly_sql`.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, timedelta

from ..config import Settings
from ..identity import Scope
from ..logging import get_logger
from ..models.anomaly import AnomalyDay, AnomalyTypeCount, CostAnomalies, CostAnomaly
from .anomaly_sql import (
    BASELINE_DAYS,
    EVAL_DAYS,
    MIN_DELTA_SHARE,
    MIN_DELTA_USD,
    Z_THRESHOLD,
    anomalies_sql,
    anomaly_daily_sql,
)
from .bounded_cache import INCOMPLETE_TTL_S, MISS, BoundedCache
from .scoping import where_clause
from .sql_client import qualified_table

log = get_logger(__name__)

#: Cache TTL (seconds); the pipeline recomputes daily. FINOPS_ANOMALY_CACHE_TTL.
_CACHE_TTL_S = int(get_env("FINOPS_ANOMALY_CACHE_TTL", "300") or "300")
_CACHE_MAX_ENTRIES = 512  # entries are small (~0.1 MB), so a plain entry cap

#: Wall-clock budget for the live read — same value and reasoning as apps_source.
_LIVE_BUDGET_S = 45

#: Rows shipped per grain. Sorted by excess, so the tail is the smallest movements.
_TOP_ANOMALIES = 300

_SNAPSHOT_KINDS = ("anomaly", "daily")

_TYPES = ("step_change", "spike", "new_spender")

_ANALYSIS_NOTE = (
    "An anomaly is not a saving, so nothing on this tab has a savings figure or feeds "
    "any total. A spike can be a backfill, a quarter-end run or a launch; only someone "
    "who knows the workload can say whether it is waste. What is shown is how far each "
    "cost moved from its own recent normal, when, and which identity drove it — so the "
    "right person can be asked."
)

_METHOD_NOTE = (
    f"Each day of the last {EVAL_DAYS} days is compared with the {BASELINE_DAYS} days "
    "before it (days without usage count as $0). A day is unusual when it sits more "
    f"than {Z_THRESHOLD:g} robust standard deviations above the baseline median AND at "
    f"least ${MIN_DELTA_USD:,.0f} above it (or {MIN_DELTA_SHARE:.0%} of the workspace's "
    "median daily spend, whichever is larger). The spread uses the median absolute "
    "deviation and the upper quartile, so a resource that normally runs every few days "
    "isn't flagged for running. Step change = 3 or more unusual days in a row; spike = "
    "shorter; new spender = no spend at all in the baseline. Today is excluded because "
    "it is incomplete."
)

_DATA_BASIS = (
    "system.billing.usage priced with system.billing.list_prices at the rate in "
    "effect at time of use (list price, not contract). Resources are keyed by "
    "usage_metadata (job before cluster, since a job cluster gets a new id every run); "
    "the identity is identity_metadata.run_as / owned_by of the largest usage record "
    "on the peak day. Names come from compute.warehouses / compute.clusters / "
    "lakeflow.jobs / lakeflow.pipelines where billing carries none."
)


class AnomalySource:
    """Reads cost anomalies for the caller's scope."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache = BoundedCache(_CACHE_TTL_S, _CACHE_MAX_ENTRIES)

    # --- public ---------------------------------------------------------------
    def cost_anomalies(self, scope: Scope) -> CostAnomalies:
        # Keyed on the scope, so the platform view and each drill-in can never serve
        # each other's rows.
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
        if data is None:
            data = self._from_live(scope)
        out = _rollup(data or {}, unavailable=data is None)
        # A section that missed the live budget is absent from `data`: retry it soon.
        incomplete = data is None or any(k not in data for k in _SNAPSHOT_KINDS)
        self._cache.put(key, out, INCOMPLETE_TTL_S if incomplete else None)
        return out

    # --- reads ----------------------------------------------------------------
    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        """Latest snapshot rows per kind, or ``None`` if the table is unusable."""
        if self.settings.compute_mode != "precompute":
            return None
        table = qualified_table(self.settings.anomaly_table)
        clause = where_clause(scope)
        scoped = ("AND " + clause[len("WHERE "):]) if clause else ""
        # workspace_id/name/url are REAL columns kept out of the payload so scoping
        # never parses JSON; select them explicitly and merge them back (reading only
        # (kind, payload) is the bug that once shipped on the Genie tab).
        sql = (
            f"SELECT kind, workspace_id, workspace_name, workspace_url, payload "
            f"FROM {table} "
            f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {table}) {scoped}"
        )
        try:
            rows = self._query(sql)
        except Exception:
            log.info("anomaly snapshot unavailable (%s); falling back to live", table)
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

    def _from_live(self, scope: Scope) -> dict[str, list[dict]] | None:
        """Run both aggregates concurrently under one budget. A query that fails or
        misses the budget drops only its own section; see the module docstring for
        why the window is not truncated here."""
        builders = {"anomaly": anomalies_sql, "daily": anomaly_daily_sql}
        if not scope.all_workspaces and not scope.workspace_ids:
            return {k: [] for k in builders}

        out: dict[str, list[dict]] = {}
        # Not a `with` block: exiting it joins every worker, so a straggler would
        # hold the response open past the budget (genie_source / fleet_source).
        pool = ThreadPoolExecutor(max_workers=len(builders))
        try:
            futures = {
                kind: pool.submit(
                    self._query,
                    self._scoped(
                        fn(
                            self.settings.catalog,
                            self.settings.lookback_days,
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
                        "anomaly live read: %s exceeded the %ss budget; dropping",
                        kind, _LIVE_BUDGET_S,
                    )
                except Exception as exc:
                    log.warning("anomaly live read failed for %s (%s); dropping", kind, exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        return out or None

    def _scoped(self, sql: str, scope: Scope) -> str:
        """Apply the caller's scope to an account-wide query, as the rules do."""
        clause = where_clause(scope)
        return f"SELECT * FROM (\n{sql}\n) _anom {clause}".strip()

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


class DemoAnomalySource(AnomalySource):
    """Offline anomalies for demo mode.

    Built from synthetic daily series run through :func:`detect` — the same rule the
    SQL applies — so every demo anomaly is one the real method would flag, and the
    trend chart is the sum of the same series (the Genie/Apps demo discipline: one
    event stream, every aggregate derived from it).
    """

    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        return None

    def _from_live(self, scope: Scope) -> dict[str, list[dict]] | None:
        import random

        from .demo_source import _WORKSPACES

        rng = random.Random(20260923)
        allowed = None if scope.all_workspaces else set(scope.workspace_ids)
        workspaces = [w for w in _WORKSPACES if allowed is None or w[0] in allowed]
        if not workspaces:
            return {k: [] for k in _SNAPSHOT_KINDS}

        n = BASELINE_DAYS + EVAL_DAYS
        today = date.today()
        dates = [str(today - timedelta(days=n - i)) for i in range(n)]

        # (resource_type, name, product, sku, shape)
        catalog = [
            ("warehouse", "bi-prod-warehouse", "SQL", "PREMIUM_SERVERLESS_SQL_COMPUTE", "step"),
            ("job", "nightly_customer_360", "JOBS", "PREMIUM_JOBS_SERVERLESS_COMPUTE", "spike"),
            ("serving_endpoint", "support-agent-llm", "MODEL_SERVING",
             "PREMIUM_SERVERLESS_REAL_TIME_INFERENCE", "new"),
            ("cluster", "ds-exploration", "ALL_PURPOSE", "PREMIUM_ALL_PURPOSE_COMPUTE", "step"),
            ("job", "weekly_backfill", "JOBS", "PREMIUM_JOBS_COMPUTE", "periodic"),
            ("pipeline", "orders_bronze_silver", "DLT", "PREMIUM_DLT_CORE_COMPUTE", "flat"),
            ("warehouse", "adhoc-analytics", "SQL", "PREMIUM_SQL_PRO_COMPUTE", "flat"),
            ("app", "sales-cockpit", "APPS", "PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE", "flat"),
        ]
        people = ["ana.silva@example.com", "sp-etl-prod", "li.wei@example.com",
                  "m.jones@example.com"]
        rows: list[dict] = []
        ws_totals: dict[tuple[str, str], float] = {}
        for wi, (wid, wname) in enumerate(workspaces[:6]):
            for ci, (rtype, name, product, sku, shape) in enumerate(catalog):
                # Only the first couple of workspaces get the anomalous shapes, so the
                # tab looks like a real account: a handful of events, not one per row.
                if shape not in ("flat", "periodic") and (wi + ci) % 3 != 0:
                    shape = "flat"
                base = rng.uniform(80, 900)
                series = _demo_series(rng, shape, base, n)
                for d, c in zip(dates, series):
                    ws_totals[(wid, d)] = ws_totals.get((wid, d), 0.0) + c
                rid = f"demo-{rtype}-{wid}-{ci}"
                who = people[(wi + ci) % len(people)]
                for grain in ("resource", "sku"):
                    hit = detect(series)
                    if not hit:
                        continue
                    rows.append({
                        "workspace_id": wid, "workspace_name": wname, "grain": grain,
                        "anomaly_key": f"{grain}|{wid}|{rid}",
                        "product": product,
                        "sku_name": sku if grain == "sku" else None,
                        "resource_type": rtype if grain == "resource" else None,
                        "resource_id": rid if grain == "resource" else None,
                        "resource_name": name if grain == "resource" else None,
                        "series_json": json.dumps([round(v, 2) for v in series]),
                        "run_as": who if not who.startswith("sp-") else None,
                        "owned_by": who if who.startswith("sp-") else None,
                        **{k: (dates[v] if k.endswith("_date") else v)
                           for k, v in hit.items()},
                    })
        daily = [
            {"workspace_id": wid, "usage_date": d, "cost_usd": round(c, 2)}
            for (wid, d), c in sorted(ws_totals.items(), key=lambda kv: kv[0][1])
        ]
        return {"anomaly": rows, "daily": daily}


def _demo_series(rng, shape: str, base: float, n: int) -> list[float]:
    """A plausible daily cost series of the given shape (last EVAL_DAYS = recent)."""
    eval_start = n - EVAL_DAYS
    out = []
    for i in range(n):
        noise = rng.uniform(0.9, 1.1)
        if shape == "periodic":
            v = base * 4 * noise if i % 7 == 3 else 0.0
        elif shape == "new":
            v = 0.0 if i < n - 4 else base * 2.5 * noise
        elif shape == "step":
            v = base * noise * (3.2 if i >= eval_start + 2 else 1.0)
        elif shape == "spike":
            v = base * noise * (6.0 if i == n - 3 else 1.0)
        else:
            v = base * noise
        out.append(v)
    return out


def detect(
    series: list[float],
    *,
    eval_days: int = EVAL_DAYS,
    z_threshold: float = Z_THRESHOLD,
    min_delta_usd: float = MIN_DELTA_USD,
    step_min_days: int = 3,
) -> dict | None:
    """Python mirror of the SQL rule for ONE key (workspace-relative floor ignored).

    Used by the demo source so demo anomalies are ones the real method flags, and by
    the tests to pin the method's behaviour on known shapes. Returns the anomaly's
    fields (dates as series indices) or ``None``.
    """
    import statistics

    base = series[: len(series) - eval_days]
    if not base:
        return None
    med = statistics.median(base)
    mad = statistics.median(abs(v - med) for v in base)
    q = statistics.quantiles(base, n=4, method="inclusive")[2] if len(base) > 1 else med
    sigma = max(1.4826 * mad, (q - med) / 0.6745, 0.1 * med, 1.0)
    base_total = sum(base)

    def dow_median(i: int) -> float:
        # Same position mod 7 = same weekday (the series is consecutive days).
        return statistics.median([base[j] for j in range(len(base)) if (i - j) % 7 == 0])

    def excess(i: int) -> float:
        return series[i] - max(med, dow_median(i))

    flagged = []
    for i in range(len(base), len(series)):
        c = series[i]
        if base_total == 0:
            ok = c >= min_delta_usd
        else:
            ok = (c - med) / sigma > z_threshold and excess(i) >= min_delta_usd
        if ok:
            flagged.append(i)
    if not flagged:
        return None
    longest, cur = 1, 1
    for a, b in zip(flagged, flagged[1:]):
        cur = cur + 1 if b == a + 1 else 1
        longest = max(longest, cur)
    if base_total == 0:
        kind = "new_spender"
    elif longest >= step_min_days:
        kind = "step_change"
    else:
        kind = "spike"
    peak = max(flagged, key=lambda i: series[i])
    return {
        "anomaly_type": kind,
        "first_flag_date": flagged[0],
        "last_flag_date": flagged[-1],
        "peak_date": peak,
        "flagged_days": len(flagged),
        "longest_run": longest,
        "peak_cost_usd": round(series[peak], 2),
        "baseline_median_usd": round(med, 2),
        "baseline_mad_usd": round(mad, 2),
        "baseline_active_days": sum(1 for v in base if v > 0),
        "robust_z": None if base_total == 0 else round(max(
            (series[i] - med) / sigma for i in flagged), 1),
        "excess_usd": round(sum(
            series[i] if base_total == 0 else excess(i) for i in flagged), 2),
        "floor_usd": min_delta_usd,
        "eval_cost_usd": round(sum(series[len(base):]), 2),
    }


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


def _s(v) -> str | None:
    if v is None:
        return None
    text = str(v).strip()
    return text or None


def _series(raw) -> list[float]:
    """``series_json`` arrives as a JSON string (live and snapshot alike); tolerate a
    list too, since a snapshot payload may carry it already parsed."""
    val = _loads(raw) if not isinstance(raw, list) else raw
    if not isinstance(val, list):
        return []
    return [_f(v) for v in val]


def _anomaly(r: dict) -> CostAnomaly:
    z = r.get("robust_z")
    return CostAnomaly(
        grain=str(r.get("grain") or ""),
        anomaly_type=str(r.get("anomaly_type") or ""),
        anomaly_key=str(r.get("anomaly_key") or ""),
        workspace_id=(str(r["workspace_id"]) if r.get("workspace_id") else None),
        workspace_name=r.get("workspace_name"),
        workspace_url=r.get("workspace_url"),
        product=r.get("product"),
        sku_name=r.get("sku_name"),
        resource_type=r.get("resource_type"),
        resource_id=_s(r.get("resource_id")),
        resource_name=r.get("resource_name"),
        first_flag_date=_s(r.get("first_flag_date")),
        last_flag_date=_s(r.get("last_flag_date")),
        peak_date=_s(r.get("peak_date")),
        flagged_days=_i(r.get("flagged_days")),
        longest_run=_i(r.get("longest_run")),
        peak_cost_usd=_f(r.get("peak_cost_usd")),
        baseline_median_usd=_f(r.get("baseline_median_usd")),
        baseline_mad_usd=_f(r.get("baseline_mad_usd")),
        baseline_active_days=_i(r.get("baseline_active_days")),
        robust_z=None if z is None else _f(z),
        excess_usd=_f(r.get("excess_usd")),
        floor_usd=_f(r.get("floor_usd")),
        eval_cost_usd=_f(r.get("eval_cost_usd")),
        series=_series(r.get("series_json", r.get("series"))),
        run_as=r.get("run_as"),
        owned_by=r.get("owned_by"),
    )


def _rollup(data: dict[str, list[dict]], *, unavailable: bool) -> CostAnomalies:
    """Turn raw rows into the tab's model. No scoring, no verdicts."""
    notes = dict(
        analysis_note=_ANALYSIS_NOTE,
        method_note=_METHOD_NOTE,
        data_basis=_DATA_BASIS,
        baseline_days=BASELINE_DAYS,
        eval_days=EVAL_DAYS,
        z_threshold=Z_THRESHOLD,
        min_delta_usd=MIN_DELTA_USD,
        min_delta_share=MIN_DELTA_SHARE,
    )
    today = date.today()
    notes["eval_start"] = str(today - timedelta(days=EVAL_DAYS))
    notes["eval_end"] = str(today - timedelta(days=1))
    if unavailable:
        return CostAnomalies(unavailable=True, **notes)

    rows = [_anomaly(r) for r in (data.get("anomaly") or []) if r.get("anomaly_type")]
    rows.sort(key=lambda a: a.excess_usd, reverse=True)
    resource = [a for a in rows if a.grain == "resource"]
    sku = [a for a in rows if a.grain == "sku"]

    # Totals come from the SKU grain: it covers every dollar exactly once, while a
    # resource anomaly is usually the same event seen at a finer grain.
    by_type = []
    for t in _TYPES:
        hits = [a for a in sku if a.anomaly_type == t]
        by_type.append(AnomalyTypeCount(
            anomaly_type=t, count=len(hits),
            excess_usd=round(sum(a.excess_usd for a in hits), 2),
        ))

    per_day: dict[str, float] = {}
    for r in (data.get("daily") or []):
        day = _s(r.get("usage_date"))
        if day:
            per_day[day] = per_day.get(day, 0.0) + _f(r.get("cost_usd"))
    daily = [AnomalyDay(usage_date=k, cost_usd=round(per_day[k], 2)) for k in sorted(per_day)]
    if daily:
        # Label the window from the data, not today: a snapshot computed yesterday
        # scored the 7 days ending the day before IT ran.
        try:
            end = date.fromisoformat(daily[-1].usage_date[:10])
            notes["eval_end"] = str(end)
            notes["eval_start"] = str(end - timedelta(days=EVAL_DAYS - 1))
        except ValueError:
            pass

    missing = [k for k in _SNAPSHOT_KINDS if k not in data]
    workspace_ids = {a.workspace_id for a in rows if a.workspace_id}
    return CostAnomalies(
        total_excess_usd=round(sum(a.excess_usd for a in sku), 2),
        anomaly_count=len(sku),
        workspace_count=len(workspace_ids),
        by_type=by_type,
        no_anomalies="anomaly" in data and not rows,
        missing_sections=missing,
        resource_anomalies=resource[:_TOP_ANOMALIES],
        sku_anomalies=sku[:_TOP_ANOMALIES],
        daily=daily,
        **notes,
    )
