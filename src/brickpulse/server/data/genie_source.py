"""Genie cost reads: precomputed snapshot first, live query as fallback.

Structurally a sibling of :mod:`spend_source` — same read order, same honest
``unavailable`` result, same short cache — because it answers the same *kind* of
question (what did this cost?) from the same billing table.

Read order, and why:

1. **The snapshot table** (``finops_genie_usage``, written by the SDP pipeline).
   One cheap scoped SELECT; the production path.
2. **Live queries** over ``system.billing.usage`` when the snapshot is missing or
   empty — a fresh deployment before the first pipeline run, and live/demo dev.
3. **Unavailable**, if both fail (e.g. no grant on ``system.billing``), reported
   as such so the UI omits the figures instead of showing a confident $0.

The four live queries are independent, so they run **concurrently** under one
wall-clock budget, the way :mod:`fleet_source` does. Serial execution is what made
the About Serverless page hang once before: each query is seconds on a real
account and the connector's socket timeout is 40s.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

# Aliased to the builtin TimeoutError from Python 3.11, but imported explicitly so
# the except clause is correct on older runtimes too.
from concurrent.futures import TimeoutError as FutureTimeout

from ..config import Settings
from ..identity import Scope
from ..logging import get_logger
from ..models.genie import (
    SURFACE_LABELS,
    GenieCosts,
    GenieDay,
    GenieFreeTier,
    GenieSpace,
    GenieSurface,
    GenieSurfaceUse,
    GenieUser,
    GenieWorkspace,
)
from .bounded_cache import INCOMPLETE_TTL_S, MISS, BoundedCache
from .genie_sql import (
    genie_daily_sql,
    genie_space_sql,
    genie_surface_sql,
    genie_user_sql,
)
from .scoping import where_clause
from .sql_client import qualified_table

log = get_logger(__name__)

#: Cache TTL. The pipeline recomputes daily, so minutes of staleness are
#: irrelevant while re-scanning billing.usage per page view is not free. Overridable
#: via FINOPS_GENIE_CACHE_TTL (as spend/apps).
import os as _os

_CACHE_TTL_S = int(get_env("FINOPS_GENIE_CACHE_TTL", "300") or "300")
_CACHE_MAX_ROWS = 5000  # LRU bound by spaces + workspaces + users cached

#: Wall-clock budget for the whole live read: return what arrived rather than
#: blocking the tab. Set ABOVE the connection's socket timeout (40s, see
#: data/sql_client.py) on purpose — the four queries run concurrently, so the
#: socket timeout already bounds each one. A budget *below* it (fleet_source uses
#: 25s, but its queries are individually fast) would cancel queries that were
#: about to succeed, which is exactly what happened on the first live run here:
#: all four aggregates were dropped and the tab reported "unavailable" against an
#: account that had plenty of Genie usage.
_LIVE_BUDGET_S = 45

#: How many rows the UI actually shows. Everything else is aggregate, so there is
#: no reason to ship thousands of principals to the browser.
_TOP_USERS = 25
_TOP_SPACES = 25

_SNAPSHOT_KINDS = ("user", "surface", "space", "daily")

_BUDGETS_NOTE = (
    "Set budgets are not shown because Genie usage is not attributed to a budget "
    "policy: usage_metadata.budget_policy_id is NULL on every Genie billing row, "
    "and account budget policies are not readable from a workspace-scoped app. "
    "Genie spend is therefore tracked here per user and per space instead."
)

_SPACE_COVERAGE_NOTE = (
    "Per-space costs cover Genie Spaces only. Genie Code and Genie One usage "
    "carries no space id in billing.usage, so their spend appears in the surface "
    "breakdown but cannot be attributed to a space."
)

_DATA_BASIS = (
    "system.billing.usage (billing_origin_product = 'GENIE', netting all record "
    "types so retractions and restatements correct the total) priced with "
    "system.billing.list_prices at the rate in effect at time "
    "of use. The free tier is the separately-billed GENIE_FREE_USAGE SKU, which "
    "has no list price and therefore costs exactly $0 — the free/paid split is "
    "read from billing, not estimated. Users are identity_metadata.run_as; "
    "spaces are usage_metadata.genie.agent_id."
)


class GenieSource:
    """Reads Genie cost data for the caller's scope."""

    def __init__(self, settings: Settings, connect: Callable):
        self.settings = settings
        self.connect = connect
        self._cache = BoundedCache(
            _CACHE_TTL_S, _CACHE_MAX_ROWS,
            lambda c: len(c.spaces) + len(c.workspaces) + len(c.top_users) + 1,
        )

    # --- public ---------------------------------------------------------------
    def genie_costs(self, scope: Scope) -> GenieCosts:
        # Keyed on the scope, so the account-wide view and each per-workspace
        # drill-in cache separately and can never serve each other's rows. The
        # "none" arm keeps an empty scope (a denied workspace, or a scoped user with
        # no workspaces) from sharing a key with anything else.
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
        # The live path truncates its window, so it reports back which window it
        # actually used. Every figure on the tab is window-dependent (and the
        # monthly normalization divides by it), so reporting the configured 30 days
        # for a 7-day read would mislabel the whole page and inflate nothing but
        # the reader's confidence.
        lookback = self.settings.lookback_days
        if data is None:
            data, lookback = self._from_live(scope)
        costs = _rollup(
            data or {},
            lookback_days=lookback,
            unavailable=data is None,
        )
        # A section that missed the live budget is absent from `data`: retry it soon.
        incomplete = data is None or any(k not in data for k in _SNAPSHOT_KINDS)
        self._cache.put(key, costs, INCOMPLETE_TTL_S if incomplete else None)
        return costs

    # --- reads ----------------------------------------------------------------
    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        """Latest snapshot rows per kind, or ``None`` if the table is unusable."""
        if self.settings.compute_mode != "precompute":
            return None
        table = qualified_table(self.settings.genie_table)
        clause = where_clause(scope)
        scoped = ("AND " + clause[len("WHERE "):]) if clause else ""
        # workspace_id/name/url are REAL columns, deliberately kept out of the JSON
        # payload (the pipeline lifts workspace_id out so scoping never parses JSON —
        # see ddl/finops_genie_usage.sql). They must therefore be selected explicitly
        # and merged back in below: reading only (kind, payload) silently drops every
        # workspace attribution, which showed up as "across 0 workspaces" and unnamed
        # spaces on the tab.
        sql = (
            f"SELECT kind, workspace_id, workspace_name, workspace_url, payload "
            f"FROM {table} "
            f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {table}) {scoped}"
        )
        try:
            rows = self._query(sql)
        except Exception:
            # A missing table is the expected case before the first pipeline run.
            log.info("genie snapshot unavailable (%s); falling back to live", table)
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
                # The row's own columns win: the payload never carried workspace_id,
                # and name/url are resolved at write time from workspaces_latest.
                for col in ("workspace_id", "workspace_name", "workspace_url"):
                    if r.get(col) is not None:
                        parsed[col] = r[col]
                out[kind].append(parsed)
        return out if any(out.values()) else None

    def _from_live(self, scope: Scope) -> tuple[dict[str, list[dict]] | None, int]:
        """Run the four aggregates concurrently under one budget.

        Returns ``(data, lookback_days)`` — the window is truncated here, and the
        caller needs the real value to label the page and normalize to a month.

        A single query that fails or misses the budget drops only its own section
        (e.g. no per-space table) instead of failing the whole tab.

        The window is truncated to ``live_lookback_days``, following the same rule as
        ``LiveRecommendationSource``: this path scans ``billing.usage`` synchronously
        while someone waits on a page load, and the full 30-day account-wide scan of
        ``system.*`` does not finish in that time. Precompute mode keeps the full
        window, so the snapshot the tab normally reads is not truncated.
        """
        lookback = max(1, min(self.settings.lookback_days, self.settings.live_lookback_days))
        builders = {
            "user": genie_user_sql,
            "surface": genie_surface_sql,
            "space": genie_space_sql,
            "daily": genie_daily_sql,
        }
        if not scope.all_workspaces and not scope.workspace_ids:
            return {k: [] for k in builders}, lookback

        out: dict[str, list[dict]] = {}
        # Not a `with` block: exiting the context manager joins every worker, so a
        # straggler would hold the response open long past the budget however the
        # results are collected. `shutdown(wait=False)` is what actually makes the
        # budget a budget — the same pattern (and the same hard-won reason) as
        # fleet_source.fleet_facts.
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
                        "genie live read: %s exceeded the %ss budget; dropping",
                        kind, _LIVE_BUDGET_S,
                    )
                except Exception as exc:
                    log.warning("genie live read failed for %s (%s); dropping", kind, exc)
        finally:
            # Don't block the response on stragglers; they finish in the background.
            pool.shutdown(wait=False, cancel_futures=True)
        # Every query failing (typically no grant on system.billing) is genuinely
        # "unavailable"; a partial result is still worth rendering.
        return (out or None), lookback

    def _scoped(self, sql: str, scope: Scope) -> str:
        if scope.all_workspaces:
            return sql
        ids = ", ".join(f"'{w}'" for w in scope.workspace_ids)
        return f"SELECT * FROM ({sql}) WHERE workspace_id IN ({ids})"

    def _query(self, sql: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class DemoGenieSource(GenieSource):
    """Offline Genie data for demo mode — never opens a connection.

    Shaped to match what the live account actually looks like (most users never
    leave the free tier; most paid spend is Genie Code even though most *users* are
    on Spaces), so the tab demos honestly rather than showing a flat distribution.

    Built by generating one set of synthetic **usage events** and then deriving all
    four aggregates from it — the same way the four SQL queries all derive from
    ``billing.usage``. Generating each aggregate independently is the obvious
    shortcut and it is wrong: the surface totals then disagree with the headline
    total and the DBU counts don't add up, which reads as a bug in the app rather
    than in the fixture.
    """

    def _from_snapshot(self, scope: Scope) -> dict[str, list[dict]] | None:
        return None

    def _from_live(self, scope: Scope) -> tuple[dict[str, list[dict]] | None, int]:
        import random
        from collections import defaultdict
        from datetime import date, timedelta

        from .demo_source import _WORKSPACES

        rng = random.Random(20260805)
        allowed = None if scope.all_workspaces else set(scope.workspace_ids)
        workspaces = [w for w in _WORKSPACES if allowed is None or w[0] in allowed]
        if not workspaces:
            return {k: [] for k in _SNAPSHOT_KINDS}, self.settings.lookback_days

        first_names = [
            "ana", "ben", "chen", "dara", "elif", "farid", "greta", "hugo", "iris",
            "jonas", "kira", "liam", "mira", "noor", "omar", "pia", "rafa", "sten",
            "tara", "umut", "vera", "wout", "yara", "zane",
        ]
        # Weights reproduce the observed shape: Genie Code carries most of the PAID
        # DBUs while Spaces has the most users.
        surfaces = [
            ("GENIE_CODE", "UI", 8.0),
            ("GENIE_AGENTS", "UI", 1.4),
            ("GENIE_AGENTS", "API", 1.2),
            ("GENIE_ONE", "UI", 1.0),
        ]
        window = max(1, self.settings.lookback_days)
        price = 0.07  # $/DBU, the live serverless-inference rate

        # --- one event stream, four aggregates derived from it -------------------
        events: list[dict] = []
        for w_index, (wid, _wname) in enumerate(workspaces[:6]):
            # Each workspace has a handful of Genie Spaces; only GENIE_AGENTS usage
            # is attributed to one, exactly as in billing.usage.
            space_ids = [f"01f{rng.getrandbits(60):015x}" for _ in range(rng.randint(4, 9))]
            for i in range(rng.randint(18, 34)):
                # Unique across the account: the rollup merges on principal, so a
                # repeated name would fuse two demo users — and fusing a free-tier
                # user into a paid one distorts the free-tier headline.
                name = (
                    f"{first_names[i % len(first_names)]}"
                    f"{w_index}{i // len(first_names)}@example.com"
                )
                # ~3 in 4 users never leave the free allowance, as observed live.
                free_only = rng.random() < 0.77
                for _ in range(rng.randint(1, 14)):
                    surface, channel, weight = rng.choices(
                        surfaces, weights=[w for *_, w in surfaces]
                    )[0]
                    day = date.today() - timedelta(days=rng.randrange(window))
                    # Adoption ramps up towards today rather than being flat noise.
                    age = (date.today() - day).days
                    ramp = 1.4 - 0.8 * (age / window)
                    is_free = free_only or rng.random() < 0.42
                    dbus = round(rng.uniform(2, 40) * (1.0 if is_free else weight) * ramp, 4)
                    events.append({
                        "workspace_id": wid, "principal": name, "surface": surface,
                        "channel": channel, "is_free": is_free, "usage_date": str(day),
                        "dbus": dbus, "spend_usd": 0.0 if is_free else dbus * price,
                        "space_id": rng.choice(space_ids) if surface == "GENIE_AGENTS" else None,
                    })

        def _agg(key_fn):
            out: dict = defaultdict(lambda: defaultdict(float))
            for e in events:
                k = key_fn(e)
                if k is None:
                    continue
                a = out[k]
                a["dbus"] += e["dbus"]
                a["spend_usd"] += e["spend_usd"]
                a["free_dbus"] += e["dbus"] if e["is_free"] else 0.0
                a["paid_dbus"] += 0.0 if e["is_free"] else e["dbus"]
                a["api_dbus"] += e["dbus"] if e["channel"] == "API" else 0.0
                a.setdefault("_days", set()).add(e["usage_date"])  # type: ignore[arg-type]
                a.setdefault("_users", set()).add(e["principal"])  # type: ignore[arg-type]
                a.setdefault("_surfaces", {})
                sk = (e["surface"], e["channel"])
                a["_surfaces"][sk] = a["_surfaces"].get(sk, 0.0) + e["dbus"]  # type: ignore[index]
            return out

        users = [
            {
                "workspace_id": wid, "principal": principal,
                "free_dbus": round(a["free_dbus"], 2), "paid_dbus": round(a["paid_dbus"], 2),
                "spend_usd": round(a["spend_usd"], 2),
                "active_days": len(a["_days"]),
                "last_active_date": max(a["_days"]),
                "by_surface": [
                    {"surface": s, "channel": c, "dbus": round(d, 2)}
                    for (s, c), d in sorted(a["_surfaces"].items(), key=lambda kv: -kv[1])
                ],
            }
            for (wid, principal), a in _agg(lambda e: (e["workspace_id"], e["principal"])).items()
        ]

        surface_rows = [
            {
                "workspace_id": wid, "surface": s, "channel": c, "is_free": is_free,
                "dbus": round(a["dbus"], 2), "spend_usd": round(a["spend_usd"], 2),
                "users": len(a["_users"]),
            }
            for (wid, s, c, is_free), a in _agg(
                lambda e: (e["workspace_id"], e["surface"], e["channel"], e["is_free"])
            ).items()
        ]

        spaces = [
            {
                "workspace_id": wid, "space_id": space_id,
                "dbus": round(a["dbus"], 2), "spend_usd": round(a["spend_usd"], 2),
                "users": len(a["_users"]), "active_days": len(a["_days"]),
                "api_dbus": round(a["api_dbus"], 2), "last_active_date": max(a["_days"]),
            }
            for (wid, space_id), a in _agg(
                lambda e: (e["workspace_id"], e["space_id"]) if e["space_id"] else None
            ).items()
        ]

        daily = [
            {
                "workspace_id": wid, "usage_date": day,
                "free_dbus": round(a["free_dbus"], 2), "paid_dbus": round(a["paid_dbus"], 2),
                "spend_usd": round(a["spend_usd"], 2), "users": len(a["_users"]),
            }
            for (wid, day), a in _agg(lambda e: (e["workspace_id"], e["usage_date"])).items()
        ]

        # Demo generates the full configured window, so it reports it unchanged.
        return {
            "user": users, "surface": surface_rows, "space": spaces, "daily": daily,
        }, self.settings.lookback_days


# --- rollup -------------------------------------------------------------------
def _loads(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
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


def _monthly(total: float, lookback_days: int) -> float:
    """Normalize a window total to a 30-day month, as the spend rollup does."""
    if lookback_days <= 0:
        return round(total, 2)
    return round(total * 30.0 / lookback_days, 2)


def _surface_uses(raw) -> list[GenieSurfaceUse]:
    out: list[GenieSurfaceUse] = []
    for item in _loads(raw) or []:
        if not isinstance(item, dict):
            continue
        out.append(
            GenieSurfaceUse(
                surface=str(item.get("surface") or "UNKNOWN"),
                channel=str(item.get("channel") or "UNKNOWN"),
                dbus=_f(item.get("dbus")),
            )
        )
    out.sort(key=lambda s: s.dbus, reverse=True)
    return out


def _rollup(
    data: dict[str, list[dict]], *, lookback_days: int, unavailable: bool
) -> GenieCosts:
    """Turn raw aggregate rows into the tab's model.

    Users arrive per (workspace, principal) but a person may work across several
    workspaces, so they are merged on the principal before ranking — otherwise one
    heavy user would appear as several mid-sized ones.
    """
    notes = dict(
        space_coverage_note=_SPACE_COVERAGE_NOTE,
        budgets_note=_BUDGETS_NOTE,
        data_basis=_DATA_BASIS,
        lookback_days=lookback_days,
    )
    if unavailable:
        return GenieCosts(unavailable=True, **notes)

    # --- users (merged across workspaces) -----------------------------------
    # A person may work across several workspaces, so users are merged on the
    # principal for the leaderboard. The per-workspace rollup, however, must be
    # built from the un-merged rows below (each is one workspace+principal): that
    # is the only aggregate whose distinct-user count and spend per workspace are
    # both exact, since a user active in two workspaces is one row in each.
    merged: dict[str, GenieUser] = {}
    ws: dict[str, GenieWorkspace] = {}
    workspace_ids: set[str] = set()
    for r in data.get("user") or []:
        principal = str(r.get("principal") or "unknown")
        wid = r.get("workspace_id")
        if wid:
            workspace_ids.add(str(wid))
            w = ws.get(str(wid))
            if w is None:
                w = ws[str(wid)] = GenieWorkspace(
                    workspace_id=str(wid), workspace_name=r.get("workspace_name")
                )
            elif w.workspace_name is None and r.get("workspace_name"):
                w.workspace_name = r["workspace_name"]
            r_free = _f(r.get("free_dbus"))
            r_paid = _f(r.get("paid_dbus"))
            w.free_dbus += r_free
            w.paid_dbus += r_paid
            w.spend_usd += _f(r.get("spend_usd"))
            w.users += 1
            # Classify on the unrounded paid total, for the same reason free_only is
            # a stored field on the user: a real paid record can be < 0.005 DBU.
            if r_paid > 0.0:
                w.paid_users += 1
        u = merged.get(principal)
        if u is None:
            u = merged[principal] = GenieUser(principal=principal, workspace_id=(str(wid) if wid else None))
        u.free_dbus += _f(r.get("free_dbus"))
        u.paid_dbus += _f(r.get("paid_dbus"))
        u.spend_usd += _f(r.get("spend_usd"))
        # Distinct days per workspace can't be summed exactly; the max is the
        # honest lower bound for "how many days was this person active".
        u.active_days = max(u.active_days, _i(r.get("active_days")))
        last = r.get("last_active_date")
        if last and (u.last_active_date is None or str(last) > u.last_active_date):
            u.last_active_date = str(last)
        for use in _surface_uses(r.get("by_surface")):
            existing = next(
                (s for s in u.by_surface if s.surface == use.surface and s.channel == use.channel),
                None,
            )
            if existing:
                existing.dbus += use.dbus
            else:
                u.by_surface.append(use)

    users = list(merged.values())
    for u in users:
        # Classify BEFORE rounding: a real Genie record can be a few thousandths of
        # a DBU, so rounding first would reclassify a paying user as free-tier and
        # inflate the free-tier percentage.
        u.free_only = u.paid_dbus <= 0.0
        u.free_dbus = round(u.free_dbus, 2)
        u.paid_dbus = round(u.paid_dbus, 2)
        u.spend_usd = round(u.spend_usd, 2)
        u.monthly_spend_usd = _monthly(u.spend_usd, lookback_days)
        u.by_surface.sort(key=lambda s: s.dbus, reverse=True)

    free_only = [u for u in users if u.free_only]
    free_tier = GenieFreeTier(
        total_users=len(users),
        free_only_users=len(free_only),
        paid_users=len(users) - len(free_only),
        free_only_pct=round(100.0 * len(free_only) / len(users), 1) if users else 0.0,
        free_dbus=round(sum(u.free_dbus for u in users), 2),
        paid_dbus=round(sum(u.paid_dbus for u in users), 2),
        free_dbus_free_only_users=round(sum(u.free_dbus for u in free_only), 2),
    )

    # --- surfaces -----------------------------------------------------------
    surf: dict[tuple, GenieSurface] = {}
    for r in data.get("surface") or []:
        name = str(r.get("surface") or "UNKNOWN")
        key = (name, str(r.get("channel") or "UNKNOWN"), bool(r.get("is_free")))
        s = surf.get(key)
        if s is None:
            s = surf[key] = GenieSurface(
                surface=name,
                surface_label=SURFACE_LABELS.get(name, name.replace("_", " ").title()),
                channel=key[1],
                is_free=key[2],
            )
        s.dbus += _f(r.get("dbus"))
        s.spend_usd += _f(r.get("spend_usd"))
        s.users += _i(r.get("users"))
        if r.get("workspace_id"):
            workspace_ids.add(str(r["workspace_id"]))
    surfaces = sorted(surf.values(), key=lambda s: s.spend_usd, reverse=True)
    for s in surfaces:
        s.dbus = round(s.dbus, 2)
        s.spend_usd = round(s.spend_usd, 2)

    # The single paid surface that carries most of the bill, and its share of paid
    # spend — usually Genie Code by a wide margin. Grouped by surface label across
    # channels (UI + API), because "Genie Code dominates" is a statement about the
    # surface, not about one of its channels. Computed here so the UI states the
    # real share instead of a hardcoded number.
    paid_by_label: dict[str, float] = {}
    for s in surfaces:
        if not s.is_free and s.spend_usd > 0:
            paid_by_label[s.surface_label] = paid_by_label.get(s.surface_label, 0.0) + s.spend_usd
    paid_total = sum(paid_by_label.values())
    top_paid_label: str | None = None
    top_paid_pct = 0.0
    if paid_total > 0:
        top_paid_label, top_spend = max(paid_by_label.items(), key=lambda kv: kv[1])
        top_paid_pct = round(100.0 * top_spend / paid_total, 1)

    # --- spaces -------------------------------------------------------------
    spaces: list[GenieSpace] = []
    for r in data.get("space") or []:
        dbus = _f(r.get("dbus"))
        api = _f(r.get("api_dbus"))
        spend = _f(r.get("spend_usd"))
        wid = r.get("workspace_id")
        if wid:
            workspace_ids.add(str(wid))
        spaces.append(
            GenieSpace(
                space_id=str(r.get("space_id") or "unknown"),
                workspace_id=str(wid) if wid else None,
                workspace_name=r.get("workspace_name"),
                workspace_url=r.get("workspace_url"),
                dbus=round(dbus, 2),
                spend_usd=round(spend, 2),
                monthly_spend_usd=_monthly(spend, lookback_days),
                users=_i(r.get("users")),
                active_days=_i(r.get("active_days")),
                api_share=round(api / dbus, 3) if dbus > 0 else 0.0,
                last_active_date=str(r["last_active_date"]) if r.get("last_active_date") else None,
            )
        )
    spaces.sort(key=lambda s: s.spend_usd, reverse=True)

    # --- daily trend (summed across workspaces) -----------------------------
    by_day: dict[str, GenieDay] = {}
    for r in data.get("daily") or []:
        day = str(r.get("usage_date") or "")
        if not day:
            continue
        d = by_day.get(day)
        if d is None:
            d = by_day[day] = GenieDay(usage_date=day)
        d.free_dbus += _f(r.get("free_dbus"))
        d.paid_dbus += _f(r.get("paid_dbus"))
        d.spend_usd += _f(r.get("spend_usd"))
        d.users += _i(r.get("users"))
        if r.get("workspace_id"):
            workspace_ids.add(str(r["workspace_id"]))
    daily = [by_day[k] for k in sorted(by_day)]
    for d in daily:
        d.free_dbus = round(d.free_dbus, 2)
        d.paid_dbus = round(d.paid_dbus, 2)
        d.spend_usd = round(d.spend_usd, 2)

    # Prefer the daily series for the headline total: it is the one aggregate with
    # no double-count risk (a day belongs to exactly one workspace row). Fall back
    # through the other aggregates rather than reporting $0 — each is a complete
    # partition of the same usage, so any one of them yields the true total. On a
    # live read where only the surface query beat the budget, the earlier
    # daily-then-users chain bottomed out at $0.00 while the surface table plainly
    # showed hundreds of dollars: a confident zero, which this tab must never show.
    total_spend = round(
        sum(d.spend_usd for d in daily)
        or sum(u.spend_usd for u in users)
        or sum(s.spend_usd for s in surfaces),
        2,
    )
    total_dbus = round(
        sum(d.free_dbus + d.paid_dbus for d in daily)
        or (free_tier.free_dbus + free_tier.paid_dbus)
        or sum(s.dbus for s in surfaces),
        2,
    )

    # --- per-workspace rollup -----------------------------------------------
    workspaces = list(ws.values())
    for w in workspaces:
        w.free_dbus = round(w.free_dbus, 2)
        w.paid_dbus = round(w.paid_dbus, 2)
        w.spend_usd = round(w.spend_usd, 2)
        w.monthly_spend_usd = _monthly(w.spend_usd, lookback_days)
    # Ranked by spend, but keep zero-spend (all-free) workspaces after the paying
    # ones rather than dropping them — an all-free workspace is a real fact worth
    # seeing, and by user count is the sensible tiebreak.
    workspaces.sort(key=lambda w: (w.spend_usd, w.users), reverse=True)

    # Which aggregates never arrived, so the UI can say "this section is missing"
    # instead of implying the account has no spaces / no trend.
    missing = [k for k in _SNAPSHOT_KINDS if not (data.get(k) or [])]

    return GenieCosts(
        missing_sections=missing,
        total_spend_usd=total_spend,
        monthly_spend_usd=_monthly(total_spend, lookback_days),
        total_dbus=total_dbus,
        workspace_count=len(workspace_ids),
        no_usage=not users and not surfaces and not daily,
        free_tier=free_tier,
        top_users=sorted(users, key=lambda u: (u.spend_usd, u.paid_dbus), reverse=True)[:_TOP_USERS],
        workspaces=workspaces,
        surfaces=surfaces,
        spaces=spaces[:_TOP_SPACES],
        daily=daily,
        top_paid_surface_label=top_paid_label,
        top_paid_surface_pct=top_paid_pct,
        **notes,
    )
