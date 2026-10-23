"""Synthetic CANDIDATE ROWS for demo mode, scored by the real rule.

Most demo findings are assembled from a per-rule profile (a waste range and a
savings fraction). That is fine for rules whose copy is generic, but it
misrepresents rules whose status and chart come out of ``score()``: an advisory
rule got a random non-zero saving, and no rule here got its chart. For the rules
below the demo instead builds a row shaped like the rule's SQL output and runs
the rule's own ``score()`` on it, so the demo finding has the production why /
how / savings status / evidence / chart — only the numbers are invented.

Each generator takes ``(rng, k)`` where ``k`` scales every dollar figure so the
demo spread varies between resources; the shapes mirror the rule's unit-test
fixtures (``rules/finops_rules/tests/test_<rule_id>.py``), which pass its guards.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Callable

LOOKBACK = 30


def _days(n: int = LOOKBACK) -> list[str]:
    """The closed-open window ending yesterday — what window_lower() selects."""
    start = date.today() - timedelta(days=n)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _keepalive(rng: random.Random, k: float) -> dict:
    ka = rng.randint(150, 260)
    real = rng.randint(40, 336 - ka)
    per_h = 10.0 * k
    states = []
    for d in range(14):  # business hours real work, the rest held by pings
        for h in range(24):
            states.append(1 if (9 <= h < 18 and d % 7 < 5 and rng.random() < 0.6) else 2)
    return {
        "warehouse_type": "PRO", "warehouse_size": "MEDIUM", "auto_stop_minutes": 30,
        "billed_hours": ka + real, "real_hours": real, "keepalive_hours": ka,
        "keepalive_days": 14, "keepalive_islands": rng.randint(10, 20),
        "keepalive_statements": ka * 60, "keepalive_cache_hits": ka * 55,
        "cost_window_usd": (ka + real) * per_h, "keepalive_cost_usd": ka * per_h,
        "dbus_30d": (ka + real) * 14.0, "unpriced_dbus": 0.0, "observed_days": 14,
        "top_clients": [{"client": "Databricks SQL Dashboard", "user": "bi-refresh", "n": ka * 50},
                        {"client": "unknown", "user": "svc-monitor", "n": ka * 10}],
        "hourly_states": states,
    }


def _min_clusters(rng: random.Random, k: float) -> dict:
    # min_clusters = 12, always on, ONE cluster needed in 97% of running minutes: a
    # phased card (12 -> 4 -> 2 -> 1). Per day: w = ~2 cluster-hours of real work (1
    # needed + autoscaling), x = the rest of the 12 x 24 floor-held cluster-hours.
    floor = 12
    days = []
    for d in _days():
        w = round(24 + rng.uniform(0, 3), 2)
        days.append({"d": d, "w": w, "x": round(floor * 24 - 24 - rng.uniform(0, 2), 2)})
    actual = sum(x["w"] + x["x"] for x in days) * 60
    excess = sum(x["x"] for x in days) * 60
    running = len(days) * 24 * 60
    step = max(1, -(-floor // 3))  # what the SQL's CEIL(min_clusters / 3) gives
    # Per running minute the floor holds 12 clusters; min 1 keeps ~1, min 4 keeps 4.
    step_excess = excess * (floor - step) / (floor - 1)
    return {
        "warehouse_type": "SERVERLESS", "warehouse_size": "LARGE", "min_clusters": floor,
        "max_clusters": 14, "auto_stop_minutes": 10, "actual_cluster_min": actual,
        "floor_cluster_min": actual, "excess_cluster_min": excess,
        "step_min_clusters": step, "step_excess_cluster_min": step_excess,
        "running_min": running,
        "one_cluster_min": running * 0.97, "no_query_min": running / 5, "peak_needed": 3,
        "starts": rng.randint(0, 20), "queries": rng.randint(8_000, 20_000), "queued_pct": 0.002,
        "p95_queue_wait_s": 0.0, "p50_duration_s": 1.4, "query_hours": 700,
        "hours_of_day_active": 24,
        "top_clients": [{"client": "Databricks SQL Dashboard", "user": "bi-refresh", "n": 5000}],
        "cost_window_usd": 40_320.0 * k / 8, "dbus_30d": actual / 60 * 38.0, "unpriced_dbus": 0.0,
        "observed_days": 30, "days": days,
    }


def _alert(rng: random.Random, k: float) -> dict:
    days = _days(14)
    warm = [{"d": d, "h": rng.randint(14, 24)} for d in days]
    hours = sum(x["h"] for x in warm)
    cost = hours * 9.0 * k / 3
    return {
        "display_name": rng.choice(["Orders null-rate", "Late ingest check", "Freshness SLA"]),
        "owned_by": "data-platform-team", "run_as": "data-platform-team",
        "compute_type": "WAREHOUSE", "warehouse_id": "wh-alerts",
        "warehouse_name": "alerts-serverless", "warehouse_type": "SERVERLESS",
        "warehouse_size": "Small", "auto_stop_minutes": 10, "cron": "0 0/5 * * * ?",
        "subscription_count": 1, "evaluations": 4032, "errored_evaluations": 0,
        "triggered_evaluations": 12, "notifying_evaluations": 12, "user_job_evaluations": 0,
        "observed_days": 14, "error_streak_days": 0, "last_error_code": None,
        "last_error_message": None, "last_evaluation_errored": False,
        "alert_statements": 4032, "alert_statement_hours": 336,
        "warm_hours": hours, "warm_days": 14, "warm_cost_usd": cost,
        "wh_alert_only_hours": hours, "wh_alert_only_cost_usd": cost,
        "wh_alert_only_islands": rng.randint(4, 10), "wh_cost_window_usd": cost * 1.6,
        "wh_billed_hours": 336, "dbus_30d": hours * 12.0, "unpriced_dbus": 0.0,
        "warm_by_day": warm,
        "eval_by_day": [{"d": d, "ok": 286, "t": rng.choice([0, 0, 1, 2]), "e": 0} for d in days],
    }


def _no_new_input(rng: random.Random, k: float) -> dict:
    days = _days()
    last = rng.randint(3, 10)  # the upstream stopped changing ~a week in
    rows = [{"d": d, "n": (rng.randint(2, 6) if i < last else 0),
             "z": (18 if i < last else 24)} for i, d in enumerate(days)]
    runs = sum(r["n"] + r["z"] for r in rows)
    none = sum(r["z"] for r in rows)
    per_run = 0.5 * k
    return {
        "job_name": rng.choice(["hourly-refresh", "orders-rollup", "kpi-snapshot"]),
        "evaluated_runs": runs, "observed_days": 30, "no_new_input_runs": none,
        "no_new_input_days": 30, "no_new_input_cost_30d": none * per_run,
        "cost_30d_usd": runs * per_run, "dbus_30d": runs * 1.0, "unpriced_dbus": 0.0,
        "last_new_input_run": f"{days[last - 1]} 06:00:00", "lineage_runs": runs,
        "has_path_read": 0, "has_view_read": 0, "has_indirect_read": 0, "n_sources": 2,
        "n_sources_unwritten": 0, "unwritten_sources": [], "has_system_source": 0,
        "has_inference_source": 0,
        "sample_sources": ["main.sales.orders", "main.sales.customers"], "days": rows,
    }


def _retry(rng: random.Random, k: float) -> dict:
    rows = [{"d": d, "c": rng.randint(4, 7), "r": rng.choice([0, 1, 1, 2, 3])} for d in _days()]
    ok = sum(r["c"] + r["r"] for r in rows)
    retried = sum(r["r"] for r in rows)
    return {
        "job_name": rng.choice(["nightly-ingest", "partner-sftp-load", "api-sync"]),
        "succeeded_runs": ok, "observed_days": 30, "retried_runs": retried,
        "retry_days": sum(1 for r in rows if r["r"]), "failed_attempts": int(retried * 1.2),
        "retry_rate": retried / ok, "retry_cost_30d": 400.0 * k,
        "cost_30d_usd": 4000.0 * k, "dbus_30d": 8000.0, "unpriced_dbus": 0.0,
        "continuous_runs": 0, "top_task_key": "load_orders",
        "top_failure_code": rng.choice(["RUN_EXECUTION_ERROR", "DRIVER_ERROR"]), "days": rows,
    }


def _serving(rng: random.Random, k: float) -> dict:
    days = _days(14)
    series = [{"d": d, "w": 1, "r": (rng.randint(5, 40) if rng.random() < 0.3 else 0)} for d in days]
    return {
        "endpoint_id": "ep", "endpoint_name": "pt-endpoint", "entity_type": "FOUNDATION_MODEL",
        "entity_name": "system.ai.gpt-oss-20b", "created_by": "ml-engineering",
        "billed_hours": 322, "active_hours": 6, "idle_hours": 316, "days_billed": 14,
        "observed_days": 14, "cost_30d_usd": 1205.0 * k, "dbus_30d": 17200.0,
        "unpriced_dbus": 0.0, "reclaimable_idle_hours": 315,
        "reclaimable_cost_usd": 1181.0 * k, "longest_idle_run_hours": 171,
        "requests_window": sum(s["r"] for s in series) or 12,
        "last_request_hour": f"{days[-3]}T10:00:00", "tokens_window": 20637,
        "peak_hour_tokens": 11042, "entity_matched": 1, "days_series": series,
    }


def _unpruned(rng: random.Random, k: float) -> dict:
    def shape(kind, key, execs, files, pruned, gb, task, text=None, nondet=0):
        return {"kind": kind, "key": key, "executions": execs, "shape_days": 30,
                "cache_hits": 0, "scans": execs, "read_files": files, "pruned_files": pruned,
                "read_bytes": gb * 1e9, "task_ms": task, "distinct_texts": 1 if text else 0,
                "sample_text": text, "nondeterministic_text": nondet}
    return {
        "warehouse_type": "PRO", "warehouse_size": "MEDIUM", "queries": 10_000,
        "observed_days": 30, "total_task_ms": 1_000_000.0, "candidate_shapes": 3,
        "shapes": [
            shape("dashboard", "sales-overview", 3000, 1_500_000, 15_000, 6_000, 450_000.0),
            shape("sql_query", "q-orders-by-region", 900, 400_000, 8_000, 1_800, 200_000.0),
            shape("text", "abc123", 1500, 3000, 3000, 15, 120_000.0,
                  "select count(*) from sales.orders where ts > now() - interval 1 hour", 1),
        ],
        "cost_30d_usd": 2000.0 * k, "dbus_30d": 2857.0, "unpriced_dbus": 0.0,
    }


def _dashboards(rng: random.Random, k: float) -> dict:
    days = _days()
    last_view = rng.randint(3, 12)
    rows = [{"d": d, "r": rng.randint(22, 26), "v": (rng.randint(1, 4) if i < last_view else 0)}
            for i, d in enumerate(days)]
    return {
        "warehouse_id": "wh-bi", "warehouses": 1, "executed_by": "bi-refresh",
        "queries": 336, "task_hours": 2.5, "observed_days": 14, "active_hours": 336,
        "first_query": f"{days[-14]}T09:00:00.000Z", "last_query": f"{days[-1]}T09:00:00.000Z",
        "unviewed_cost_window": 140.0 * k, "active_hours_cost_window": 200.0 * k,
        "sole_user_hours": 300, "dbus_30d": 50.0, "unpriced_dbus": 0.0,
        "unviewed_window_days": 14, "audit_events": 700, "view_events": 20,
        "last_view_time": f"{days[last_view - 1]}T09:00:00.000Z", "recent_view_events": 0,
        "automated_events": 690, "was_deleted": 0, "days": rows,
    }


def _smart_routing(rng: random.Random, k: float) -> dict:
    # Realistic distribution: long agent sessions hold ~90% of the spend,
    # and routed sessions save far more when short than when long.
    users = rng.randint(25, 120)
    adoption = rng.choice([0.0, 0.15, 0.3])
    routed_users = int(users * adoption)
    days, up_t, uo_t, r_t = [], 0.0, 0.0, 0.0
    for i, d in enumerate(_days()):
        weekday = (date.fromisoformat(d).weekday() < 5)
        base = users * (9.0 if weekday else 1.5) * k * rng.uniform(0.7, 1.3)
        r = base * adoption * 0.85
        up, uo = (base - r) * 0.9, (base - r) * 0.1
        days.append({"d": d, "up": round(up, 2), "uo": round(uo, 2), "r": round(r, 2)})
        up_t, uo_t, r_t = up_t + up, uo_t + uo, r_t + r
    unrouted = up_t + uo_t
    short_u, long_u = unrouted * 0.08, unrouted * 0.92
    short_r, long_r = r_t * 0.1, r_t * 0.9
    routed_sessions = routed_users * 14
    return {
        "observed_days": 30, "requests": int((unrouted + r_t) / 0.13), "unpriced_requests": 0,
        "sessions": users * 22 + routed_sessions, "routed_sessions": routed_sessions,
        "agent_users": users, "routed_users": routed_users,
        "turns": int((unrouted + r_t) / 0.13),
        "agent_cost_usd": unrouted + r_t, "unrouted_cost_usd": unrouted,
        "unrouted_premium_cost_usd": up_t, "routed_cost_usd": r_t,
        "short_unrouted_usd": short_u, "short_unrouted_turns": int(short_u / 0.147),
        "short_routed_usd": short_r, "short_routed_turns": int(short_r / 0.06),
        "short_routed_sessions": int(routed_sessions * 0.78),
        "long_unrouted_usd": long_u, "long_unrouted_turns": int(long_u / 0.128),
        "long_routed_usd": long_r, "long_routed_turns": int(long_r / 0.116),
        "long_routed_sessions": routed_sessions - int(routed_sessions * 0.78),
        "custom_endpoint_cost_usd": rng.choice([0.0, unrouted * 0.12]),
        "days": days,
        "top_models": [
            {"model": "claude-opus-4-8", "usd": up_t * 0.45, "users": int(users * 0.7),
             "avg_in": 150_000, "avg_out": 900, "cv_in": 1.1},
            {"model": "gpt-5-6-sol", "usd": up_t * 0.35, "users": int(users * 0.5),
             "avg_in": 130_000, "avg_out": 400, "cv_in": 0.6},
            {"model": "claude-opus-5", "usd": up_t * 0.2, "users": int(users * 0.3),
             "avg_in": 160_000, "avg_out": 780, "cv_in": 0.8},
            {"model": "claude-sonnet-5", "usd": uo_t, "users": int(users * 0.4),
             "avg_in": 110_000, "avg_out": 370, "cv_in": 0.9},
        ],
    }


# rule_id -> (generator, the key its SQL uses for the resource id)
GENERATORS: dict[str, tuple[Callable[[random.Random, float], dict], str]] = {
    "warehouse_keepalive_queries": (_keepalive, "warehouse_id"),
    "warehouse_min_clusters": (_min_clusters, "warehouse_id"),
    "alert_hygiene": (_alert, "alert_id"),
    "job_no_new_input": (_no_new_input, "job_id"),
    "job_task_retry_waste": (_retry, "job_id"),
    "serving_provisioned_overcapacity": (_serving, "endpoint_id"),
    "unpruned_repeated_queries": (_unpruned, "warehouse_id"),
    "unviewed_scheduled_dashboards": (_dashboards, "dashboard_id"),
    "coding_agent_smart_routing": (_smart_routing, "demo_resource_id"),  # one per workspace
}


def scored_demo_finding(rule, *, workspace_id: str, resource_id: str, resource_name: str,
                        rng: random.Random, thresholds: dict):
    """Build a candidate row for ``rule`` and score it with the real rule, or None
    when the demo has no generator for it (the caller then uses its profile).
    The resource identity is the demo's, so grouping on the resource page works."""
    spec = GENERATORS.get(rule.id)
    if spec is None:
        return None
    from brickpulse.rules.contract import RuleContext

    gen, id_key = spec
    row = gen(rng, rng.uniform(0.6, 2.5))
    row.update({"workspace_id": workspace_id, id_key: resource_id})
    for name_key in ("warehouse_name", "job_name", "endpoint_name"):
        if name_key in row and name_key.split("_")[0] in rule.resource_type:
            row[name_key] = resource_name
    ctx = RuleContext(thresholds=thresholds.get(rule.id, {}), lookback_days=LOOKBACK,
                      catalog="system", phasing=thresholds.get("phasing"))
    return rule.score(row, ctx)
