"""Daily "Saved so far" tracker: the ``track_changes`` task of the daily trigger job.

Why a separate task and plain Delta tables
------------------------------------------
Every SDP output is a materialized view holding only the current snapshot, and an
SDP streaming table is reset by a pipeline full refresh. History must survive both,
so the four tables here are plain Delta tables written by this task, which runs
after the pipeline in ``finops_precompute_sdp_daily`` (see ``databricks.yml``):

* ``finops_finding_ledger``      one compact row per main-set finding per snapshot day
* ``finops_workspace_retirement`` which workspaces are gone, and from when
* ``finops_change_events``       one row per change of a tracked finding's state
* ``finops_savings_daily``       booked saving per (day, spend scope), rebuilt in full

What counts: a non-advisory finding's
resource moved in the direction we recommended (partly or fully), or was deleted
(or its job paused) while its workspace is still active. Valued as
``fraction * estimate_before`` from the rule's own ``tracked_setting``
(``finops_rules.change_tracking``). Dedup per spend scope per day with the SAME
``book_scope`` as the headline (``finops.services.aggregation``).

Safety rules, each with a test in ``precompute/tests/test_change_tracker.py``:

* Absence is never a deletion: a deletion needs ``delete_time`` on the latest SCD row.
  A resource missing from the config query is "unknown" and yields no event.
* A config query that fails skips that resource type for the day.
* A workspace listing that fails, or comes back empty, changes no retirement.
* Idempotent per snapshot day: re-running a day replaces that day's ledger rows and
  that day's events, and rebuilds the daily table, with the same result.

Layout: everything above the "I/O" banner is pure Python on plain dicts. The I/O
layer at the bottom is thin and runs on either Spark (the job) or a SQL warehouse
(``--profile/--warehouse``, for a manual run against a scratch schema)::

    PYTHONPATH=rules:backend:. python precompute/change_tracker.py \\
        <catalog>.<scratch_schema> \\
        --recommendations-table <catalog>.<schema>.finops_recommendations \\
        --as-of 2026-10-06 --profile <profile> --warehouse <warehouse_id>
"""

from __future__ import annotations

from brickpulse.environment import get_env

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from brickpulse.server.services.aggregation import book_scope, scope_key
from brickpulse.rules.change_tracking import (
    CREDIT_KINDS,
    KIND_AWAY,
    KIND_DELETED,
    KIND_UNCHANGED,
    OBSERVED_KEY,
    ConfigState,
    assess_for_rule,
    evidence_core,
    to_utc_naive,
)
from brickpulse.rules.pricing import DAYS_PER_MONTH

LOG_PREFIX = "[finops-tracker]"

# ============================================================== table contracts
# The code is the source of truth for the shapes; precompute/ddl/<table>.sql mirrors
# each one and test_change_tracker pins them column for column.

LEDGER = "finops_finding_ledger"
EVENTS = "finops_change_events"
RETIREMENT = "finops_workspace_retirement"
DAILY = "finops_savings_daily"

TABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    LEDGER: [
        ("snapshot_date", "date"),
        ("finding_key", "string"),
        ("rule_id", "string"),
        ("rule_version", "string"),
        ("workspace_id", "string"),
        ("resource_type", "string"),
        ("resource_id", "string"),
        ("resource_name", "string"),
        ("savings_status", "string"),
        ("monthly_savings_usd", "double"),
        ("monthly_savings_low_usd", "double"),
        ("monthly_waste_usd", "double"),
        ("affected_spend_scope", "string"),
        ("savings_composition", "string"),
        ("evidence_core", "string"),
        ("computed_at", "timestamp"),
    ],
    EVENTS: [
        ("event_id", "string"),
        ("finding_key", "string"),
        ("changed_at", "timestamp"),
        ("change_kind", "string"),
        ("setting", "string"),
        ("baseline_value", "string"),
        ("after_value", "string"),
        ("proposed_value", "string"),
        ("fraction", "double"),
        ("valuation_method", "string"),
        ("estimate_before_usd", "double"),
        ("waste_before_usd", "double"),
        ("valued_monthly_usd", "double"),
        ("counted", "boolean"),
        ("not_counted_reason", "string"),
        ("rule_id", "string"),
        ("workspace_id", "string"),
        ("resource_type", "string"),
        ("resource_id", "string"),
        ("resource_name", "string"),
        ("first_seen", "date"),
        ("reference_snapshot_date", "date"),
        ("affected_spend_scope", "string"),
        ("savings_composition", "string"),
        ("recorded_on", "date"),
        ("recorded_at", "timestamp"),
    ],
    RETIREMENT: [
        ("workspace_id", "string"),
        ("row_kind", "string"),
        ("retired_on", "date"),
        ("reason", "string"),
        ("revived_on", "date"),
        ("first_listed", "date"),
        ("last_listed", "date"),
        ("last_active", "date"),
        ("last_status", "string"),
        ("updated_at", "timestamp"),
    ],
    DAILY: [
        ("day", "date"),
        ("workspace_id", "string"),
        ("scope_key", "string"),
        ("rule_ids", "array<string>"),
        ("finding_keys", "array<string>"),
        ("saved_usd", "double"),
        ("run_rate_monthly_usd", "double"),
        ("change_kinds", "array<string>"),
        ("computed_at", "timestamp"),
    ],
}

NOT_COUNTED_REASONS = (
    "before_first_seen",       # the change predates the first snapshot that showed it
    "advisory",                # no non-advisory snapshot before the change
    "workspace_retired",       # the workspace was gone or deleted on the change day
    "workspace_status_unknown",  # a deletion in a workspace never listed
)

# ============================================================== config queries
# One query per resource type: the latest SCD row per id, INCLUDING deleted ones
# (deletion needs delete_time). Every column below was verified live on
# a test account on 2026-10-06 (DESCRIBE + value checks), on top of the
# 2026-09-17 profile in sample_data/profile/schema_snapshot.json.


@dataclass(frozen=True)
class ConfigQuery:
    schema: str
    table: str
    id_col: str
    #: output column -> SQL expression over the system table (alias ``s``)
    columns: tuple[tuple[str, str], ...]
    delete_col: str = "delete_time"
    order_col: str = "change_time"


CONFIG_QUERIES: dict[str, ConfigQuery] = {
    "warehouse": ConfigQuery(
        "compute", "warehouses", "warehouse_id",
        (
            ("auto_stop_minutes", "s.auto_stop_minutes"),
            ("warehouse_size", "s.warehouse_size"),
            ("warehouse_type", "s.warehouse_type"),
            ("min_clusters", "s.min_clusters"),
            ("max_clusters", "s.max_clusters"),
        ),
    ),
    # worker_count and max_autoscale_workers are mutually exclusive (live: 4,956
    # fixed-size UI/API cluster rows carry only worker_count, 1,795 autoscaling ones
    # only max_autoscale_workers, none both or neither), so the COALESCE is "max workers".
    "cluster": ConfigQuery(
        "compute", "clusters", "cluster_id",
        (
            ("auto_termination_minutes", "s.auto_termination_minutes"),
            ("worker_count", "s.worker_count"),
            ("max_workers", "COALESCE(s.max_autoscale_workers, s.worker_count)"),
        ),
    ),
    # ``paused`` is a boolean that pausing writes as a new SCD row (live: 11,760 jobs
    # have rows with both values). It is NULL on ~12 % of rows (jobs with no trigger),
    # which reads as "not paused".
    "job": ConfigQuery("lakeflow", "jobs", "job_id", (("paused", "COALESCE(s.paused, false)"),)),
    # Scale-to-zero on == min_concurrency 0 (the same reading idle_serving_endpoints'
    # gate uses; 0 is common live). One row per served entity: the latest wins.
    "serving_endpoint": ConfigQuery(
        "serving", "served_entities", "endpoint_id",
        (
            (
                "min_concurrency",
                "COALESCE(s.custom_model_config.min_concurrency, "
                "s.feature_spec_config.min_concurrency)",
            ),
        ),
        delete_col="endpoint_delete_time",
    ),
    "instance_pool": ConfigQuery(
        "compute", "instance_pools", "instance_pool_id",
        (("min_idle_instances", "s.min_idle_instances"),),
    ),
    # Deletion only.
    "pipeline": ConfigQuery("lakeflow", "pipelines", "pipeline_id", ()),
    # Deletion only.
    "alert": ConfigQuery("alert", "alerts", "alert_id", ()),
}

#: Workspace statuses that mean "gone" (gone or deleted).
#: Verified live 2026-10-06: the column is ``status``, documented as NOT_PROVISIONED |
#: PROVISIONING | RUNNING | FAILED | BANNED, and only RUNNING (3,947) and BANNED (284)
#: occur. There is NO deleted/cancelled status: a deleted workspace drops out of the
#: listing (1,223 of 5,377 workspaces billed in the last 30 days are not listed), which
#: the "no longer listed" rule covers. BANNED (an expired/blocked workspace, e.g. an
#: workspace teardown) is unusable, so it counts as gone. The other names are kept
#: defensively in case the enum grows; anything else counts as active.
RETIRED_STATUSES = {
    "BANNED": "banned",
    "DELETED": "deleted",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "CANCELLING": "cancelled",
}
#: ``status`` is the real column; ``workspace_status`` is an alternative name some
#: copies of the table use.
STATUS_COLUMNS = ("status", "workspace_status")

ID_CHUNK = 500


def config_sql(resource_type: str, ids: list[tuple[str, str]], catalog: str = "system") -> str:
    """The latest SCD row per id for ``ids`` (``[(workspace_id, resource_id)]``),
    deleted ones included. Ties on change_time prefer the deletion row."""
    q = CONFIG_QUERIES[resource_type]
    values = ", ".join(f"({_sql_literal(w)}, {_sql_literal(r)})" for w, r in ids)
    cols = "".join(f",\n         {expr} AS {name}" for name, expr in q.columns)
    names = "".join(f", {name}" for name, _ in q.columns)
    return (
        f"WITH ids AS (\n"
        f"  SELECT * FROM VALUES {values} AS t(workspace_id, resource_id)\n"
        f"),\n"
        f"ranked AS (\n"
        f"  SELECT CAST(s.workspace_id AS STRING) AS workspace_id,\n"
        f"         CAST(s.{q.id_col} AS STRING) AS resource_id{cols},\n"
        f"         s.{q.order_col} AS change_time,\n"
        f"         s.{q.delete_col} AS delete_time,\n"
        f"         ROW_NUMBER() OVER (\n"
        f"           PARTITION BY s.workspace_id, s.{q.id_col}\n"
        f"           ORDER BY s.{q.order_col} DESC, s.{q.delete_col} DESC NULLS LAST\n"
        f"         ) AS rn\n"
        f"  FROM {catalog}.{q.schema}.{q.table} s\n"
        f"  JOIN ids ON CAST(s.workspace_id AS STRING) = ids.workspace_id\n"
        f"          AND CAST(s.{q.id_col} AS STRING) = ids.resource_id\n"
        f")\n"
        f"SELECT workspace_id, resource_id{names}, change_time, delete_time\n"
        f"FROM ranked WHERE rn = 1"
    )


def config_state(row: dict, resource_type: str) -> ConfigState:
    q = CONFIG_QUERIES[resource_type]
    return ConfigState(
        values={name: _plain(row.get(name)) for name, _ in q.columns if name in row},
        change_time=to_utc_naive(row.get("change_time")),
        delete_time=to_utc_naive(row.get("delete_time")),
    )


# ============================================================== small helpers


def _plain(v):
    """Decimals -> float, numpy scalars -> python; leave everything else."""
    if isinstance(v, Decimal):
        return float(v)
    item = getattr(v, "item", None)
    if callable(item) and not isinstance(v, (str, bytes, list, dict)):
        try:
            return item()
        except Exception:
            return v
    return v


def _float(v) -> float | None:
    v = _plain(v)
    if v is None:
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _json(v) -> dict:
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _event_id(finding_key: str, changed_at, kind: str, after) -> str:
    raw = f"{finding_key}|{changed_at.isoformat() if changed_at else ''}|{kind}|{after}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _finding_key(rec: dict) -> str:
    if rec.get("finding_key"):
        return str(rec["finding_key"])
    from brickpulse.server.finding_key import finding_key

    return finding_key(
        str(rec.get("rule_id")), str(rec.get("workspace_id")),
        str(rec.get("resource_type")), str(rec.get("resource_id")),
    )


def _ts_sort(v) -> datetime:
    return to_utc_naive(v) or datetime.min


# ============================================================== 1. ledger


def main_set(recs: list[dict], *, rules: dict, excluded_rule_ids: frozenset,
             sidecar_categories: frozenset) -> list[dict]:
    """The findings the app shows on its main surfaces: no sidecar category, no
    experimental (Labs) rule. Hidden rules stay in (they are excluded from the
    daily booking instead, see :func:`rebuild_daily`)."""
    out = []
    for r in recs:
        rid = str(r.get("rule_id") or "")
        rule = rules.get(rid)
        if rid in excluded_rule_ids or (rule is not None and getattr(rule, "experimental", False)):
            continue
        if (r.get("category") or "") in sidecar_categories:
            continue
        out.append(r)
    return out


def ledger_rows(recs: list[dict], snapshot: date, *, rules: dict,
                configs: dict, now: datetime) -> list[dict]:
    """Today's compact ledger rows. ``evidence_core`` keeps the tracked-setting keys,
    phasing / savings curve / idle gap, and the config value the tracker OBSERVED
    for the tracked column today (the baseline fallback)."""
    out = []
    for r in recs:
        rule = rules.get(str(r.get("rule_id") or ""))
        setting = getattr(rule, "tracked_setting", None)
        core = evidence_core(_json(r.get("evidence")), setting)
        rtype = str(r.get("resource_type") or "")
        by_id = configs.get(rtype)
        if setting is not None and by_id:
            state = by_id.get((str(r.get("workspace_id")), str(r.get("resource_id"))))
            if state is not None and setting.column in state.values:
                core[OBSERVED_KEY] = {setting.column: state.values[setting.column]}
        out.append({
            "snapshot_date": snapshot,
            "finding_key": _finding_key(r),
            "rule_id": str(r.get("rule_id") or ""),
            "rule_version": r.get("rule_version"),
            "workspace_id": str(r.get("workspace_id") or ""),
            "resource_type": rtype,
            "resource_id": str(r.get("resource_id") or ""),
            "resource_name": r.get("resource_name"),
            "savings_status": r.get("savings_status") or "bookable",
            "monthly_savings_usd": _float(r.get("monthly_savings_usd")),
            "monthly_savings_low_usd": _float(r.get("monthly_savings_low_usd")),
            "monthly_waste_usd": _float(r.get("monthly_waste_usd")),
            "affected_spend_scope": r.get("affected_spend_scope"),
            "savings_composition": r.get("savings_composition"),
            "evidence_core": json.dumps(core, sort_keys=True, default=str),
            "computed_at": to_utc_naive(r.get("computed_at")) or now,
        })
    return out


def histories(rows: list[dict]) -> dict[str, list[dict]]:
    """finding_key -> its ledger rows, oldest first (one per snapshot day)."""
    by: dict[str, dict] = defaultdict(dict)
    for r in rows:
        d = _as_date(r.get("snapshot_date"))
        by[str(r["finding_key"])][d] = r
    return {k: [v[d] for d in sorted(v)] for k, v in by.items()}


def baseline_evidence(rows: list[dict]) -> dict:
    """The finding's FIRST ledger row's evidence. If the config query failed that day
    (no ``_observed``), the observation comes from the earliest row that has one."""
    base = _json(rows[0].get("evidence_core")) if rows else {}
    if OBSERVED_KEY not in base:
        for r in rows[1:]:
            observed = _json(r.get("evidence_core")).get(OBSERVED_KEY)
            if observed:
                base = {**base, OBSERVED_KEY: observed}
                break
    return base


def is_tracked(rows: list[dict]) -> bool:
    """Advisory findings are never tracked: track a finding once ANY snapshot showed
    it with dollars (bookable or estimated)."""
    return any((r.get("savings_status") or "bookable") != "advisory" for r in rows)


# ============================================================== 2. retirement


class WorkspaceStatus:
    """Answers "was this workspace active, retired or unknown on day D?" from the
    retirement table (listing rows + retirement periods)."""

    def __init__(self, rows: list[dict]):
        self.listed: set[str] = set()
        self.periods: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
        for r in rows:
            ws = str(r["workspace_id"])
            if r.get("row_kind") == "listing":
                self.listed.add(ws)
            elif r.get("row_kind") == "retirement" and r.get("retired_on"):
                self.periods[ws].append((_as_date(r["retired_on"]), _as_date(r.get("revived_on"))))

    def state(self, workspace_id: str, day: date) -> str:
        ws = str(workspace_id)
        for start, end in self.periods.get(ws, ()):
            if start <= day and (end is None or day < end):
                return "retired"
        if ws in self.listed or ws in self.periods:
            return "active"
        return "unknown"


def update_retirement(prior: list[dict], listing: list[dict] | None, snapshot: date,
                      now: datetime) -> list[dict]:
    """The full retirement table after today's workspace listing.

    Two row kinds: one ``listing`` row per workspace ever listed (first/last listed,
    last active day, last status) and one ``retirement`` row per period a workspace
    was gone (``retired_on`` .. ``revived_on``). A workspace is retired when its
    status is in :data:`RETIRED_STATUSES` (BANNED, live), or when it was listed before
    and is no longer listed (how a deleted workspace shows up).
    ``retired_on`` is the day after it was last seen active (conservative: never
    later than the first run that saw it gone). A failed or EMPTY listing changes
    nothing: an empty answer is far likelier a permission glitch than every workspace
    vanishing at once. Idempotent for a re-run of the same day.
    """
    rows = [dict(r) for r in prior]
    if not listing:
        return rows
    listing_rows = {str(r["workspace_id"]): r for r in rows if r.get("row_kind") == "listing"}

    def open_period(ws: str) -> dict | None:
        for r in rows:
            if (r.get("row_kind") == "retirement" and str(r["workspace_id"]) == ws
                    and r.get("revived_on") is None):
                return r
        return None

    def retire(ws: str, on: date, reason: str) -> None:
        if open_period(ws) is None:
            rows.append({
                "workspace_id": ws, "row_kind": "retirement", "retired_on": on,
                "reason": reason, "revived_on": None, "first_listed": None,
                "last_listed": None, "last_active": None, "last_status": None,
                "updated_at": now,
            })

    seen: set[str] = set()
    for item in listing:
        ws = str(item.get("workspace_id"))
        if not ws or ws == "None":
            continue
        seen.add(ws)
        status = None
        for col in STATUS_COLUMNS:
            if item.get(col) is not None:
                status = str(item[col]).strip().upper()
                break
        lr = listing_rows.get(ws)
        if lr is None:
            lr = {
                "workspace_id": ws, "row_kind": "listing", "retired_on": None,
                "reason": None, "revived_on": None, "first_listed": snapshot,
                "last_listed": snapshot, "last_active": None, "last_status": status,
                "updated_at": now,
            }
            rows.append(lr)
            listing_rows[ws] = lr
        lr["first_listed"] = min(_as_date(lr.get("first_listed")) or snapshot, snapshot)
        lr["last_listed"] = max(_as_date(lr.get("last_listed")) or snapshot, snapshot)
        lr["last_status"] = status
        lr["updated_at"] = now
        reason = RETIRED_STATUSES.get(status or "")
        if reason:
            last_active = _as_date(lr.get("last_active"))
            retire(ws, (last_active + timedelta(days=1)) if last_active else snapshot, reason)
        else:
            lr["last_active"] = max(_as_date(lr.get("last_active")) or snapshot, snapshot)
            period = open_period(ws)
            if period is not None and _as_date(period["retired_on"]) <= snapshot:
                period["revived_on"] = snapshot
                period["updated_at"] = now

    for ws, lr in listing_rows.items():
        if ws in seen:
            continue
        last_listed = _as_date(lr.get("last_listed")) or snapshot
        retire(ws, last_listed + timedelta(days=1), "no_longer_listed")
    return rows


# ============================================================== 3. events


def _event_order(e: dict) -> tuple[datetime, datetime]:
    return _ts_sort(e.get("changed_at")), _ts_sort(e.get("recorded_at"))


def _last_event(events: list[dict]) -> dict | None:
    return max(events, key=_event_order) if events else None


def _first_credit_event(events: list[dict]) -> dict | None:
    credit = [e for e in events if e.get("change_kind") in CREDIT_KINDS]
    return min(credit, key=_event_order) if credit else None


def reference_row(rows: list[dict], change_day: date) -> tuple[dict | None, bool]:
    """(row, is_before_change): the last non-advisory snapshot on or before the
    change day, which is the estimate the change is valued against. When no such
    snapshot exists, the first non-advisory one (so a not-counted event still shows
    what it would have been worth) and False."""
    eligible = [r for r in rows if (r.get("savings_status") or "bookable") != "advisory"]
    before = [r for r in eligible if (_as_date(r.get("snapshot_date")) or date.max) <= change_day]
    if before:
        return before[-1], True
    return (eligible[0] if eligible else None), False


def count_status(*, change_day: date, first_seen: date, reference_before: bool,
                 kind: str, workspace_id: str, status: WorkspaceStatus) -> tuple[bool, str | None]:
    """Whether a change counts, and why not."""
    if change_day < first_seen:
        return False, "before_first_seen"
    if not reference_before:
        return False, "advisory"
    state = status.state(workspace_id, change_day)
    if state == "retired":
        return False, "workspace_retired"
    if kind == KIND_DELETED and state == "unknown":
        return False, "workspace_status_unknown"
    return True, None


_STATIC_REASONS = frozenset({"before_first_seen", "advisory"})


def plan_events(
    *,
    finding_rows: dict[str, list[dict]],
    configs: dict,
    prior_events: list[dict],
    retirement: list[dict],
    rules: dict,
    snapshot: date,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """Today's new change events and the counted-flag updates for earlier events.

    * ``finding_rows``: finding_key -> ledger rows (oldest first). Every finding that
      yields an event needs its FULL history here (the I/O layer loads it).
    * ``configs``: resource_type -> {(workspace_id, resource_id): ConfigState}, or None
      for a type whose query failed (skipped today).
    * ``prior_events``: events recorded on earlier days.

    A new event is recorded when the assessment differs from the finding's last
    recorded state. Several changes re-value the same finding from its ORIGINAL
    baseline against one fixed reference snapshot (the one before its first credited
    change), so the valuation is a timeline, not a sum.
    """
    status = WorkspaceStatus(retirement)
    by_finding: dict[str, list[dict]] = defaultdict(list)
    for e in prior_events:
        by_finding[str(e["finding_key"])].append(e)

    new_events: list[dict] = []
    for key, rows in finding_rows.items():
        if not rows or not is_tracked(rows):
            continue
        first = rows[0]
        rtype = str(first.get("resource_type") or "")
        by_id = configs.get(rtype)
        if by_id is None:
            continue  # unknown type, or its query failed today: skip
        state = by_id.get((str(first.get("workspace_id")), str(first.get("resource_id"))))
        if state is None:
            continue  # missing from the query: unknown, NEVER a deletion
        prior = by_finding.get(key, [])
        last = _last_event(prior)
        if last is not None and last.get("change_kind") == KIND_DELETED:
            continue  # deletion is final

        rule = rules.get(str(first.get("rule_id") or ""))
        # The valuation anchor: the reference snapshot of the finding's first credited
        # change (one that was not ruled out statically), so every later change is
        # re-valued against the same proposal and estimate.
        anchor = _first_credit_event(
            [e for e in prior if e.get("not_counted_reason") not in _STATIC_REASONS]
        )
        if anchor is not None:
            ref_day = (_as_date(anchor.get("reference_snapshot_date"))
                       or _as_date(anchor["changed_at"]))
            ref = next((r for r in rows if _as_date(r.get("snapshot_date")) == ref_day), None)
            if ref is None:  # that ledger row is gone: the closest one before it
                ref, _ = reference_row(rows, ref_day)
            ref_before = True
        else:
            # Provisional reference (latest non-advisory) to read the proposal; replaced
            # below once the change day is known.
            ref, ref_before = reference_row(rows, date.max)[0], False
        baseline = baseline_evidence(rows)
        a = assess_for_rule(rule, baseline, _json((ref or {}).get("evidence_core")), state)
        if a is None:
            continue
        if a.kind == KIND_UNCHANGED:
            if last is None or last.get("change_kind") == KIND_AWAY:
                continue
            kind = KIND_AWAY  # back to where it started: the earlier credit stops
        else:
            kind = a.kind
        changed_at = (a.changed_at or state.change_time
                      or datetime.combine(snapshot, datetime.min.time()))
        change_day = changed_at.date()
        if anchor is None:
            ref, ref_before = reference_row(rows, change_day)
            if ref is not None and kind != KIND_DELETED:
                # Re-assess against the snapshot actually before the change.
                a2 = assess_for_rule(rule, baseline, _json(ref.get("evidence_core")), state)
                if a2 is not None and a2.kind != KIND_UNCHANGED:
                    a, kind = a2, a2.kind
        if kind not in CREDIT_KINDS and (last is None or last.get("change_kind") == KIND_AWAY):
            continue  # an initial move away is not news, nor a second one
        fraction = a.fraction if kind in CREDIT_KINDS else 0.0
        if last is not None and (
            last.get("change_kind") == kind
            and str(last.get("after_value")) == str(a.after_value)
            and round(float(last.get("fraction") or 0.0), 4) == round(fraction, 4)
        ):
            continue  # same state as already recorded
        estimate = _float((ref or {}).get("monthly_savings_usd")) or 0.0
        waste = _float((ref or {}).get("monthly_waste_usd"))
        first_seen = _as_date(first.get("snapshot_date"))
        counted, reason = count_status(
            change_day=change_day, first_seen=first_seen, reference_before=ref_before,
            kind=kind, workspace_id=str(first.get("workspace_id")), status=status,
        )
        latest = rows[-1]
        new_events.append({
            "event_id": _event_id(key, changed_at, kind, a.after_value),
            "finding_key": key,
            "changed_at": changed_at,
            "change_kind": kind,
            "setting": a.setting,
            "baseline_value": a.baseline_value,
            "after_value": a.after_value,
            "proposed_value": a.proposed_value,
            "fraction": round(fraction, 4),
            "valuation_method": a.method,
            "estimate_before_usd": round(estimate, 2),
            "waste_before_usd": round(waste, 2) if waste is not None else None,
            "valued_monthly_usd": round(fraction * estimate, 2),
            "counted": counted,
            "not_counted_reason": reason,
            "rule_id": str(first.get("rule_id") or ""),
            "workspace_id": str(first.get("workspace_id") or ""),
            "resource_type": rtype,
            "resource_id": str(first.get("resource_id") or ""),
            "resource_name": latest.get("resource_name") or first.get("resource_name"),
            "first_seen": first_seen,
            "reference_snapshot_date": _as_date((ref or {}).get("snapshot_date")),
            "affected_spend_scope": (ref or latest).get("affected_spend_scope"),
            "savings_composition": (ref or latest).get("savings_composition"),
            "recorded_on": snapshot,
            "recorded_at": now,
        })

    # Re-derive the workspace-dependent counted flag of earlier events: a retirement
    # detected today can retroactively cover a change (and a revival can't undo it).
    updates = []
    for e in prior_events:
        if e.get("not_counted_reason") in _STATIC_REASONS:
            continue
        counted, reason = count_status(
            change_day=_as_date(e["changed_at"]),
            first_seen=_as_date(e.get("first_seen")) or date.min,
            reference_before=True, kind=str(e.get("change_kind")),
            workspace_id=str(e.get("workspace_id")), status=status,
        )
        if bool(e.get("counted")) != counted or e.get("not_counted_reason") != reason:
            updates.append(
                {"event_id": e["event_id"], "counted": counted, "not_counted_reason": reason}
            )
    return new_events, updates


def apply_updates(events: list[dict], updates: list[dict]) -> list[dict]:
    by_id = {u["event_id"]: u for u in updates}
    out = []
    for e in events:
        u = by_id.get(e.get("event_id"))
        out.append({**e, **u} if u else e)
    return out


def needs_full_history(*, finding_rows: dict[str, list[dict]], configs: dict,
                       prior_events: list[dict], rules: dict) -> set[str]:
    """Findings whose full ledger history the I/O layer must load before
    :func:`plan_events`: any with an earlier non-final event, or whose resource no
    longer matches its baseline. Everything else needs only its first and latest
    rows (the summary query), which keeps the daily read small."""
    final = {str(e["finding_key"]) for e in prior_events if e.get("change_kind") == KIND_DELETED}
    with_events = {str(e["finding_key"]) for e in prior_events} - final
    out = set(with_events)
    for key, rows in finding_rows.items():
        if key in out or key in final or not rows or not is_tracked(rows):
            continue
        first = rows[0]
        by_id = configs.get(str(first.get("resource_type") or ""))
        if not by_id:
            continue
        state = by_id.get((str(first.get("workspace_id")), str(first.get("resource_id"))))
        if state is None:
            continue
        ref, _ = reference_row(rows, date.max)
        a = assess_for_rule(
            rules.get(str(first.get("rule_id") or "")), baseline_evidence(rows),
            _json((ref or {}).get("evidence_core")), state,
        )
        if a is not None and a.kind != KIND_UNCHANGED:
            out.add(key)
    return out


def tracked_ids(finding_rows: dict[str, list[dict]], prior_events: list[dict]) -> dict[str, set]:
    """resource_type -> {(workspace_id, resource_id)} whose config to read today."""
    final = {str(e["finding_key"]) for e in prior_events if e.get("change_kind") == KIND_DELETED}
    out: dict[str, set] = defaultdict(set)
    for key, rows in finding_rows.items():
        if key in final or not rows or not is_tracked(rows):
            continue
        r = rows[0]
        rtype = str(r.get("resource_type") or "")
        if rtype in CONFIG_QUERIES:
            out[rtype].add((str(r.get("workspace_id")), str(r.get("resource_id"))))
    return out


# ============================================================== 4. daily rebuild


@dataclass
class _Booked:
    """The attributes :func:`book_scope` / :func:`scope_key` read, for one finding on
    one day."""

    workspace_id: str
    resource_type: str
    resource_id: str
    affected_spend_scope: str | None
    savings_composition: str | None
    monthly_waste_usd: float
    valued: float
    rule_id: str
    finding_key: str
    kind: str


def rebuild_daily(
    events: list[dict],
    retirement: list[dict],
    *,
    tracking_since: date | None,
    end_day: date,
    excluded_rule_ids: frozenset = frozenset(),
    now: datetime,
) -> list[dict]:
    """``finops_savings_daily`` from scratch: per (day, spend scope), the booked
    monthly value of every counted change in effect that day, deduped with the
    headline's ``book_scope``, and that day's share ``booked / DAYS_PER_MONTH``.

    A change accrues from its own ``changed_at`` day (back-dated if detected late)
    until the finding's next event replaces it (a re-valuation or a move away), and
    never on a day its workspace is retired. Rows only where something was booked.
    """
    if tracking_since is None or end_day < tracking_since:
        return []
    status = WorkspaceStatus(retirement)
    by_finding: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if str(e.get("rule_id")) in excluded_rule_ids:
            continue
        by_finding[str(e["finding_key"])].append(e)

    # finding -> [(start_day, end_day_inclusive, event)]
    segments: list[tuple[date, date, dict]] = []
    for evs in by_finding.values():
        evs.sort(key=lambda e: (_ts_sort(e.get("changed_at")), _ts_sort(e.get("recorded_at"))))
        for i, e in enumerate(evs):
            start = max(_as_date(e["changed_at"]), tracking_since)
            nxt = evs[i + 1] if i + 1 < len(evs) else None
            stop = (_as_date(nxt["changed_at"]) - timedelta(days=1)) if nxt else end_day
            stop = min(stop, end_day)
            value = float(e.get("valued_monthly_usd") or 0.0)
            if (not e.get("counted") or e.get("change_kind") not in CREDIT_KINDS
                    or value <= 0 or stop < start):
                continue
            segments.append((start, stop, e))

    per_day: dict[date, list[_Booked]] = defaultdict(list)
    for start, stop, e in segments:
        estimate = float(e.get("estimate_before_usd") or 0.0)
        waste = max(float(e.get("waste_before_usd") or 0.0), estimate)
        booked = _Booked(
            workspace_id=str(e.get("workspace_id")), resource_type=str(e.get("resource_type")),
            resource_id=str(e.get("resource_id")),
            affected_spend_scope=e.get("affected_spend_scope"),
            savings_composition=e.get("savings_composition"),
            monthly_waste_usd=waste, valued=float(e["valued_monthly_usd"]),
            rule_id=str(e.get("rule_id")), finding_key=str(e["finding_key"]),
            kind=str(e.get("change_kind")),
        )
        d = start
        while d <= stop:
            if status.state(booked.workspace_id, d) != "retired":
                per_day[d].append(booked)
            d += timedelta(days=1)

    out = []
    for day in sorted(per_day):
        by_scope: dict[tuple[str, str], list[_Booked]] = defaultdict(list)
        for b in per_day[day]:
            by_scope[scope_key(b)].append(b)
        for (ws, scope), items in sorted(by_scope.items()):
            run_rate = book_scope(items, value=lambda b: b.valued)
            if run_rate <= 0:
                continue
            out.append({
                "day": day,
                "workspace_id": ws,
                "scope_key": scope,
                "rule_ids": sorted({b.rule_id for b in items}),
                "finding_keys": sorted({b.finding_key for b in items}),
                "saved_usd": round(run_rate / DAYS_PER_MONTH, 4),
                "run_rate_monthly_usd": round(run_rate, 2),
                "change_kinds": sorted({b.kind for b in items}),
                "computed_at": now,
            })
    return out


def summarize(events: list[dict], daily: list[dict]) -> dict:
    """Counts for the run log (and the live-verification report)."""
    by_kind: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for e in events:
        by_kind[str(e.get("change_kind"))] += 1
        by_reason[str(e.get("not_counted_reason") or "counted")] += 1
    last_day = max((r["day"] for r in daily), default=None)
    return {
        "events": len(events),
        "by_kind": dict(sorted(by_kind.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "saved_usd_total": round(sum(r["saved_usd"] for r in daily), 2),
        "run_rate_monthly_usd": round(
            sum(r["run_rate_monthly_usd"] for r in daily if r["day"] == last_day), 2
        ),
    }


# ======================================================================= I/O
# Everything below talks to Spark or a SQL warehouse. Keep it thin.


def _sql_literal(v) -> str:
    v = _plain(v)
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return "NULL"
        return repr(v)
    if isinstance(v, datetime):
        return f"TIMESTAMP'{to_utc_naive(v).isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"DATE'{v.isoformat()}'"
    if isinstance(v, (list, tuple)):
        inner = ", ".join(_sql_literal(x) for x in v)
        return f"CAST(array({inner}) AS ARRAY<STRING>)"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def table_ddl(fqn: str, name: str) -> str:
    cols = ",\n  ".join(f"{c} {t.upper()}" for c, t in TABLE_COLUMNS[name])
    return f"CREATE TABLE IF NOT EXISTS {fqn} (\n  {cols}\n) USING DELTA"


class Engine:
    """``query`` returns plain dicts; ``execute`` runs a statement; ``write`` appends
    or overwrites rows given in the table's column order."""

    def query(self, sql: str) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def execute(self, sql: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def write(self, fqn: str, name: str, rows: list[dict], *, overwrite: bool) -> None:
        """Default: batched INSERT … VALUES (works on any SQL engine)."""
        cols = [c for c, _ in TABLE_COLUMNS[name]]
        if overwrite:
            self.execute(f"DELETE FROM {fqn}")
        for i in range(0, len(rows), 200):
            batch = rows[i:i + 200]
            values = ", ".join(
                "(" + ", ".join(_sql_literal(r.get(c)) for c in cols) + ")" for r in batch
            )
            self.execute(f"INSERT INTO {fqn} ({', '.join(cols)}) VALUES {values}")


class SparkEngine(Engine):
    def __init__(self, spark):
        self.spark = spark

    def query(self, sql: str) -> list[dict]:
        rows = self.spark.sql(sql).collect()
        return [{k: _plain(v) for k, v in r.asDict().items()} for r in rows]

    def execute(self, sql: str) -> None:
        self.spark.sql(sql)

    def write(self, fqn: str, name: str, rows: list[dict], *, overwrite: bool) -> None:
        cols = [c for c, _ in TABLE_COLUMNS[name]]
        schema = ", ".join(f"{c} {t}" for c, t in TABLE_COLUMNS[name])
        data = [tuple(r.get(c) for c in cols) for r in rows]
        df = self.spark.createDataFrame(data, schema=schema)
        df.write.format("delta").mode("overwrite" if overwrite else "append").saveAsTable(fqn)


class WarehouseEngine(Engine):  # pragma: no cover - needs a warehouse
    """databricks-sql-connector against a SQL warehouse, for a manual run."""

    def __init__(self, profile: str, warehouse: str):
        from databricks import sql
        from databricks.sdk.core import Config

        cfg = Config(profile=profile)
        host = (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
        self._conn = sql.connect(
            server_hostname=host, http_path=f"/sql/1.0/warehouses/{warehouse}",
            credentials_provider=lambda: cfg.authenticate, _socket_timeout=300,
        )

    def query(self, sql: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [{c: _plain(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]

    def execute(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)


def _safe_query(engine: Engine, sql: str, what: str) -> list[dict] | None:
    try:
        return engine.query(sql)
    except Exception as exc:  # noqa: BLE001 - one failed read must not fail the run
        print(f"{LOG_PREFIX} {what} unavailable ({str(exc).splitlines()[0][:300]}); skipping")
        return None


def read_configs(engine: Engine, ids_by_type: dict[str, set], catalog: str) -> dict:
    """resource_type -> {(ws, id): ConfigState}; None for a type whose query failed."""
    out: dict = {}
    for rtype, ids in ids_by_type.items():
        if rtype not in CONFIG_QUERIES:
            continue
        ordered = sorted(ids)
        states: dict | None = {}
        for i in range(0, len(ordered), ID_CHUNK):
            rows = _safe_query(
                engine, config_sql(rtype, ordered[i:i + ID_CHUNK], catalog), f"{rtype} config"
            )
            if rows is None:
                states = None
                break
            for r in rows:
                states[(str(r["workspace_id"]), str(r["resource_id"]))] = config_state(r, rtype)
        out[rtype] = states
    return out


def _ledger_summary_sql(fqn: str, before: date) -> str:
    """First row and latest non-advisory row per finding, before ``before``."""
    return (
        f"SELECT * EXCEPT (rn_first, rn_last) FROM (\n"
        f"  SELECT *,\n"
        f"    ROW_NUMBER() OVER (PARTITION BY finding_key ORDER BY snapshot_date) AS rn_first,\n"
        f"    ROW_NUMBER() OVER (PARTITION BY finding_key ORDER BY\n"
        f"      CASE WHEN savings_status = 'advisory' THEN 1 ELSE 0 END,\n"
        f"      snapshot_date DESC) AS rn_last\n"
        f"  FROM {fqn} WHERE snapshot_date < DATE'{before.isoformat()}'\n"
        f") WHERE rn_first = 1 OR rn_last = 1"
    )


def run(
    schema_fqn: str,
    *,
    engine: Engine,
    rec_table: str | None = None,
    as_of: date | None = None,
    catalog: str = "system",
    now: datetime | None = None,
) -> dict:
    from brickpulse.server.config import Settings
    from brickpulse.rules import list_rules

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rec_table = rec_table or f"{schema_fqn}.finops_recommendations"
    t = {name: f"{schema_fqn}.{name}" for name in TABLE_COLUMNS}
    for name, fqn in t.items():
        engine.execute(table_ddl(fqn, name))
    try:  # real Delta tables take a table comment; never fail the run over one
        from brickpulse.server.services.table_comments import table_comment_sql

        for name, fqn in t.items():
            stmt = table_comment_sql(fqn, name)
            if stmt:
                engine.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_PREFIX} table comments skipped ({str(exc).splitlines()[0][:200]})")

    settings = Settings.from_env()
    rules = {r.id: r for r in list_rules()}
    recs = engine.query(
        f"SELECT * FROM {rec_table} "
        f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {rec_table})"
    )
    snapshot = as_of or max(
        (_as_date(r.get("snapshot_date")) for r in recs if r.get("snapshot_date")),
        default=None,
    ) or now.date()
    end_day = (as_of or now.date()) - timedelta(days=1)
    today_recs = main_set(
        recs, rules=rules, excluded_rule_ids=settings.experimental_rule_ids,
        sidecar_categories=settings.sidecar_category_set,
    )
    print(f"{LOG_PREFIX} snapshot {snapshot}: {len(today_recs)} main-set findings of {len(recs)}")

    prior_ledger = engine.query(_ledger_summary_sql(t[LEDGER], snapshot))
    prior_events = engine.query(
        f"SELECT * FROM {t[EVENTS]} WHERE recorded_on < DATE'{snapshot.isoformat()}'"
    )
    prior_retirement = engine.query(f"SELECT * FROM {t[RETIREMENT]}")
    listing = _safe_query(engine, f"SELECT * FROM {catalog}.access.workspaces_latest",
                          "workspace listing")

    # Config for every tracked resource, incl. today's findings (whose observation
    # becomes the baseline fallback in today's ledger rows).
    provisional = histories(prior_ledger + [
        {**r, "finding_key": _finding_key(r), "snapshot_date": snapshot,
         "savings_status": r.get("savings_status") or "bookable"} for r in today_recs
    ])
    configs = read_configs(engine, tracked_ids(provisional, prior_events), catalog)

    today_rows = ledger_rows(today_recs, snapshot, rules=rules, configs=configs, now=now)
    engine.execute(f"DELETE FROM {t[LEDGER]} WHERE snapshot_date = DATE'{snapshot.isoformat()}'")
    if today_rows:
        engine.write(t[LEDGER], LEDGER, today_rows, overwrite=False)

    finding_rows = histories(prior_ledger + today_rows)
    full = needs_full_history(finding_rows=finding_rows, configs=configs,
                              prior_events=prior_events, rules=rules)
    keys = sorted(full)
    for i in range(0, len(keys), ID_CHUNK):
        chunk = ", ".join(_sql_literal(k) for k in keys[i:i + ID_CHUNK])
        finding_rows.update(histories(engine.query(
            f"SELECT * FROM {t[LEDGER]} WHERE finding_key IN ({chunk})"
        )))

    retirement = update_retirement(prior_retirement, listing, snapshot, now)
    engine.write(t[RETIREMENT], RETIREMENT, retirement, overwrite=True)

    new_events, updates = plan_events(
        finding_rows=finding_rows, configs=configs, prior_events=prior_events,
        retirement=retirement, rules=rules, snapshot=snapshot, now=now,
    )
    engine.execute(f"DELETE FROM {t[EVENTS]} WHERE recorded_on = DATE'{snapshot.isoformat()}'")
    if new_events:
        engine.write(t[EVENTS], EVENTS, new_events, overwrite=False)
    for u in updates:
        reason = _sql_literal(u["not_counted_reason"])
        engine.execute(
            f"UPDATE {t[EVENTS]} SET counted = {_sql_literal(u['counted'])}, "
            f"not_counted_reason = {reason} WHERE event_id = {_sql_literal(u['event_id'])}"
        )
    all_events = apply_updates(prior_events, updates) + new_events

    since = engine.query(f"SELECT MIN(snapshot_date) AS d FROM {t[LEDGER]}")
    tracking_since = _as_date(since[0]["d"]) if since and since[0].get("d") else None
    daily = rebuild_daily(
        all_events, retirement, tracking_since=tracking_since, end_day=end_day,
        excluded_rule_ids=settings.hidden_rule_ids, now=now,
    )
    engine.write(t[DAILY], DAILY, daily, overwrite=True)

    result = {
        "snapshot_date": snapshot.isoformat(),
        "tracking_since": tracking_since.isoformat() if tracking_since else None,
        "ledger_rows": len(today_rows),
        "new_events": len(new_events),
        "flag_updates": len(updates),
        "skipped_resource_types": sorted(k for k, v in configs.items() if v is None),
        "workspace_listing": "unavailable" if listing is None else len(listing),
        "daily_rows": len(daily),
        **summarize(all_events, daily),
    }
    print(f"{LOG_PREFIX} {json.dumps(result, sort_keys=True)}")
    return result


def _spark():
    try:
        from databricks.sdk.runtime import spark  # inside Databricks

        return spark
    except Exception:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()


def main(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("schema", help="<catalog>.<schema> that holds (or will hold) the tracker tables")
    p.add_argument("--recommendations-table", default=None,
                   help="defaults to <schema>.finops_recommendations")
    p.add_argument("--as-of", default=None, help="snapshot date to record (YYYY-MM-DD)")
    p.add_argument("--system-catalog", default=get_env("FINOPS_CATALOG", "system"))
    p.add_argument("--profile", default=None, help="run on a SQL warehouse instead of Spark")
    p.add_argument("--warehouse", default=None)
    args = p.parse_args(argv)
    if args.profile and args.warehouse:
        engine: Engine = WarehouseEngine(args.profile, args.warehouse)
    else:
        engine = SparkEngine(_spark())
    return run(
        args.schema, engine=engine, rec_table=args.recommendations_table,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        catalog=args.system_catalog,
    )


if __name__ == "__main__":
    main()
