"""Rule: repeated SQL query shapes that scan without pruning or never hit the cache (Tier 2, C8).

A dashboard, alert or saved query that runs the same SELECT
many times a day costs that query's compute every time. Two shapes of it are fixable:

* **no file pruning** — each run reads ~every file of the tables it touches (pruned
  files ≈ 0) and a lot of bytes. Liquid clustering on the filter columns, or a
  materialized view the query reads instead, lets later runs skip most files.
* **identical query, never cached** — the very same query runs many times a day and not
  one run was served from the result cache. Usually the text is non-deterministic
  (``now()``, ``current_timestamp``, ``rand()``), or the tables are rewritten between
  runs — either way every run recomputes the same answer.

"The same query" is a fingerprint (see the SQL header): normalized text where it is
readable, else the saved-query / alert / dashboard / job id. ``statement_text`` is often
fully ``<REDACTED>``, so a redacted statement with no id is dropped, never pooled.

**Advisory, $0 booked.** The improvement factor of clustering or a rewrite is unknown,
so the figure is "$ spent on this query shape" (its share of the warehouse's Spark task
time × the warehouse's cost) — addressable spend, not a saving. One finding per
warehouse, naming the top shapes, so the resource page is not flooded with one option
per query. Failed statements are left to ``sql_repeated_failures``.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_ranked_bars,
)
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.query.history (FINISHED SELECTs grouped into query shapes: normalized "
    "statement text, else query_source sql_query_id / alert_id / dashboard_id / job_id; "
    "read_files, pruned_files, read_bytes, from_result_cache, total_task_duration_ms) + "
    "system.billing.usage x list_prices for the warehouse's corrected cost, {n}-day "
    "lookback. A shape's dollars are its share of the warehouse's Spark task time. The "
    "figure is what these shapes COST, not what a fix saves: how much clustering or a "
    "rewrite cuts is unknown, so no saving is booked. A dashboard- or job-level shape "
    "pools all of that dashboard's / job's queries."
)

_KIND_LABEL = {
    "text": "query",
    "sql_query": "saved query",
    "alert": "alert",
    "dashboard": "dashboard",
    "job": "job",
}

# Shape kinds whose executions are the IDENTICAL query every time (for the cache check).
# A dashboard or job fingerprint pools several different queries, so "never cached"
# says nothing about any one of them.
_IDENTICAL_KINDS = {"sql_query", "alert"}


def _label(shape: dict) -> str:
    kind = shape.get("kind") or "text"
    key = str(shape.get("key") or "")
    if kind == "text":
        text = (shape.get("sample_text") or "").strip()
        if text:
            return "“" + text[:90] + ("…" if len(text) > 90 else "") + "”"
        return f"query shape {key}"
    return f"{_KIND_LABEL.get(kind, kind)} {key}"


def classify_shape(shape: dict, t: dict) -> dict | None:
    """Classify one candidate shape: ``unpruned``, ``uncached`` or ``None``.

    Returns a dict with the derived metrics, or ``None`` when neither pattern holds.
    Unpruned wins when both do — a layout fix is the bigger lever, and the cache
    note is kept as a secondary flag."""
    executions = int(shape.get("executions") or 0)
    days = max(1, int(shape.get("shape_days") or 0))
    if executions < int(t.get("min_executions", 20)):
        return None
    if days < int(t.get("min_shape_days", 3)):
        return None

    scans = int(shape.get("scans") or 0)
    read_files = float(shape.get("read_files") or 0)
    pruned_files = float(shape.get("pruned_files") or 0)
    read_bytes = float(shape.get("read_bytes") or 0)
    total_files = read_files + pruned_files
    prune_ratio = (pruned_files / total_files) if total_files > 0 else None
    avg_read_files = (read_files / scans) if scans else 0.0
    avg_read_gb = (read_bytes / scans / 1e9) if scans else 0.0

    unpruned = (
        prune_ratio is not None
        and prune_ratio <= float(t.get("max_prune_ratio", 0.1))
        and avg_read_files >= float(t.get("min_avg_read_files", 20))
        and avg_read_gb >= float(t.get("min_avg_read_gb", 1.0))
    )

    kind = shape.get("kind") or "text"
    identical = kind in _IDENTICAL_KINDS or (
        kind == "text" and int(shape.get("distinct_texts") or 0) == 1
    )
    runs_per_day = executions / days
    uncached = (
        identical
        and int(shape.get("cache_hits") or 0) == 0
        and runs_per_day >= float(t.get("min_uncached_runs_per_day", 4))
    )
    if not (unpruned or uncached):
        return None
    return {
        "pattern": "unpruned" if unpruned else "uncached",
        "also_uncached": bool(unpruned and uncached),
        "kind": kind,
        "key": str(shape.get("key") or ""),
        "label": _label(shape),
        "executions": executions,
        "shape_days": days,
        "runs_per_day": round(runs_per_day, 1),
        "cache_hits": int(shape.get("cache_hits") or 0),
        "prune_ratio": None if prune_ratio is None else round(prune_ratio, 4),
        "avg_read_files": round(avg_read_files, 1),
        "avg_read_gb": round(avg_read_gb, 2),
        "task_ms": float(shape.get("task_ms") or 0),
        "nondeterministic_text": bool(int(shape.get("nondeterministic_text") or 0)),
    }


@register
class UnprunedRepeatedQueriesRule(Rule):
    id = "unpruned_repeated_queries"
    # Advisory: the spend on the shape is measured; what clustering or a rewrite
    # would cut from it is not.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Repeated queries that scan without pruning or never hit the cache"
    resource_type = "warehouse"
    category = "sql"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    cloud_savings = "none"
    sql_file = "unpruned_repeated_queries.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        total_task = float(row.get("total_task_ms") or 0.0)
        if total_task <= 0:
            return None
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None

        min_shape_usd = float(t.get("min_shape_usd", 10))
        flagged: list[dict] = []
        for shape in row.get("shapes") or []:
            c = classify_shape(shape, t)
            if c is None:
                continue
            share = max(0.0, min(1.0, c["task_ms"] / total_task))
            c["task_share"] = round(share, 4)
            c["monthly_usd"] = round(monthly_cost * share, 2)
            if c["monthly_usd"] < min_shape_usd:
                continue
            del c["task_ms"]
            flagged.append(c)
        if not flagged:
            return None

        flagged.sort(key=lambda c: c["monthly_usd"], reverse=True)
        addressable = min(monthly_cost, sum(c["monthly_usd"] for c in flagged))
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        unpruned = [c for c in flagged if c["pattern"] == "unpruned"]
        uncached = [c for c in flagged if c["pattern"] == "uncached"]
        unpruned_usd = sum(c["monthly_usd"] for c in unpruned)
        uncached_usd = sum(c["monthly_usd"] for c in uncached)
        primary = "unpruned" if unpruned_usd >= uncached_usd else "uncached"
        top = flagged[: int(t.get("top_shapes_shown", 5))]
        lead = top[0]
        name = row.get("warehouse_name") or str(row["warehouse_id"])
        n = ctx.lookback_days

        if lead["pattern"] == "unpruned":
            lead_clause = (
                f"The biggest, {lead['label']}, ran {lead['executions']:,} times on "
                f"{lead['shape_days']} days, reading on average {lead['avg_read_files']:,.0f} "
                f"files ({lead['avg_read_gb']:,.1f} GB) per run with "
                f"{(lead['prune_ratio'] or 0) * 100:.0f}% of files pruned — "
                f"~${lead['monthly_usd']:,.0f}/mo."
            )
        else:
            lead_clause = (
                f"The biggest, {lead['label']}, ran {lead['executions']:,} times "
                f"(~{lead['runs_per_day']:,.0f} a day) and was never served from the "
                f"result cache — ~${lead['monthly_usd']:,.0f}/mo."
            )
        parts = []
        if unpruned:
            parts.append(
                f"{len(unpruned)} repeated query shape(s) scan almost every file they "
                f"touch (~${unpruned_usd:,.0f}/mo)"
            )
        if uncached:
            parts.append(
                f"{len(uncached)} identical quer{'y' if len(uncached) == 1 else 'ies'} "
                f"re-run many times a day without ever hitting the result cache "
                f"(~${uncached_usd:,.0f}/mo)"
            )
        why = (
            f"On SQL warehouse '{name}', over the last {n} days, "
            + " and ".join(parts)
            + f" — together {addressable / monthly_cost * 100:.0f}% of the warehouse's "
            f"compute, ~${addressable:,.0f}/mo. {lead_clause} This is what these queries "
            f"cost, not a saving: how much a fix cuts depends on the tables and filters."
        )

        how_parts = []
        if unpruned:
            how_parts.append(
                "For the no-pruning shapes: open one run in Query History > Query "
                "Profile, find the scan that reads every file, and cluster that table on "
                "the columns the query filters or joins on (ALTER TABLE … CLUSTER BY "
                "(col, …), then OPTIMIZE; Predictive Optimization keeps it clustered). If "
                "a dashboard re-aggregates the same large table on every refresh, point "
                "it at a materialized view instead."
            )
        if uncached:
            nondet = any(c["nondeterministic_text"] for c in uncached)
            how_parts.append(
                "For the never-cached queries: "
                + (
                    "the text calls now() / current_timestamp / rand(), which disables "
                    "the result cache — replace it with a date parameter or a truncated "
                    "date. "
                    if nondet
                    else "check the text for now() / current_timestamp / rand() (they "
                    "disable the result cache) and replace them with a parameter; "
                )
                + "if the tables it reads are rewritten between runs, the cache cannot "
                "help, and a less frequent schedule or a materialized view is the fix."
            )
            alerts = [c for c in uncached if c["kind"] == "alert"]
            if alerts:
                a = alerts[0]
                how_parts.append(
                    f"Alert {a['key']} is evaluated ~{a['runs_per_day']:,.0f} times a "
                    "day; if its data does not change that often, give it a less "
                    "frequent schedule."
                )
        how = " ".join(how_parts) + (
            " No dollar saving is booked — re-check this finding after the change to "
            "see what the shape costs then."
        )

        if addressable >= 1000:
            priority = RulePriority.HIGH
        elif addressable >= 200:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        viz = viz_ranked_bars(
            title="What the repeated query shapes cost each month",
            unit="$/mo",
            rows=[
                {
                    "label": c["label"],
                    "value": c["monthly_usd"],
                    "color": "warn" if c["pattern"] == "unpruned" else "violet",
                    "sub": (
                        f"{c['executions']:,} runs · {(c.get('prune_ratio') or 0) * 100:.0f}% "
                        f"of files pruned · ~{c.get('avg_read_gb') or 0:,.1f} GB a run"
                        if c["pattern"] == "unpruned"
                        else f"{c['executions']:,} runs (~{c.get('runs_per_day') or 0:,.0f} a "
                        f"day) · never served from the result cache"
                    ),
                    "sub_msg": c["pattern"] if c["pattern"] == "unpruned" else "uncached",
                    "sub_params": {
                        "runs": int(c["executions"]),
                        "pruned_pct": round((c.get("prune_ratio") or 0) * 100),
                        "read_gb": round(float(c.get("avg_read_gb") or 0), 1),
                        "per_day": round(float(c.get("runs_per_day") or 0)),
                    },
                }
                for c in top
            ],
            note="Amber: scans almost every file. Violet: identical query never cached. "
                 "What the shapes cost, not what a fix saves.",
            msg="query_shape_cost",
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,  # a table layout change or a query rewrite
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                viz=viz,
                # Files/bytes/cache hits are measured; for the cache pattern we cannot
                # see whether the tables changed between runs, so it is weaker.
                confidence=CONFIDENCE_MEDIUM if primary == "unpruned" else CONFIDENCE_LOW,
                data_basis=_BASIS.format(n=n) + history_note(row, ctx) + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_size=row.get("warehouse_size"),
                warehouse_type=row.get("warehouse_type"),
                primary_pattern=primary,
                queries=int(row.get("queries") or 0),
                flagged_shapes=len(flagged),
                unpruned_shapes=len(unpruned),
                uncached_shapes=len(uncached),
                unpruned_monthly_usd=round(unpruned_usd, 2),
                uncached_monthly_usd=round(uncached_usd, 2),
                top_shapes=top,
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
