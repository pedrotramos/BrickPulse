"""Reads for the "Saved so far" figure. It comes from the snapshot only (no live fallback).

Built on the same pattern as :mod:`spend_source`, with one deliberate difference: there is
**no live fallback**. The figure is history, built day by day by the daily job's
``track_changes`` task (``precompute/change_tracker.py``). No system-table query can
reconstruct it on a page load, so with no tracker tables the honest answer is
``unavailable`` and the UI hides the tile. That covers live mode and a precompute
deployment before the job's first tracker run.

Three reads, all optional apart from the first:

* ``finops_savings_daily``: the booked figure per (day, spend scope). If it can't be
  read, the result is ``unavailable``. The tracker already applied the headline's
  ``book_scope`` dedup, so the API only sums it.
* ``finops_change_events``: explains the figure (full / partial / deleted, the
  example, what wasn't counted). If unreadable, the breakdown is empty and the dollars
  still show.
* ``finops_finding_ledger``: ``MIN(snapshot_date)`` = ``tracking_since``, the
  "since deployment on" date. If unreadable, it falls back to the first booked day.

Every column is read with ``.get`` from ``SELECT *``, so a table written by an older or
newer tracker still reads.

Hidden and experimental rules are excluded (:func:`aggregation.realized_totals`).
"""

from __future__ import annotations

from brickpulse.environment import get_env

import random
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from ..config import Settings
from ..identity import Scope
from ..logging import get_logger
from ..models.realized import RealizedSavings
from ..services.aggregation import _as_date, book_scope, realized_totals, scope_key
from .bounded_cache import MISS, BoundedCache
from .scoping import where_clause
from .sql_client import qualified_table

log = get_logger(__name__)

#: Cache TTL (seconds). The tracker runs once a day, so a few minutes of staleness costs
#: nothing, and the tile shows on every Overview and Workspace page. FINOPS_REALIZED_CACHE_TTL.
_CACHE_TTL_S = int(get_env("FINOPS_REALIZED_CACHE_TTL", "300") or "300")
_CACHE_MAX_ENTRIES = 512

#: The app's month (pricing.DAYS_PER_MONTH), the same /30 the tracker uses.
_DAYS_PER_MONTH = 30


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


class RealizedSource:
    """Reads "Saved so far" for the caller's scope and a date window."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache = BoundedCache(_CACHE_TTL_S, _CACHE_MAX_ENTRIES)

    # --- public ---------------------------------------------------------------
    def realized(
        self,
        scope: Scope,
        *,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> RealizedSavings:
        """Saved in ``window_start..window_end`` (default: ``tracking_since`` through
        yesterday, i.e. cumulative since deployment)."""
        reason = self._unavailable_reason()
        if reason:
            return RealizedSavings(unavailable=True, unavailable_reason=reason)
        default_end = _yesterday()
        if not scope.all_workspaces and not scope.workspace_ids:
            # An empty scope (e.g. a workspace admin naming a workspace they don't own)
            # stops BEFORE any query, so it never reads another workspace's history.
            return RealizedSavings(window_start=window_start, window_end=window_end or default_end)

        data = self._read_cached(scope)
        if data is None:
            return RealizedSavings(unavailable=True, unavailable_reason="no_tracker_tables")
        totals = realized_totals(
            data["daily"],
            data["events"],
            window_start=window_start,
            window_end=window_end,
            default_end=default_end,
            tracking_since=data.get("tracking_since"),
            data_through=data.get("data_through"),
            excluded_rule_ids=self._excluded_rule_ids(),
        )
        return RealizedSavings(**totals)

    # --- internals ------------------------------------------------------------
    def _unavailable_reason(self) -> str | None:
        if self.settings.compute_mode != "precompute":
            return "live_mode"
        return None

    def _excluded_rule_ids(self) -> frozenset[str]:
        return frozenset(self.settings.hidden_rule_ids) | frozenset(
            self.settings.experimental_rule_ids
        )

    def _read_cached(self, scope: Scope) -> dict | None:
        key = "all" if scope.all_workspaces else ",".join(sorted(map(str, scope.workspace_ids)))
        hit = self._cache.get(key, MISS)
        if hit is not MISS:
            return hit
        data = self._read(scope)
        self._cache.put(key, data)
        return data

    def _read(self, scope: Scope) -> dict | None:
        """Raw rows for the scope, or ``None`` when the daily table is unreadable."""
        clause = where_clause(scope)
        daily_t = qualified_table(self.settings.savings_daily_table)
        # data_through is the tracker's last booked day across the WHOLE table, taken
        # before the scope filter. That way a workspace whose bookings ended reads a
        # run-rate of 0 instead of its last value (see realized_totals).
        daily_sql = (
            f"SELECT * FROM (SELECT *, MAX(day) OVER () AS _data_through FROM {daily_t}) _d "
            f"{clause}"
        ).strip()
        try:
            daily = self._query(daily_sql)
        except Exception as exc:  # noqa: BLE001 - absent table / no grant: unavailable
            log.info("savings-daily table unavailable (%s): %s", daily_t, str(exc)[:200])
            return None

        events: list[dict] = []
        try:
            events_t = qualified_table(self.settings.change_events_table)
            events = self._query(f"SELECT * FROM {events_t} {clause}".strip())
        except Exception as exc:  # noqa: BLE001 - the dollars still show without it
            log.info("change-events table unreadable; no breakdown: %s", str(exc)[:200])

        tracking_since = None
        try:
            ledger_t = qualified_table(self.settings.finding_ledger_table)
            got = self._query(f"SELECT MIN(snapshot_date) AS tracking_since FROM {ledger_t}")
            tracking_since = _as_date(got[0].get("tracking_since")) if got else None
        except Exception as exc:  # noqa: BLE001 - falls back to the first booked day
            log.info("finding ledger unreadable; tracking_since from daily: %s", str(exc)[:200])

        data_through = max(
            (d for d in (_as_date(r.get("_data_through")) for r in daily) if d), default=None
        )
        return {
            "daily": daily,
            "events": events,
            "tracking_since": tracking_since,
            "data_through": data_through,
        }

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# =================================================================== demo
#: Seed for the demo's OWN random stream. The shared demo rng (DemoRecommendationSource,
#: seed 20260706) is never touched, so no other demo figure shifts.
_DEMO_SEED = 20261006

#: Days of tracking history the demo pretends to have (about three months).
_DEMO_HISTORY_DAYS = 80

#: The demo workspace that "goes away" during tracking (staging-sandbox). Its deletions
#: after it was retired are not counted, and its one earlier change stops accruing.
_DEMO_RETIRED_WORKSPACE = "9182736450192837"
_DEMO_RETIRED_AFTER_DAYS = 52

# One step = (rule_id, kind, setting, baseline, after, proposed, fraction, method, day).
# Several steps on one `chain` id hit the SAME finding, so 240 -> 60 -> 30 is one
# finding whose fraction rises twice, valued against the same estimate (as the tracker
# does). Fractions are the tracker's own methods, worked by hand:
#   linear  (240-60)/(240-10) = 0.7826 ; (240-30)/(240-10) = 0.9130
#   ladder  X_LARGE -> LARGE, proposed MEDIUM: (1-2^-1)/(1-2^-2) = 0.6667
#   linear, off counts as 1,440 min: (1440-120)/(1440-60) = 0.9565
_DEMO_SCRIPT: list[tuple] = [
    ("a", "warehouse_autostop", "toward", "auto_stop_minutes", "240", "60", "10", 0.7826, "linear", 18),
    ("a", "warehouse_autostop", "toward", "auto_stop_minutes", "240", "30", "10", 0.9130, "linear", 44),
    ("b", "warehouse_autostop_serverless", "reached", "auto_stop_minutes", "30", "10", "10", 1.0, "linear", 25),
    ("c", "warehouse_rightsize", "toward", "warehouse_size", "X_LARGE", "LARGE", "MEDIUM", 0.6667, "ladder", 30),
    ("d", "cluster_autotermination", "reached", "auto_termination_minutes", None, "60", "60", 1.0, "linear", 12),
    ("e", "cluster_autotermination", "toward", "auto_termination_minutes", None, "120", "60", 0.9565, "linear", 50),
    ("f", "enable_serverless", "reached", "warehouse_type", "PRO", "SERVERLESS", "SERVERLESS", 1.0, "binary", 35),
    ("g", "idle_zombie_clusters", "deleted", None, None, None, None, 1.0, "deletion", 8),
    ("h", "job_no_new_input", "paused", "paused", "false", "true", "true", 1.0, "binary", 28),
    ("i", "idle_serving_endpoints", "reached", "min_concurrency", "4", "0", "0", 1.0, "binary", 55),
    # Set, then moved back: booked for a while, then an `away` event worth 0.
    ("j", "warehouse_autostop_serverless", "reached", "auto_stop_minutes", "45", "10", "10", 1.0, "linear", 15),
    ("j", "warehouse_autostop_serverless", "away", "auto_stop_minutes", "45", "45", "10", 0.0, "linear", 48),
]
# In the retired workspace: one change booked before it went away, then two deletions
# after (not counted, workspace_retired).
_DEMO_RETIRED_SCRIPT: list[tuple] = [
    ("r1", None, "deleted", None, None, None, None, 1.0, "deletion", 10),
    ("r2", None, "deleted", None, None, None, None, 1.0, "deletion", 53),
    ("r3", None, "deleted", None, None, None, None, 1.0, "deletion", 53),
]


class DemoRealizedSource(RealizedSource):
    """Offline "Saved so far" for demo mode: about 15 deterministic change events on
    real demo findings, plus the daily rows the tracker would build from them.

    The daily rows come from the same loop the tracker runs (each change accrues from
    its day until the finding's next event, never in a retired workspace, one
    ``book_scope`` per spend pool and day). So the tile, the per-month list and the
    breakdown reconcile with each other.
    """

    def _unavailable_reason(self) -> str | None:
        return None

    def _read(self, scope: Scope) -> dict | None:
        events, retired, since = self._demo_events()
        daily = _demo_daily(events, retired, since, _yesterday())
        if not scope.all_workspaces:
            allowed = {str(w) for w in scope.workspace_ids}
            events = [e for e in events if e["workspace_id"] in allowed]
            daily = [r for r in daily if r["workspace_id"] in allowed]
        return {
            "daily": daily,
            "events": events,
            "tracking_since": since,
            "data_through": _yesterday(),
        }

    def _demo_events(self) -> tuple[list[dict], dict[str, date], date]:
        from .demo_source import DemoRecommendationSource

        excluded = self._excluded_rule_ids()
        recs = [
            r for r in DemoRecommendationSource(self.settings, None)._all()
            if r.savings_status != "advisory" and r.rule_id not in excluded
            and (r.monthly_savings_usd or 0) > 0
        ]
        recs.sort(key=lambda r: (r.rule_id, r.workspace_id, r.resource_id))
        rng = random.Random(_DEMO_SEED)
        since = date.today() - timedelta(days=_DEMO_HISTORY_DAYS)
        retired_on = since + timedelta(days=_DEMO_RETIRED_AFTER_DAYS)
        used: set[str] = set()
        chains: dict[str, object] = {}
        events: list[dict] = []

        def pick(pool, biggest=False):
            pool = [r for r in pool if f"{r.resource_type}|{r.resource_id}" not in used]
            if not pool:
                return None
            # The 240 -> 30 auto-stop chain takes the biggest auto-stop finding.
            # Everything else is drawn. (The tooltip's "e.g." is the largest partial
            # change overall; since phased rightsize findings lead with their floor
            # saving, that is the rightsize step, not this chain.)
            rec = max(pool, key=lambda r: r.monthly_savings_usd) if biggest else rng.choice(pool)
            used.add(f"{rec.resource_type}|{rec.resource_id}")
            return rec

        active = [r for r in recs if r.workspace_id != _DEMO_RETIRED_WORKSPACE]
        for chain, rule_id, *rest in _DEMO_SCRIPT:
            rec = chains.get(chain) or pick(
                [r for r in active if r.rule_id == rule_id], biggest=chain == "a"
            )
            if rec is None:
                continue
            chains[chain] = rec
            events.append(_demo_event(rec, since, *rest, counted=True, reason=None))

        in_retired = [r for r in recs if r.workspace_id == _DEMO_RETIRED_WORKSPACE]
        for chain, _rule, kind, *rest in _DEMO_RETIRED_SCRIPT:
            rec = pick(in_retired)
            if rec is None:
                continue
            day = rest[-1]
            after = since + timedelta(days=day) >= retired_on
            events.append(_demo_event(
                rec, since, kind, *rest,
                counted=not after, reason="workspace_retired" if after else None,
            ))
        return events, {_DEMO_RETIRED_WORKSPACE: retired_on}, since


def _demo_event(rec, since: date, kind, setting, baseline, after, proposed, fraction,
                method, day, *, counted: bool, reason: str | None) -> dict:
    changed = datetime.combine(since + timedelta(days=day), datetime.min.time()) + timedelta(
        hours=10
    )
    estimate = float(rec.monthly_savings_usd or 0.0)
    return {
        "event_id": f"demo-{rec.finding_key}-{day}",
        "finding_key": rec.finding_key,
        "changed_at": changed,
        "change_kind": kind,
        "setting": setting,
        "baseline_value": baseline,
        "after_value": after,
        "proposed_value": proposed,
        "fraction": fraction,
        "valuation_method": method,
        "estimate_before_usd": round(estimate, 2),
        "waste_before_usd": round(float(rec.monthly_waste_usd or 0.0), 2),
        "valued_monthly_usd": round(fraction * estimate, 2),
        "counted": counted,
        "not_counted_reason": reason,
        "rule_id": rec.rule_id,
        "workspace_id": rec.workspace_id,
        "resource_type": rec.resource_type,
        "resource_id": rec.resource_id,
        "resource_name": rec.resource_name,
        "first_seen": since,
        "reference_snapshot_date": since + timedelta(days=max(0, day - 1)),
        "affected_spend_scope": rec.affected_spend_scope,
        "savings_composition": rec.savings_composition,
        "recorded_on": since + timedelta(days=day + 1),
        "recorded_at": changed + timedelta(days=1),
    }


def _demo_daily(events: list[dict], retired: dict[str, date], since: date,
                end: date) -> list[dict]:
    """The tracker's daily rebuild, reduced to what the demo needs."""
    by_finding: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_finding[e["finding_key"]].append(e)
    per_day: dict[date, list] = defaultdict(list)
    for evs in by_finding.values():
        evs.sort(key=lambda e: e["changed_at"])
        for i, e in enumerate(evs):
            if not e["counted"] or e["change_kind"] == "away" or e["valued_monthly_usd"] <= 0:
                continue
            start = max(e["changed_at"].date(), since)
            stop = evs[i + 1]["changed_at"].date() - timedelta(days=1) if i + 1 < len(evs) else end
            gone = retired.get(e["workspace_id"])
            d = start
            while d <= min(stop, end):
                if gone is None or d < gone:
                    per_day[d].append(SimpleNamespace(
                        workspace_id=e["workspace_id"], resource_type=e["resource_type"],
                        resource_id=e["resource_id"],
                        affected_spend_scope=e["affected_spend_scope"],
                        savings_composition=e["savings_composition"],
                        monthly_waste_usd=max(e["waste_before_usd"], e["estimate_before_usd"]),
                        valued=e["valued_monthly_usd"], rule_id=e["rule_id"],
                        finding_key=e["finding_key"], kind=e["change_kind"],
                    ))
                d += timedelta(days=1)
    out = []
    for day in sorted(per_day):
        pools: dict[tuple[str, str], list] = defaultdict(list)
        for b in per_day[day]:
            pools[scope_key(b)].append(b)
        for (ws, pool), items in sorted(pools.items()):
            rate = book_scope(items, value=lambda b: b.valued)
            if rate <= 0:
                continue
            out.append({
                "day": day,
                "workspace_id": ws,
                "scope_key": pool,
                "rule_ids": sorted({b.rule_id for b in items}),
                "finding_keys": sorted({b.finding_key for b in items}),
                "saved_usd": round(rate / _DAYS_PER_MONTH, 4),
                "run_rate_monthly_usd": round(rate, 2),
                "change_kinds": sorted({b.kind for b in items}),
            })
    return out
