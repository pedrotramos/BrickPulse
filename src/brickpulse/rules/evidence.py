"""Evidence visualization contract — the "show the data" payload.

A :class:`RuleFinding` carries an ``evidence`` dict of the raw metrics that
triggered it. This module standardizes three *additive* keys inside that dict so
the frontend can render a chart and honestly label what backed the number:

* ``confidence`` — how directly the platform data supports the estimate
  (``high`` / ``medium`` / ``low``). Driven by *what data was available*, not by
  dollar size. E.g. cluster right-sizing reads real per-minute CPU (high);
  warehouse right-sizing has no CPU signal and is inferred from query
  concurrency (low).
* ``data_basis`` — one honest sentence: which system tables backed it, the
  lookback, and where we rounded down. Surfaced verbatim in the UI.
* ``viz`` — a small, self-describing chart payload (one of the ``viz_*``
  builders below). The frontend switches on ``viz["kind"]``.

Both the demo source and the live rule ``score()`` build these through the same
builders so the two modes produce an identical shape. Keep the payloads compact
— they are serialized into the recommendation table's ``evidence`` JSON column.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Confidence tiers. A rule picks the tier that matches the data it actually had.
CONFIDENCE_HIGH = "high"      # directly measured (e.g. per-minute CPU, event log)
CONFIDENCE_MEDIUM = "medium"  # measured but coarse / partially inferred
CONFIDENCE_LOW = "low"        # inferred from a proxy (no direct signal exists)


def evidence_block(
    *,
    confidence: str,
    data_basis: str,
    viz: dict | None = None,
    viz_extra: list[dict] | None = None,
    **metrics,
) -> dict:
    """Assemble a finding's ``evidence`` dict: raw metrics + the standardized
    keys. ``viz`` is omitted when a rule has no series to show.

    ``viz_extra`` holds ADDITIONAL charts rendered after the primary one, for
    findings that need more than one picture to be understood — e.g. a cluster
    shows both "when did it run" and "what were its CPU/memory/IO doing". Kept as
    a separate key (rather than making ``viz`` a list) so every existing consumer
    of ``viz`` keeps working unchanged."""
    block = dict(metrics)
    block["confidence"] = confidence
    block["data_basis"] = data_basis
    if viz is not None:
        block["viz"] = viz
    if viz_extra:
        block["viz_extra"] = [v for v in viz_extra if v]
    return block


# --- viz payload builders (one per chart kind the frontend knows) ----------

def viz_hour_heatmap(
    *,
    grid: list[list[int]],
    day_labels: list[str],
    reclaimable_hours: float,
    context: str = "warehouse",
    day_dates: list[str] | None = None,
) -> dict:
    """Day x hour grid of resource state for the warehouse auto-stop case.

    ``grid`` is one row per day, each a list of 24 hour-states:
    ``0`` = stopped (not billed), ``1`` = warm & serving queries,
    ``2`` = warm but idle (the reclaimable waste).

    ``day_dates`` (ISO dates, one per row) is set when rows are calendar days
    rather than a weekday profile, so the UI can name the actual day."""
    out = {
        "kind": "hour_heatmap",
        "day_labels": day_labels,
        "grid": grid,
        "reclaimable_hours": round(reclaimable_hours, 1),
        "context": context,
    }
    if day_dates and len(day_dates) == len(grid):
        out["day_dates"] = day_dates
    return out


def hourly_states_to_grid(states: list[int], cols: int = 24) -> list[list[int]]:
    """Reshape a flat hourly state series into day rows for :func:`viz_hour_heatmap`.

    Drops a trailing partial day so every row is a full 24 hours — a short last row
    would render as a stripe that stops mid-chart and read as missing data.

    Shared by every rule that emits an hourly series (warehouse auto-stop, and the
    continuous-pipeline rule) so the two cannot disagree on the reshape.
    """
    full = (len(states) // cols) * cols
    return [states[i : i + cols] for i in range(0, full, cols)]


def calendar_grid(states: list[int], start) -> tuple[list[list[int | None]], list[str]]:
    """Align an hourly series that begins at ``start`` to UTC calendar days.

    The SQL windows start at "now minus N days" truncated to the hour, so the
    first hour is rarely midnight; reshaping straight into rows of 24 would label
    13:00 as 00:00 and shift every day. The lead-in hours before the first sample
    and the not-yet-elapsed hours of the last day are ``None`` (no data), never 0
    (which would read as "stopped"). Returns the rows and one ISO date per row;
    without a parseable ``start`` it falls back to :func:`hourly_states_to_grid`
    and no dates.
    """
    if isinstance(start, str):
        try:
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            start = None
    if not isinstance(start, datetime) or not states:
        return hourly_states_to_grid(states), []
    if start.tzinfo is not None:
        start = start.astimezone(timezone.utc)
    cells: list[int | None] = [None] * start.hour + [int(s) for s in states]
    cells += [None] * (-len(cells) % 24)
    first = start.date()
    rows = [cells[i : i + 24] for i in range(0, len(cells), 24)]
    return rows, [(first + timedelta(days=i)).isoformat() for i in range(len(rows))]


def viz_burst_compare(
    *,
    load: list[float],
    warm_floor: float,
    idle_pct: float,
    cold_start_s: float,
) -> dict:
    """Query-load series (0..1 per bucket) for the serverless case. ``warm_floor``
    is the always-on billed minimum a classic warehouse pays across idle gaps;
    serverless drops to zero. ``idle_pct``/``cold_start_s`` annotate the callout."""
    return {
        "kind": "burst_compare",
        "load": [round(x, 3) for x in load],
        "warm_floor": round(warm_floor, 3),
        "idle_pct": round(idle_pct, 1),
        "cold_start_s": round(cold_start_s, 1),
    }


def cpu_area_evidence(
    *, cpu_hourly, p95_cpu, lookback_days: int, cpu_ts=None, idle_frac=None,
    idle_threshold: float = 0.10, viz_extra: list[dict] | None = None,
    basis_extra: str = "", **metrics,
) -> dict:
    """Build a cluster rule's evidence with an hourly-CPU chart from the
    node-timeline series the SQL supplies. Shared by the cluster rules so their
    detail pages all show the utilization graph, not just text. ``cpu_ts`` gives
    a real dated x-axis (the series is sparse — only hours the cluster ran);
    ``idle_frac`` lets the chart shade *when* it was idle.

    ``basis_extra`` is appended to the standard ``data_basis`` sentence — used to
    state the observation window (see :func:`finops_rules.history.history_note`)
    without every caller having to restate the whole basis."""
    cpu = [float(x) for x in (cpu_hourly or []) if x is not None]
    ts = [int(t) for t in (cpu_ts or []) if t is not None]
    idle = [float(x) for x in (idle_frac or []) if x is not None]
    if len(ts) != len(cpu):
        ts = []  # only trust timestamps that align 1:1 with the samples
    if len(idle) != len(cpu):
        idle = []
    viz = (
        viz_cpu_area(
            cpu=cpu, p95=float(p95_cpu or 0.0),
            window_label=f"{lookback_days} days", ts=ts or None,
            idle_frac=idle or None, idle_threshold=idle_threshold,
        )
        if cpu
        else None
    )
    basis = (
        f"system.compute.node_timeline, per-minute CPU across all nodes, "
        f"{lookback_days}-day lookback. CPU is measured directly, but it is a proxy "
        f"for the resource that will bind after downsizing."
        f"{basis_extra}"
    )
    return evidence_block(
        confidence=CONFIDENCE_MEDIUM, data_basis=basis, viz=viz, viz_extra=viz_extra, **metrics
    )


def cluster_activity_stats(cpu_ts, cpu_peak=None):
    """Summarize a cluster's node-timeline series for honest copy: returns
    ``(active_days, last_active_iso, days_since_active, peak_pct)``. Lets a rule
    say "ran on N of 30 days, last active X days ago, peaked at Y% when used" —
    so the recommendation acknowledges real (bursty) work instead of implying the
    cluster was always idle. Empty series → ``(0, None, None, 0.0)``."""
    ts = [int(t) for t in (cpu_ts or [])]
    if not ts:
        return (0, None, None, 0.0)
    days = {datetime.fromtimestamp(t, tz=timezone.utc).date() for t in ts}
    last = max(days)
    days_since = (date.today() - last).days
    peaks = [float(x) for x in (cpu_peak or []) if x is not None]
    peak_pct = round(max(peaks) * 100, 0) if peaks else 0.0
    return (len(days), last.isoformat(), days_since, peak_pct)


#: ``system.compute.clusters.cluster_source`` -> human label. The distinction is
#: not cosmetic: an all-purpose cluster legitimately idles between a person's
#: commands, while a job/pipeline cluster sitting idle is pure waste, so the same
#: measured idle time warrants a different conversation.
CLUSTER_KIND_LABELS: dict[str, str] = {
    "UI": "All-purpose cluster (created in the UI)",
    "API": "All-purpose cluster (created via API/Terraform/DAB)",
    "JOB": "Job cluster",
    "PIPELINE": "Pipeline (DLT) cluster",
    "PIPELINE_MAINTENANCE": "Pipeline maintenance cluster",
    "SQL": "SQL warehouse cluster",
    "MODELS": "Model serving cluster",
}


def cluster_kind_label(cluster_source: str | None) -> str:
    """Human label for a cluster's ``cluster_source`` (falls back to the raw
    value, or "Cluster" when the source is missing)."""
    if not cluster_source:
        return "Cluster"
    return CLUSTER_KIND_LABELS.get(str(cluster_source).upper(), str(cluster_source))


def cluster_resource_metrics(row: dict) -> dict:
    """Per-metric window summary for a cluster's evidence key/value table.

    Pulled from the hourly arrays the cluster SQL emits so the raw Evidence table
    states memory / swap / I/O-wait / network alongside CPU — the numbers a user
    needs to tell "oversized" apart from "memory-bound" or "I/O-starved"."""
    out: dict = {}
    for key, col, is_pct in _RESOURCE_SERIES:
        vals = [float(x) for x in (row.get(col) or []) if x is not None]
        if not vals:
            continue
        avg, peak = sum(vals) / len(vals), max(vals)
        if is_pct:
            out[f"{key}_avg_pct"] = round(avg * 100, 1)
            out[f"{key}_peak_pct"] = round(peak * 100, 1)
        else:
            out[f"{key}_avg_mb_per_min"] = round(avg / (1024 * 1024), 2)
            out[f"{key}_peak_mb_per_min"] = round(peak / (1024 * 1024), 2)
    return out


def cluster_daily_evidence(
    *, cpu_hourly, cpu_ts, idle_frac, cpu_peak, lookback_days: int,
    idle_threshold: float = 0.10, confidence: str = CONFIDENCE_HIGH,
    data_basis: str | None = None, viz_extra: list[dict] | None = None, **metrics,
) -> dict:
    """Cluster idle / auto-terminate evidence as a DAILY activity chart over the
    full lookback window (not the sparse hourly line, which collapses to the few
    hours a cluster actually ran and hides bursts under an hourly average).

    For each calendar day it reports how many hours the cluster ran, split into
    *active* vs *idle* by ACTUAL time below the CPU threshold (``idle_frac`` = the
    share of that hour's minutes below it) — NOT by whether the hour had any
    burst. A bursty-but-mostly-idle cluster therefore reads honestly (lots of
    amber), while the day's peak CPU is still surfaced so real work stays visible."""
    # node_timeline can have gaps → null elements inside these parallel arrays.
    # Coalesce element-wise (a missing CPU sample reads as 0% = idle) to keep the
    # four arrays index-aligned; filtering would misalign them.
    cpu = [float(x) if x is not None else 0.0 for x in (cpu_hourly or [])]
    ts = [int(t) for t in (cpu_ts or []) if t is not None]
    idle = [float(x) if x is not None else 0.0 for x in (idle_frac or [])]
    peak = [float(x) if x is not None else 0.0 for x in (cpu_peak or [])]
    n = len(cpu)
    if len(ts) != n:
        ts = []
    if len(peak) != n:
        peak = cpu[:]  # fall back to hourly average if no per-hour peak
    if len(idle) != n:
        idle = []

    # Aggregate present hours into per-day buckets keyed on the UTC calendar day.
    # active/idle are in *hours* (fractional), split by the real idle share of
    # each hour, so the amber (idle) portion reflects actual idle time.
    buckets: dict[date, dict] = {}
    for i in range(len(ts)):
        day = datetime.fromtimestamp(ts[i], tz=timezone.utc).date()
        b = buckets.setdefault(day, {"active": 0.0, "idle": 0.0, "peak": 0.0})
        frac_idle = idle[i] if i < len(idle) else (0.0 if peak[i] >= idle_threshold else 1.0)
        frac_idle = min(1.0, max(0.0, frac_idle))
        b["idle"] += frac_idle
        b["active"] += 1.0 - frac_idle
        b["peak"] = max(b["peak"], peak[i])

    last_active = max((d for d, b in buckets.items() if b["active"] or b["idle"]), default=None)
    today = date.today()
    end = max([today, *buckets.keys()]) if buckets else today
    start = end - timedelta(days=max(1, lookback_days) - 1)
    days = []
    d = start
    while d <= end:
        b = buckets.get(d)
        days.append({
            "d": d.isoformat(),
            "a": round(b["active"], 2) if b else 0,
            "i": round(b["idle"], 2) if b else 0,
            "pk": round(b["peak"], 3) if b else 0.0,
        })
        d += timedelta(days=1)

    viz = viz_cluster_daily(
        days=days, lookback_days=lookback_days, idle_threshold=idle_threshold,
        last_active=last_active.isoformat() if last_active else None,
        peak_overall=round(max(peak), 3) if peak else 0.0,
    )
    basis = data_basis or (
        f"system.compute.node_timeline, per-minute CPU across all nodes, "
        f"{lookback_days}-day lookback. Direct measurement — highest confidence tier."
    )
    return evidence_block(
        confidence=confidence, data_basis=basis, viz=viz, viz_extra=viz_extra, **metrics
    )


def viz_cluster_daily(
    *, days: list[dict], lookback_days: int, idle_threshold: float,
    last_active: str | None, peak_overall: float,
) -> dict:
    """Per-day activity for a cluster over the lookback: one entry per calendar
    day with ``a`` active hours, ``i`` idle hours, ``pk`` peak CPU (0..1).
    ``last_active`` is the ISO date the cluster last ran (or null)."""
    return {
        "kind": "cluster_daily",
        "days": days,
        "lookback_days": lookback_days,
        "idle_threshold": round(idle_threshold, 3),
        "last_active": last_active,
        "peak_overall": round(peak_overall, 3),
    }


def viz_cluster_week_heatmap(
    *,
    active_by_dow_hour: list[int] | None,
    up_by_dow_hour: list[int] | None = None,
    active_by_hour_minute: list[int] | None = None,
    lookback_days: int,
    idle_threshold: float = 0.10,
) -> dict | None:
    """"When was this cluster actually used?" as a 7x24 weekday x hour matrix,
    with a three-state split and an hour->minute drill.

    * ``active_by_dow_hour`` — 168 elements (index ``dow*24 + hour``, dow 0=Mon..6=Sun)
      of IN-USE minutes per cell, from the SQL's ``active_minute`` signal (CPU over
      threshold OR a job/notebook ran).
    * ``up_by_dow_hour`` — 168 elements of minutes the cluster was UP (billable) at
      all in that cell. Lets the frontend show three states rather than two:
      ``up == 0`` is OFF (not billed), ``active`` is in-use, and ``up - active`` is
      up-but-idle — the reclaimable waste, the same split the daily chart draws.
      Absent (or wrong length) => the frontend falls back to the two-state view.
    * ``active_by_hour_minute`` — 1440 elements (index ``hour*60 + minute``) of
      in-use minutes, so clicking an hour can drill into the minute the activity
      lands on (a :00 spike = a scheduled poller). Absent => no drilldown.

    The plain 24-bucket hour-of-day view sums every day together and hides that a
    cluster is idle every weekend / every night — exactly the contiguous gaps
    auto-termination reclaims — so this keeps the weekly rhythm. Returns ``None``
    when the row carried no matrix (older/precomputed rows) so the caller simply
    omits the panel."""
    if not active_by_dow_hour or len(active_by_dow_hour) != 168:
        return None
    out: dict = {
        "kind": "cluster_week_heatmap",
        "active_by_dow_hour": [int(x) for x in active_by_dow_hour],
        "lookback_days": int(lookback_days),
        "idle_threshold": round(idle_threshold, 3),
    }
    # up-minutes: only when present and the right shape (absent-not-empty, so the
    # frontend can tell "no up data" from "an all-off week").
    if up_by_dow_hour and len(up_by_dow_hour) == 168:
        out["up_by_dow_hour"] = [int(x) for x in up_by_dow_hour]
    if active_by_hour_minute and len(active_by_hour_minute) == 1440:
        out["active_by_hour_minute"] = [int(x) for x in active_by_hour_minute]
    return out


def viz_cluster_contention(
    *,
    cpu_solo: float | None,
    cpu_concurrent: float | None,
    cpu_concurrent_p95: float | None,
    mem_solo: float | None,
    mem_concurrent: float | None,
    mem_concurrent_p95: float | None,
    peak_concurrent_runs: int,
    contended_minutes: int,
    contended_job_count: int,
) -> dict | None:
    """Solo-vs-concurrent saturation for a shared all-purpose cluster: the picture
    behind the job-contention finding.

    Two grouped pairs (CPU, memory), each showing the cluster's saturation when a
    single job ran (``solo``) next to when >=2 jobs overlapped (``concurrent``,
    with its p95). The *gap* between the bars IS the argument — contention is
    saturation that RISES with concurrency, so a taller concurrent bar is the
    visible evidence, and a concurrent bar no taller than solo (an equally-busy
    box regardless of overlap) reads as "undersized, not contended" at a glance.

    The headline numbers (peak concurrent runs, contended minutes, jobs involved)
    annotate the chart. Returns ``None`` when there is no measured concurrent
    saturation to compare against — the panel is then simply omitted."""
    if cpu_concurrent is None and mem_concurrent is None:
        return None

    def _f(x: float | None) -> float | None:
        return None if x is None else round(float(x), 1)

    return {
        "kind": "cluster_contention",
        "cpu_solo": _f(cpu_solo),
        "cpu_concurrent": _f(cpu_concurrent),
        "cpu_concurrent_p95": _f(cpu_concurrent_p95),
        "mem_solo": _f(mem_solo),
        "mem_concurrent": _f(mem_concurrent),
        "mem_concurrent_p95": _f(mem_concurrent_p95),
        "peak_concurrent_runs": int(peak_concurrent_runs),
        "contended_minutes": int(contended_minutes),
        "contended_job_count": int(contended_job_count),
    }


def viz_cluster_resources(
    *,
    series: list[dict],
    lookback_days: int,
    stats: dict,
    cluster_kind: str | None,
    node_type: str | None,
    worker_count: int | None,
) -> dict:
    """Multi-resource utilization for a cluster: CPU is not enough to diagnose.

    A cluster at 8% CPU might be perfectly sized (memory-bound), starved on I/O,
    or genuinely abandoned — and the right action differs in each case. This
    payload carries the parallel hourly series so the detail page can show them
    together:

    ``series`` — one entry per sampled hour with ``ts`` plus whichever of
    ``cpu`` / ``mem`` / ``swap`` / ``io_wait`` / ``net`` the node timeline
    provided (0..1 for percentages; network normalized to its own peak so the
    shape is readable on one axis).

    ``stats`` — window summary (avg/peak per metric, and the peak network byte
    rate in absolute terms so the normalized line stays interpretable).

    ``cluster_kind`` distinguishes a Job cluster from an All-Purpose one, which
    changes what "idle" even means: an all-purpose cluster idles between a
    human's commands, a job cluster should not exist while idle at all."""
    return {
        "kind": "cluster_resources",
        "series": series,
        "lookback_days": int(lookback_days),
        "stats": stats,
        "cluster_kind": cluster_kind,
        "node_type": node_type,
        "worker_count": worker_count,
    }


#: Resource metrics we chart alongside CPU, as
#: ``(viz_key, row_array_column, is_percentage)``. Each is optional: node_timeline
#: coverage varies by cloud/runtime, so a missing column simply drops that line
#: rather than failing the rule.
#:
#: Note there is no disk-throughput entry: ``node_timeline`` exposes free space
#: per mount point (``disk_free_bytes_per_mount_point``), not read/write rates, so
#: we do not pretend to chart disk I/O. ``io_wait`` (CPU blocked on I/O) is the
#: honest proxy for storage pressure, and ``swap`` catches memory pressure that
#: ``mem_used_percent`` alone can hide.
_RESOURCE_SERIES: list[tuple[str, str, bool]] = [
    ("cpu", "cpu_hourly", True),
    ("mem", "mem_hourly", True),
    ("swap", "swap_hourly", True),
    ("io_wait", "iowait_hourly", True),
    ("net", "net_hourly", False),
]


def cluster_resources_viz(
    *,
    row: dict,
    lookback_days: int,
    cluster_kind: str | None = None,
) -> dict | None:
    """Build the multi-metric cluster resource chart from a candidate row.

    Reads the parallel hourly arrays the cluster rule SQL emits (CPU, memory,
    swap, I/O wait, network bytes) and assembles the index-aligned series plus a
    per-metric summary. Absolute byte counters are normalized to their own window
    peak for plotting, with the real peak rate kept in ``stats`` so the axis stays
    honest. Returns ``None`` when the node timeline gave us nothing to plot.

    Cluster rules attach this as an EXTRA chart (``viz_extra``) alongside their
    primary one, because "when did it run" and "what was it doing while it ran"
    are two different questions and users need both to act."""
    ts = [int(t) for t in (row.get("cpu_ts") or []) if t is not None]
    n = len(ts)
    if not n:
        return None

    # Element-wise coalesce keeps every array index-aligned with ts (node_timeline
    # can have gaps); filtering would silently shift metrics against each other.
    cols: dict[str, list[float]] = {}
    for key, col, _pct in _RESOURCE_SERIES:
        raw = row.get(col)
        if not raw:
            continue
        vals = [float(x) if x is not None else 0.0 for x in raw]
        if len(vals) == n:
            cols[key] = vals

    stats: dict = {}
    plot: dict[str, list[float]] = {}
    for key, _col, is_pct in _RESOURCE_SERIES:
        vals = cols.get(key)
        if not vals:
            continue
        peak, avg = max(vals), sum(vals) / len(vals)
        if is_pct:
            plot[key] = vals
            stats[f"{key}_avg_pct"] = round(avg * 100, 1)
            stats[f"{key}_peak_pct"] = round(peak * 100, 1)
        else:
            # Normalize byte rates to their own peak for a shared 0..1 axis; the
            # real magnitude lives in stats so the chart can label it.
            plot[key] = [v / peak if peak > 0 else 0.0 for v in vals]
            stats[f"{key}_peak_mb_per_min"] = round(peak / (1024 * 1024), 2)
            stats[f"{key}_avg_mb_per_min"] = round(avg / (1024 * 1024), 2)

    if not plot:
        return None
    series = [
        {"ts": ts[i], **{k: round(plot[k][i], 3) for k in plot}}
        for i in range(n)
    ]
    return viz_cluster_resources(
        series=series,
        lookback_days=lookback_days,
        stats=stats,
        cluster_kind=cluster_kind,
        node_type=row.get("node_type_id"),
        worker_count=row.get("worker_count"),
    )


_CLUSTER_RESOURCE_BASIS = (
    "system.compute.node_timeline, per-minute samples across all nodes rolled to "
    "hourly, {n}-day lookback: CPU, memory, swap, CPU I/O wait and network "
    "throughput. Direct measurement. Disk read/write rates are NOT in the node "
    "timeline (it reports free space per mount point), so I/O wait stands in for "
    "storage pressure. Metrics absent from this cloud/runtime are not plotted."
)


def viz_job_runs(
    *,
    days: list[dict],
    lookback_days: int,
    fail_rate: float,
    baseline_fail_rate: float,
    failed_cost: float,
) -> dict:
    """Per-day job-run outcomes for the failing-jobs rule (G1). One entry per
    calendar day with ``s`` succeeded runs and ``f`` failed runs, so the chart
    stacks a green success bar under a red failure bar and the recurring
    failures are visible across the window. ``fail_rate`` is the window failure
    share, ``baseline_fail_rate`` the healthy line drawn for reference, and
    ``failed_cost`` the measured dollars burned on failed runs."""
    return {
        "kind": "job_runs",
        "days": days,
        "lookback_days": lookback_days,
        "fail_rate": round(fail_rate, 3),
        "baseline_fail_rate": round(baseline_fail_rate, 3),
        "failed_cost": round(failed_cost, 2),
    }


def viz_driver_worker_split(
    *,
    driver_cpu: float,
    worker_cpu: float | None,
    driver_mem: float,
    worker_mem: float | None,
    worker_count: int,
    symptom: str,
) -> dict:
    """Driver-vs-worker CPU/memory for the jobs-compute rightsize rule. Two metric
    groups (CPU, memory), each with a driver bar and a worker bar, so "the work is on
    the driver while the workers idle" reads at a glance. ``worker_*`` is ``None`` for
    a job that ran single-node (no worker samples) — the chart then shows the driver
    only. ``symptom`` (driver_bound | idle_workers | memory_oversize) tailors the
    callout drawn beneath the bars."""
    return {
        "kind": "driver_worker_split",
        "driver_cpu": round(driver_cpu, 1),
        "worker_cpu": (round(worker_cpu, 1) if worker_cpu is not None else None),
        "driver_mem": round(driver_mem, 1),
        "worker_mem": (round(worker_mem, 1) if worker_mem is not None else None),
        "worker_count": int(worker_count),
        "symptom": symptom,
    }


def viz_write_no_read(
    *,
    days: list[dict],
    lookback_days: int,
    total_writes: int,
    total_reads: int,
    last_write: str | None,
) -> dict:
    """Per-day write vs read activity for the unused-data rule (G2). Each entry
    has ``w`` write events and ``r`` read events for that day; the chart shows a
    wall of writes with a flat-zero read line, making "produced but never
    consumed" self-evident. ``total_reads`` is surfaced (0 => the finding)."""
    return {
        "kind": "write_no_read",
        "days": days,
        "lookback_days": lookback_days,
        "total_writes": int(total_writes),
        "total_reads": int(total_reads),
        "last_write": last_write,
    }


def viz_serving_traffic(
    *,
    days: list[dict],
    lookback_days: int,
    days_billed: int,
    total_requests: int,
    last_request: str | None,
) -> dict:
    """Billed serving days against inference requests for an endpoint.

    Each day carries ``w`` for whether the endpoint billed and ``r`` for request
    count. The field names stay compact in the stored JSON, but the dedicated kind
    prevents serving traffic from being mislabeled as table writes and reads.
    """
    return {
        "kind": "serving_traffic",
        "days": days,
        "lookback_days": lookback_days,
        "days_billed": int(days_billed),
        "total_requests": int(total_requests),
        "last_request": last_request,
    }


def viz_wh_rightsize(
    *,
    current_size: str,
    proposed_size: str,
    queued_pct: float,
    max_queued_pct: float,
    spilled_pct: float,
    max_spilled_pct: float,
    p95_read_gb: float,
    max_p95_read_gb: float,
    p95_runtime_s: float,
    peak_concurrency: int,
    peak_slots_used: float | None = None,
    size_slots: int | None = None,
    floor_size: str | None = None,
    floor_savings_usd: float | None = None,
) -> dict:
    """Headroom payload for the warehouse right-size rule (G3).

    The chart does NOT draw an abstract size ladder — that told the reader
    nothing about *this* warehouse. It shows the signals the rule actually
    used to decide the size is safe to drop, each as a margin: the measured value
    (``value``) against the danger line (``limit``) that would have excluded the
    warehouse had it crossed. A queue/spill/scan sitting far under its line is the
    visible headroom. ``p95_runtime_s`` and ``peak_concurrency`` are the baselines
    to watch after the change (the KPIs the ``how`` tells the operator to monitor).

    When ``peak_slots_used`` and ``size_slots`` are provided (measured Spark slot
    occupancy from ``total_task_duration_ms`` and the size's slot estimate), a
    fourth gauge is added FIRST: peak slots busy against the size's capacity. This
    is the direct horsepower signal — unlike the other three it is a positive
    measurement of how little of the warehouse the work actually uses, not just the
    absence of a stress symptom — so it leads.

    All axes are normalized to a common shape (``label``/``value``/``limit``/
    ``unit``/``meaning``) so the frontend can render one gauge per signal without
    special-casing units."""
    headroom = []
    if peak_slots_used is not None and size_slots:
        headroom.append({
            "key": "slots",
            "label": "Peak Spark slots used",
            "value": round(float(peak_slots_used), 1),
            "limit": int(size_slots),
            "unit": "slots",
            "meaning": "measured horsepower used at peak — far below capacity means real headroom to shrink",
        })
    headroom += [
        {
            "key": "queue",
            "label": "Queued for capacity",
            "value": round(queued_pct, 2),
            "limit": round(max_queued_pct, 2),
            "unit": "%",
            "meaning": "concurrency pressure — high means add clusters, not shrink size",
        },
        {
            "key": "spill",
            "label": "Spilled to disk",
            "value": round(spilled_pct, 2),
            "limit": round(max_spilled_pct, 2),
            "unit": "%",
            "meaning": "memory pressure — a smaller size would make this worse",
        },
        {
            "key": "scan",
            "label": "p95 data scanned",
            "value": round(p95_read_gb, 2),
            "limit": round(max_p95_read_gb, 2),
            "unit": "GB",
            "meaning": "data volume per query — large scans need the bigger nodes",
        },
    ]
    out = {
        "kind": "wh_rightsize",
        "current_size": current_size,
        "proposed_size": proposed_size,
        "headroom": headroom,
        "p95_runtime_s": round(p95_runtime_s, 1),
        "peak_concurrency": int(peak_concurrency),
    }
    if peak_slots_used is not None and size_slots:
        out["peak_slots_used"] = round(float(peak_slots_used), 1)
        out["size_slots"] = int(size_slots)
        out["slot_util_pct"] = round(100.0 * float(peak_slots_used) / size_slots, 1)
    # The deeper size the measured headroom suggests is ultimately reachable, shown
    # as context (the recommendation itself is always a single step). Present only
    # when that floor is more than one step below the current size.
    if floor_size:
        out["floor_size"] = floor_size
        if floor_savings_usd is not None:
            out["floor_savings_usd"] = round(float(floor_savings_usd), 2)
    return out


def viz_wh_scale_out(
    *,
    current_max_clusters: int,
    proposed_max_clusters: int,
    min_clusters: int,
    queued_pct: float,
    p95_queue_wait_s: float,
    avg_queue_wait_s: float,
    queries: int,
    queued_queries: int,
    queries_by_hour: list[int] | None,
    queued_by_hour: list[int] | None,
    auto_stop_minutes: int | None,
) -> dict:
    """Under-provisioned warehouse payload for the scale-out rule. The chart shows
    the current vs proposed ``max_clusters`` (the lever), the share of queries that
    waited at capacity, and — from ``queries_by_hour`` / ``queued_by_hour`` — WHEN
    the contention happens, so the reader sees the extra cluster is only paid for
    during the busy hours it auto-scales into. ``auto_stop_minutes`` states that
    the added capacity stops when the queue drains, so idle-hour cost is unchanged."""
    return {
        "kind": "wh_scale_out",
        "current_max_clusters": int(current_max_clusters),
        "proposed_max_clusters": int(proposed_max_clusters),
        "min_clusters": int(min_clusters),
        "queued_pct": round(queued_pct, 1),
        "p95_queue_wait_s": round(p95_queue_wait_s, 1),
        "avg_queue_wait_s": round(avg_queue_wait_s, 1),
        "queries": int(queries),
        "queued_queries": int(queued_queries),
        "queries_by_hour": [int(x) for x in (queries_by_hour or [])],
        "queued_by_hour": [int(x) for x in (queued_by_hour or [])],
        "auto_stop_minutes": int(auto_stop_minutes) if auto_stop_minutes else None,
    }


def viz_autostop_tradeoff(
    *,
    gap_buckets: list[dict],
    candidates: list[dict],
    current_minutes: float | None,
    chosen_minutes: int,
    median_gap_min: float,
    p90_gap_min: float,
    queries_by_hour: list[int] | None,
    lookback_days: int,
    max_extra_stops_per_day: float,
    queries_by_hour_minute: list[int] | None = None,
    queries_by_dow_hour: list[int] | None = None,
    tiers: dict | None = None,
    high_churn: bool = False,
) -> dict:
    """The serverless auto-stop decision, shown rather than asserted.

    Linked panels the frontend draws from one payload:

    * ``gap_buckets`` — the warehouse's inter-query idle gaps by length band
      (``{"label", "lo", "hi", "n"}``). This is the shape that decides everything:
      a wall of sub-5-minute gaps means a short auto-stop would thrash.
    * ``candidates`` — every auto-stop value considered, each with the idle it
      reclaims, the extra restarts/day it introduces, and whether it was
      acceptable. Makes the recommendation auditable: the user sees why 7 and not
      1, and what going shorter would actually cost them.
    * ``queries_by_hour`` — 24 hourly query counts, the daily rhythm.
    * ``queries_by_hour_minute`` — 1440 per-minute counts (index ``h*60+m``), so
      clicking an hour can drill into the minute the queries actually land on;
      omitted for older/precomputed rows that predate it.
    * ``queries_by_dow_hour`` — 168 day-of-week x hour counts (index ``dow*24+h``,
      dow 0=Mon..6=Sun), drawn as a weekly heatmap. This is the panel that answers
      "where are the gaps?": a 24-bucket hour-of-day chart sums every day together
      and so hides that the warehouse is idle every weekend / every night — exactly
      the gaps the auto-stop reclaims. Absent on rows that predate it.
    * ``tiers`` — the saving split into ``enable`` (turn auto-stop ON, the bulk of
      the money for a warehouse that has none) and ``tighten`` (tune into the 5-10
      min window for a smaller further gain at the cost of restarts), so the UI
      leads with the safe win and presents the churny refinement as secondary.
      ``None`` / absent when the split wasn't computed.

    ``current_minutes`` is the reference the recommendation moves FROM: ``None`` (or
    0) means auto-stop is OFF today — the frontend must surface that prominently,
    since "it never stops" is the whole reason the bill is what it is.
    ``max_extra_stops_per_day`` is drawn as the acceptability line.
    ``high_churn`` means the chosen value sits ABOVE that line — surfaced anyway
    because the idle spend is material, so the chart must show the recommendation
    and the threshold it exceeds, not pretend it passed."""
    return {
        "kind": "autostop_tradeoff",
        "high_churn": bool(high_churn),
        "gap_buckets": gap_buckets,
        "candidates": candidates,
        "current_minutes": current_minutes,
        "chosen_minutes": int(chosen_minutes),
        "median_gap_min": round(float(median_gap_min or 0.0), 1),
        "p90_gap_min": round(float(p90_gap_min or 0.0), 1),
        "queries_by_hour": [int(x) for x in (queries_by_hour or [])],
        # 1440 per-minute counts when available; kept absent (not []) when the row
        # didn't carry it, so the frontend can distinguish "no drill data" from
        # "an all-zero minute profile".
        **(
            {"queries_by_hour_minute": [int(x) for x in queries_by_hour_minute]}
            if queries_by_hour_minute
            else {}
        ),
        # 168 day-of-week x hour counts; same absent-not-empty convention.
        **(
            {"queries_by_dow_hour": [int(x) for x in queries_by_dow_hour]}
            if queries_by_dow_hour
            else {}
        ),
        # The enable/tighten saving split; absent when not computed.
        **({"tiers": tiers} if tiers else {}),
        "lookback_days": int(lookback_days),
        "max_extra_stops_per_day": round(float(max_extra_stops_per_day), 2),
    }


#: Gap-histogram bucket bounds emitted by warehouse_autostop_serverless.sql.j2,
#: as ``(row_suffix, lower_bound_exclusive, upper_bound_inclusive)``. The suffix
#: matches the SQL column (``gap_hist_<suffix>``); ``over`` is the open-ended tail.
GAP_BUCKET_BOUNDS: list[tuple[str, float, float]] = [
    ("1", 0, 1), ("2", 1, 2), ("3", 2, 3), ("4", 3, 4), ("5", 4, 5),
    ("6", 5, 6), ("7", 6, 7), ("8", 7, 8), ("9", 8, 9), ("10", 9, 10),
    ("15", 10, 15), ("20", 15, 20), ("30", 20, 30), ("60", 30, 60),
    ("over", 60, float("inf")),
]


#: Fallback representative length (minutes) for the open-ended ``>60 min`` gap
#: bucket when the SQL did not carry the measured total (older/precomputed rows).
#: One day: long enough that every candidate setting fully reclaims such a gap.
_OVER_BUCKET_FALLBACK_MIN = 1440


def gap_histogram_from_row(row: dict) -> dict[int, float]:
    """Collect the SQL's ``gap_hist_*`` columns into the ``{bucket_upper: count}``
    mapping :func:`savings.choose_serverless_autostop` consumes.

    The open-ended ``over`` (>60 min) bucket is keyed at the **measured average
    length** of the gaps in it (``gap_over_minutes / gap_hist_over``) rather than a
    flat placeholder. This matters for a warehouse with multi-DAY idle stretches
    (weekends, dead weeks): the scorer prices reclaim as ``count * (rep - setting)``,
    so a placeholder far above the true average (the old flat 1440) over-states the
    reclaimed idle and can drive the modelled saving PAST the warehouse's real warm
    time — violating the never-exceed-true-avoidable-spend rule. The true average is
    exact for any candidate setting (all such gaps exceed it, so ``min(gap, s) = s``
    per gap, and ``count * avg == sum of the gaps``). Falls back to a one-day
    placeholder only when the measured total is absent (older rows)."""
    hist: dict[int, float] = {}
    over_n = float(row.get("gap_hist_over") or 0.0)
    over_min = row.get("gap_over_minutes")
    for suffix, _lo, hi in GAP_BUCKET_BOUNDS:
        n = row.get(f"gap_hist_{suffix}")
        if not n:
            continue
        if suffix == "over":
            # Measured average length; fall back to the placeholder if unavailable
            # or degenerate. Clamp to just above the 60-min lower bound so a rounding
            # artefact can never place the bucket inside the decision window.
            avg = (float(over_min) / over_n) if (over_min and over_n) else _OVER_BUCKET_FALLBACK_MIN
            key = int(max(61.0, avg))
        else:
            key = int(hi)
        hist[key] = hist.get(key, 0.0) + float(n)
    return hist


def gap_buckets_for_viz(row: dict) -> list[dict]:
    """The same histogram as display rows for the chart (label + bounds + count)."""
    out: list[dict] = []
    for suffix, lo, hi in GAP_BUCKET_BOUNDS:
        n = float(row.get(f"gap_hist_{suffix}") or 0.0)
        label = ">60m" if suffix == "over" else (f"{int(hi)}m" if hi - lo <= 1 else f"{int(lo)}-{int(hi)}m")
        out.append({"label": label, "lo": lo, "hi": None if hi == float("inf") else hi, "n": int(n)})
    return out


def viz_sql_failures(
    *,
    days: list[dict],
    lookback_days: int,
    fail_rate: float,
    failed_queries: int,
    total_queries: int,
    failed_compute_share: float,
    addressable_monthly_usd: float,
    top_failed_statement: str | None,
    top_failed_count: int,
    distinct_failed_shapes: int,
) -> dict:
    """Per-day query outcomes for the repeated-SQL-failures rule. Each
    ``days`` entry has ``t`` total and ``f`` failed queries that day, so the chart
    stacks failed (red) under total and the recurring failures show across the
    window — the same shape as the failing-jobs chart, one level up (queries, not
    runs). ``failed_compute_share`` is the fraction of Spark task-time spent on
    failed queries (what the addressable dollars are apportioned from), and
    ``top_failed_statement`` names the single most-repeated failing query."""
    return {
        "kind": "sql_failures",
        "days": days,
        "lookback_days": int(lookback_days),
        "fail_rate": round(fail_rate, 4),
        "failed_queries": int(failed_queries),
        "total_queries": int(total_queries),
        "failed_compute_share": round(failed_compute_share, 4),
        "addressable_monthly_usd": round(addressable_monthly_usd, 2),
        "top_failed_statement": top_failed_statement,
        "top_failed_count": int(top_failed_count),
        "distinct_failed_shapes": int(distinct_failed_shapes),
    }


def viz_sql_maintenance(
    *,
    days: list[dict],
    lookback_days: int,
    optimize_runs: int,
    vacuum_runs: int,
    maintenance_compute_share: float,
    addressable_monthly_usd: float,
) -> dict:
    """Per-day manual-maintenance activity for the Predictive-Optimization-overlap
    rule. Each ``days`` entry has ``o`` OPTIMIZE and ``v`` VACUUM
    statements run on the warehouse that day, drawn as two stacked bars, so a
    recurring maintenance cadence (a nightly job) is visible. ``addressable_monthly_usd``
    is the compute those statements consumed — the spend that becomes redundant if
    Predictive Optimization already maintains the same tables (advisory: unproven
    here, verify in Catalog Explorer)."""
    return {
        "kind": "sql_maintenance",
        "days": days,
        "lookback_days": int(lookback_days),
        "optimize_runs": int(optimize_runs),
        "vacuum_runs": int(vacuum_runs),
        "maintenance_compute_share": round(maintenance_compute_share, 4),
        "addressable_monthly_usd": round(addressable_monthly_usd, 2),
    }


def viz_ai_gateway_errors(
    *,
    days: list[dict],
    lookback_days: int,
    error_rate: float,
    failed_requests: int,
    total_requests: int,
    server_errors: int,
    failed_input_tokens: int,
    top_status_code: int | None,
    top_status_count: int,
) -> dict:
    """Per-day AI-gateway request outcomes for the wasted-inference rule.
    Each ``days`` entry has ``t`` total and ``f`` failed requests that day, so the chart
    stacks failed (red) under total — the same shape as the SQL-failures chart, one
    layer up (inference requests, not queries). ``failed_input_tokens`` is the input
    tokens billed on requests that then failed (wasted inference), and ``top_status_code``
    names the most common failing HTTP status. Operational: no dollars (gateway token
    cost is not reliably per-endpoint in the billing tables)."""
    return {
        "kind": "ai_gateway_errors",
        "days": days,
        "lookback_days": int(lookback_days),
        "error_rate": round(error_rate, 4),
        "failed_requests": int(failed_requests),
        "total_requests": int(total_requests),
        "server_errors": int(server_errors),
        "failed_input_tokens": int(failed_input_tokens),
        "top_status_code": int(top_status_code) if top_status_code else None,
        "top_status_count": int(top_status_count),
    }


def viz_pipeline_failures(
    *,
    days: list[dict],
    lookback_days: int,
    fail_rate: float,
    failed_updates: int,
    total_updates: int,
    failed_compute_share: float,
    addressable_monthly_usd: float,
    failed_requests: int,
) -> dict:
    """Per-day pipeline update outcomes for the failed-pipeline-updates rule.
    Each ``days`` entry has ``t`` total and ``f`` failed updates that day, drawn as
    failed (red) under total — the recurring wall of failed updates reads at a glance.
    ``failed_compute_share`` is the fraction of pipeline wall-time spent on failed
    updates (what the addressable dollars are apportioned from)."""
    return {
        "kind": "pipeline_failures",
        "days": days,
        "lookback_days": int(lookback_days),
        "fail_rate": round(fail_rate, 4),
        "failed_updates": int(failed_updates),
        "total_updates": int(total_updates),
        "failed_compute_share": round(failed_compute_share, 4),
        "addressable_monthly_usd": round(addressable_monthly_usd, 2),
        "failed_requests": int(failed_requests),
    }


def viz_instance_pool(
    *,
    days: list[dict],
    lookback_days: int,
    min_idle_instances: int,
    idle_autotermination_minutes: int | None,
    launches: int,
    acquisitions: int,
    node_type: str,
) -> dict:
    """Per-day instance-pool activity for the warm-standby rule. Each
    ``days`` entry has ``l`` instances launched and ``a`` acquired by a cluster that
    day, drawn against the ``min_idle_instances`` warm-floor reference line — so a warm
    floor sitting well above the demand it serves reads at a glance. Operational: no
    dollars (the warm VMs are cloud-billed, unpriceable from system tables)."""
    return {
        "kind": "instance_pool",
        "days": days,
        "lookback_days": int(lookback_days),
        "min_idle_instances": int(min_idle_instances),
        "idle_autotermination_minutes": (
            int(idle_autotermination_minutes) if idle_autotermination_minutes is not None else None
        ),
        "launches": int(launches),
        "acquisitions": int(acquisitions),
        "node_type": node_type,
    }


def viz_sharing_materialization(
    *,
    days: list[dict],
    lookback_days: int,
    materializations: int,
    distinct_tables: int,
    distinct_recipients: int,
) -> dict:
    """Per-day Delta Sharing materialization counts for the re-materialization rule. Each ``days`` entry has ``n`` materialization events that day, so a
    recurring cadence (rebuilding the same share far more often than it is read) is
    visible. Operational: no dollars (the table records the event, not its cost)."""
    return {
        "kind": "sharing_materialization",
        "days": days,
        "lookback_days": int(lookback_days),
        "materializations": int(materializations),
        "distinct_tables": int(distinct_tables),
        "distinct_recipients": int(distinct_recipients),
    }


def viz_storage_po(
    *,
    days: list[dict],
    lookback_days: int,
    po_dbus: float,
    operations: int,
    compaction_dbus: float,
    clustering_dbus: float,
    vacuum_dbus: float,
    top_operation: str | None,
) -> dict:
    """Per-day Predictive-Optimization DBUs for the storage-churn rule.
    Each ``days`` entry has ``n`` estimated DBUs PO spent maintaining the table that
    day, so a table drawing constant maintenance reads at a glance. The compaction /
    clustering / vacuum split says WHICH work dominates (compaction => small-file
    churn). Operational: DBUs are PO's own estimate, not booked dollars."""
    return {
        "kind": "storage_po",
        "days": days,
        "lookback_days": int(lookback_days),
        "po_dbus": round(po_dbus, 2),
        "operations": int(operations),
        "compaction_dbus": round(compaction_dbus, 2),
        "clustering_dbus": round(clustering_dbus, 2),
        "vacuum_dbus": round(vacuum_dbus, 2),
        "top_operation": top_operation,
    }


def viz_cpu_area(
    *,
    cpu: list[float],
    p95: float,
    window_label: str,
    ts: list[int] | None = None,
    idle_frac: list[float] | None = None,
    idle_threshold: float = 0.10,
) -> dict:
    """Hourly CPU utilization series (0..1) for cluster idle / right-size.

    ``ts`` — parallel unix-second hour timestamps for a real dated x-axis.
    ``idle_frac`` — parallel share (0..1) of that hour's minutes below the idle
    threshold, so the chart can shade *when the cluster was idle* (hourly average
    alone smooths the minute-level idleness the rule actually detects).
    ``idle_threshold`` — the CPU fraction below which a sample counts as idle."""
    out: dict = {
        "kind": "cpu_area",
        "cpu": [round(x, 3) for x in cpu],
        "p95": round(p95, 3),
        "window_label": window_label,
        "idle_threshold": round(idle_threshold, 3),
    }
    if ts:
        out["ts"] = [int(t) for t in ts]
    if idle_frac:
        out["idle_frac"] = [round(x, 3) for x in idle_frac]
    return out


# --- generic per-day and ranked charts ---------------------------------------
# One renderer each, so a new rule's picture is a builder call, not a new
# frontend component. Colours are design tokens the frontend maps to CSS
# variables, so both themes stay in lockstep.
VIZ_COLORS = ("info", "warn", "danger", "savings", "violet", "track")


def _day_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value)
    return s[:10] if len(s) >= 10 else None


def densify_days(rows, keys: list[str], lookback_days: int,
                 today: date | None = None) -> list[dict]:
    """Rows of ``{"d": "YYYY-MM-DD", <key>: n, ...}`` (sparse: the SQL only emits
    days with activity) -> one entry per day of the closed-open window ending
    yesterday, missing days filled with 0. A day outside the window is dropped."""
    today = today or date.today()
    start = today - timedelta(days=int(lookback_days))
    by_day: dict[str, dict] = {}
    for r in rows or []:
        d = _day_str(r.get("d") if isinstance(r, dict) else None)
        if d:
            by_day[d] = r
    out = []
    for i in range(int(lookback_days)):
        d = (start + timedelta(days=i)).isoformat()
        r = by_day.get(d) or {}
        out.append({"d": d, "v": [round(float(r.get(k) or 0.0), 2) for k in keys]})
    return out


def _with_msg(out: dict, msg: str | None, params: dict | None) -> None:
    if msg:
        out["msg"] = msg
        out["params"] = {k: v for k, v in (params or {}).items()
                         if v is None or isinstance(v, (int, float, str))}


def viz_daily_stack(
    *,
    title: str,
    unit: str,
    series: list[tuple[str, str, str]],
    rows,
    lookback_days: int,
    marker: tuple[object, str] | None = None,
    ref: tuple[float, str] | None = None,
    note: str | None = None,
    today: date | None = None,
    msg: str | None = None,
    params: dict | None = None,
) -> dict | None:
    """Per-day stacked bars. ``series`` is ``(row_key, label, colour_token)`` in
    stacking order (bottom first); ``rows`` the SQL's sparse daily array. An
    optional ``marker`` ``(day, label)`` draws a dashed vertical line (e.g. "last
    viewed"), ``ref`` ``(value, label)`` a dashed horizontal one (e.g. a floor).
    Returns None when there is nothing to draw, so the finding shows no chart
    rather than an empty frame.

    ``msg`` names the chart's message group in the frontend catalogs
    (``viz.<msg>.title`` / ``.series.<row_key>`` / ``.note`` / ``.marker`` /
    ``.ref``) and ``params`` the raw numbers those strings interpolate, so the UI
    renders the chart in the reader's language. The English strings stay in the
    payload as the fallback for rows written before a message existed."""
    keys = [s[0] for s in series]
    days = densify_days(rows, keys, lookback_days, today)
    if not any(any(x > 0 for x in e["v"]) for e in days):
        return None
    out = {
        "kind": "daily_stack",
        "title": title,
        "unit": unit,
        "series": [{"key": s[0], "label": s[1],
                    "color": s[2] if s[2] in VIZ_COLORS else "info"}
                   for s in series],
        "days": days,
    }
    _with_msg(out, msg, params)
    if marker is not None and _day_str(marker[0]):
        out["marker"] = {"d": _day_str(marker[0]), "label": marker[1]}
    if ref is not None:
        out["ref"] = {"value": round(float(ref[0]), 2), "label": ref[1]}
    if note:
        out["note"] = note
    return out


def viz_ranked_bars(
    *,
    title: str,
    unit: str,
    rows: list[dict],
    note: str | None = None,
    keep_order: bool = False,
    msg: str | None = None,
    params: dict | None = None,
) -> dict | None:
    """Horizontal bars, largest first. Each row is ``{"label", "value", "sub",
    "color"}``: ``sub`` is a short measured fact under the label ("412 runs, 3%
    pruned"). ``keep_order`` keeps the caller's order instead, for bars that are
    read in pairs (before / after). None when there are no rows.

    ``msg``/``params`` work as in :func:`viz_daily_stack`. A row may add ``msg`` +
    ``params`` (its label, ``viz.<msg>.row.<row msg>``) and ``sub_msg`` +
    ``sub_params`` (its fact line, ``viz.<msg>.sub.<sub msg>``); rows whose label
    is a name (a model, a query shape) carry none and show verbatim."""
    rows = [r for r in rows if float(r.get("value") or 0) > 0]
    if not rows:
        return None
    if not keep_order:
        rows = sorted(rows, key=lambda r: -float(r["value"]))
    out = {
        "kind": "ranked_bars",
        "title": title,
        "unit": unit,
        "rows": [
            {
                "label": str(r.get("label") or ""),
                "value": round(float(r["value"]), 2),
                "sub": r.get("sub") or "",
                "color": r.get("color") if r.get("color") in VIZ_COLORS else "info",
                **{k: r[k] for k in ("msg", "params", "sub_msg", "sub_params") if r.get(k)},
            }
            for r in rows
        ],
    }
    _with_msg(out, msg, params)
    if note:
        out["note"] = note
    return out
