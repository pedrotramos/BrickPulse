import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Json } from "../../lib/types";
import { date, money, number } from "../../lib/format";
import { SettingChange } from "./AutostopEvidence";
import { TermHelp, useFormat } from "./Metric";
import {
  type ChartModel,
  type ChartSeries,
  SeriesChart,
  TOKEN,
} from "./SeriesChart";
import {
  isRecord,
  Legend,
  num,
  nums,
  Panel,
  records,
  type Viz,
} from "./shared";

/** System-table column behind each plotted series, by evidence kind. */
const SOURCES: Record<string, Record<string, string>> = {
  job_runs: {
    s: "system.lakeflow.job_run_timeline.result_state = 'SUCCEEDED'",
    f: "system.lakeflow.job_run_timeline.result_state ∈ (FAILED, TIMEDOUT, …)",
  },
  sql_failures: {
    ok: "system.query.history.execution_status = 'FINISHED'",
    f: "system.query.history.execution_status = 'FAILED'",
  },
  ai_gateway_errors: {
    ok: "system.ai_gateway.usage.status_code < 400",
    f: "system.ai_gateway.usage.status_code ≥ 400",
  },
  pipeline_failures: {
    ok: "system.lakeflow.pipeline_update_timeline.result_state = 'COMPLETED'",
    f: "system.lakeflow.pipeline_update_timeline.result_state = 'FAILED'",
  },
  sql_maintenance: {
    o: "system.query.history.statement_text LIKE 'OPTIMIZE%'",
    v: "system.query.history.statement_text LIKE 'VACUUM%'",
  },
  write_no_read: {
    w: "system.access.table_lineage (target_table_full_name)",
    r: "system.access.table_lineage (source_table_full_name)",
  },
  serving_traffic: {
    r: "system.serving.endpoint_usage (request count)",
    w: "system.billing.usage.usage_metadata.endpoint_id",
  },
  instance_pool: {
    l: "system.compute.node_timeline (instances started in the pool)",
    a: "system.compute.node_timeline (instances taken by a cluster)",
  },
  sharing_materialization: {
    n: "system.sharing.materialization_history",
  },
  storage_po: {
    n: "system.storage.predictive_optimization_operations_history.usage_quantity",
  },
  cpu_area: {
    cpu: "system.compute.node_timeline.cpu_user_percent + cpu_system_percent",
    idle_frac:
      "system.compute.node_timeline (minutes below the idle threshold)",
  },
  cluster_resources: {
    cpu: "system.compute.node_timeline.cpu_user_percent + cpu_system_percent",
    mem: "system.compute.node_timeline.mem_used_percent",
    swap: "system.compute.node_timeline.mem_swap_percent",
    io_wait: "system.compute.node_timeline.cpu_wait_percent",
    net: "system.compute.node_timeline.network_sent_bytes + network_received_bytes",
  },
  cluster_contention: {
    solo: "system.compute.node_timeline × system.lakeflow.job_task_run_timeline (1 run)",
    concurrent:
      "system.compute.node_timeline × system.lakeflow.job_task_run_timeline (≥ 2 runs)",
    p95: "system.compute.node_timeline (p95 while ≥ 2 runs overlapped)",
  },
  driver_worker_split: {
    driver: "system.compute.node_timeline (driver = true)",
    worker: "system.compute.node_timeline (driver = false)",
  },
  wh_scale_out: {
    ok: "system.query.history.start_time (hour)",
    queued: "system.query.history.waiting_at_capacity_duration_ms > 0",
  },
  burst_compare: {
    load: "system.query.history (concurrent queries, share of peak)",
  },
};

const dayRows = (viz: Viz) =>
  records(viz.days).filter((d) => typeof d.d === "string");

function useKind(kind: string) {
  const { t } = useTranslation();
  const text = (key: string, params?: Record<string, unknown>) =>
    t(`chart.${kind}.${key}`, params ?? {});
  const series = (
    key: string,
    color: string,
    extra: Partial<ChartSeries> = {},
  ): ChartSeries => ({
    key,
    label: text(key),
    help: text(`${key}Help`),
    source: SOURCES[kind]?.[key],
    color,
    ...extra,
  });
  const base = (
    rows: ChartModel["rows"],
    s: ChartSeries[],
    extra: Partial<ChartModel> = {},
  ): ChartModel => ({
    id: kind,
    title: text("title"),
    x: { label: t("chart.axisDay"), type: "date" },
    y: { label: text("y"), unit: "count" },
    rows,
    series: s,
    ...extra,
  });
  return { t, text, series, base };
}

/** Per-day kinds: one stacked or grouped bar per calendar day. */
function DailyKind({ viz }: { viz: Viz }) {
  const kind = String(viz.kind);
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const { text, series, base } = useKind(kind);
  const pct = (v: Json | undefined) =>
    new Intl.NumberFormat(lang, {
      style: "percent",
      maximumFractionDigits: 1,
    }).format(num(v) ?? 0);
  const n = (v: Json | undefined) => number(num(v) ?? 0, lang);
  const days = dayRows(viz);
  const failed = (okLabel = "ok") =>
    days.map((d) => ({
      x: String(d.d),
      [okLabel]: Math.max(0, (num(d.t) ?? 0) - (num(d.f) ?? 0)),
      f: num(d.f) ?? 0,
    }));
  let model: ChartModel;
  let extra: ReactNode = null;
  switch (kind) {
    case "job_runs":
      model = base(
        days.map((d) => ({
          x: String(d.d),
          s: num(d.s) ?? 0,
          f: num(d.f) ?? 0,
        })),
        [
          series("s", TOKEN.info, { stack: true }),
          series("f", TOKEN.danger, { stack: true }),
        ],
        {
          note: text("note", {
            rate: pct(viz.fail_rate),
            baseline: pct(viz.baseline_fail_rate),
            cost: money(num(viz.failed_cost) ?? 0, lang),
          }),
        },
      );
      break;
    case "sql_failures":
    case "ai_gateway_errors":
    case "pipeline_failures":
      model = base(
        failed(),
        [
          series("ok", TOKEN.info, { stack: true }),
          series("f", TOKEN.danger, { stack: true }),
        ],
        {
          note: text("note", {
            rate: pct(viz.fail_rate ?? viz.error_rate),
            failed: n(
              viz.failed_queries ?? viz.failed_requests ?? viz.failed_updates,
            ),
            total: n(
              viz.total_queries ?? viz.total_requests ?? viz.total_updates,
            ),
          }),
        },
      );
      break;
    case "sql_maintenance":
      model = base(
        days.map((d) => ({
          x: String(d.d),
          o: num(d.o) ?? 0,
          v: num(d.v) ?? 0,
        })),
        [
          series("o", TOKEN.info, { stack: true }),
          series("v", TOKEN.violet, { stack: true }),
        ],
        {
          note: text("note", {
            optimize: n(viz.optimize_runs),
            vacuum: n(viz.vacuum_runs),
          }),
        },
      );
      break;
    case "write_no_read":
      model = base(
        days.map((d) => ({
          x: String(d.d),
          w: num(d.w) ?? 0,
          r: num(d.r) ?? 0,
        })),
        [series("w", TOKEN.info), series("r", TOKEN.warn, { mark: "line" })],
        {
          note: text("note", {
            writes: n(viz.total_writes),
            reads: n(viz.total_reads),
          }),
        },
      );
      break;
    case "serving_traffic":
      model = base(
        days.map((d) => ({ x: String(d.d), r: num(d.r) ?? 0 })),
        [series("r", TOKEN.info)],
        {
          note: text("note", {
            billed: n(viz.days_billed),
            days: n(days.length),
            requests: n(viz.total_requests),
          }),
        },
      );
      extra = (
        <DayStrip
          title={text("billedTitle")}
          help={text("wHelp")}
          source={SOURCES.serving_traffic.w}
          days={days.map((d) => ({ d: String(d.d), on: (num(d.w) ?? 0) > 0 }))}
          onLabel={text("billed")}
          offLabel={text("notBilled")}
        />
      );
      break;
    case "instance_pool":
      model = base(
        days.map((d) => ({
          x: String(d.d),
          l: num(d.l) ?? 0,
          a: num(d.a) ?? 0,
        })),
        [series("l", TOKEN.warn), series("a", TOKEN.info)],
        {
          note: text("note", {
            floor: n(viz.min_idle_instances),
            node: String(viz.node_type ?? ""),
          }),
          refY:
            num(viz.min_idle_instances) != null
              ? [{ value: num(viz.min_idle_instances)!, label: text("floor") }]
              : undefined,
        },
      );
      break;
    case "sharing_materialization":
      model = base(
        days.map((d) => ({ x: String(d.d), n: num(d.n) ?? 0 })),
        [series("n", TOKEN.info)],
        {
          note: text("note", {
            count: n(viz.materializations),
            tables: n(viz.distinct_tables),
            recipients: n(viz.distinct_recipients),
          }),
        },
      );
      break;
    default: // storage_po
      model = base(
        days.map((d) => ({ x: String(d.d), n: num(d.n) ?? 0 })),
        [series("n", TOKEN.info)],
        {
          y: { label: text("y"), unit: "dbu" },
          note: text("note", {
            total: number(num(viz.po_dbus) ?? 0, lang, 1),
            compaction: number(num(viz.compaction_dbus) ?? 0, lang, 1),
            clustering: number(num(viz.clustering_dbus) ?? 0, lang, 1),
            vacuum: number(num(viz.vacuum_dbus) ?? 0, lang, 1),
          }),
        },
      );
  }
  if (!days.length) return <p className="muted">{t("chartUnavailable")}</p>;
  // A window of zeros is the finding itself; an empty plot would only hide it.
  const empty = model.rows.every((r) =>
    model.series.every((s) => !Number(r[s.key])),
  );
  return (
    <>
      {empty ? (
        <Panel title={model.title} note={model.note}>
          <p className="ev-empty">
            {t("chart.allZero", {
              series: model.series.map((s) => s.label).join(", "),
            })}
          </p>
        </Panel>
      ) : (
        <SeriesChart model={model} />
      )}
      {extra}
    </>
  );
}

/** One cell per day: did the resource bill that day? */
function DayStrip({
  title,
  help,
  source,
  days,
  onLabel,
  offLabel,
}: {
  title: string;
  help: string;
  source: string;
  days: { d: string; on: boolean }[];
  onLabel: string;
  offLabel: string;
}) {
  const { i18n } = useTranslation();
  const lang = i18n.language;
  return (
    <Panel title={title}>
      <ol className="day-strip" aria-label={title}>
        {days.map((day) => (
          <li
            key={day.d}
            className={day.on ? "state-2" : "state-0"}
            title={`${date(day.d, lang, true)} · ${day.on ? onLabel : offLabel}`}
          >
            <span className="sr-only">
              {date(day.d, lang, true)}: {day.on ? onLabel : offLabel}
            </span>
          </li>
        ))}
      </ol>
      {days.length > 0 && (
        <div className="day-strip-axis" aria-hidden="true">
          <span>{date(days[0].d, lang, true)}</span>
          <span>{date(days[days.length - 1].d, lang, true)}</span>
        </div>
      )}
      <div className="series-legend">
        <Legend
          items={[
            { label: onLabel, swatch: "swatch-idle" },
            { label: offLabel, swatch: "swatch-off" },
          ]}
        />
        <TermHelp term={{ key: "", help, source }} label={title} />
      </div>
    </Panel>
  );
}

/** Hourly time series from the node timeline (CPU and the other resources). */
function TimelineKind({ viz }: { viz: Viz }) {
  const kind = String(viz.kind);
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const { text, series, base } = useKind(kind);
  const iso = (ts: number) => new Date(ts * 1000).toISOString();
  const pct = (v: number | null) =>
    v == null
      ? "—"
      : new Intl.NumberFormat(lang, {
          style: "percent",
          maximumFractionDigits: 0,
        }).format(v);
  if (kind === "cpu_area") {
    const cpu = nums(viz.cpu);
    const ts = nums(viz.ts);
    const idle = nums(viz.idle_frac);
    if (!cpu.length) return <p className="muted">{t("chartUnavailable")}</p>;
    const rows = cpu.map((v, i) => ({
      x: ts.length === cpu.length ? iso(ts[i]) : String(i + 1),
      cpu: v,
      ...(idle.length === cpu.length ? { idle_frac: idle[i] } : {}),
    }));
    return (
      <SeriesChart
        model={base(
          rows,
          [
            series("cpu", TOKEN.info, { mark: "area" }),
            ...(idle.length === cpu.length
              ? [series("idle_frac", TOKEN.warn, { mark: "line", off: true })]
              : []),
          ],
          {
            x: {
              label: t("chart.axisTime"),
              type: ts.length === cpu.length ? "time" : "category",
            },
            y: { label: text("y"), unit: "fraction", max: 1 },
            note: text("note", {
              p95: pct(num(viz.p95)),
              threshold: pct(num(viz.idle_threshold)),
            }),
            refY: [
              ...(num(viz.idle_threshold) != null
                ? [
                    {
                      value: num(viz.idle_threshold)!,
                      label: text("threshold"),
                    },
                  ]
                : []),
              ...(num(viz.p95) != null
                ? [{ value: num(viz.p95)!, label: text("p95") }]
                : []),
            ],
          },
        )}
      />
    );
  }
  // cluster_resources
  const rows = records(viz.series).filter((r) => typeof r.ts === "number");
  if (!rows.length) return <p className="muted">{t("chartUnavailable")}</p>;
  const present = ["cpu", "mem", "swap", "io_wait", "net"].filter((k) =>
    rows.some((r) => typeof r[k] === "number"),
  );
  const colors: Record<string, string> = {
    cpu: TOKEN.info,
    mem: TOKEN.violet,
    swap: TOKEN.danger,
    io_wait: TOKEN.warn,
    net: TOKEN.track,
  };
  const stats = isRecord(viz.stats) ? viz.stats : {};
  return (
    <SeriesChart
      model={base(
        rows.map((r) => ({
          x: iso(Number(r.ts)),
          ...Object.fromEntries(present.map((k) => [k, num(r[k])])),
        })),
        present.map((k) =>
          series(k, colors[k], {
            mark: "line",
            off: k === "swap" || k === "net",
          }),
        ),
        {
          x: { label: t("chart.axisTime"), type: "time" },
          y: { label: text("y"), unit: "fraction", max: 1 },
          note: text("note", {
            cpu: pct((num(stats.cpu_avg_pct) ?? 0) / 100),
            cpuPeak: pct((num(stats.cpu_peak_pct) ?? 0) / 100),
            mem: pct((num(stats.mem_avg_pct) ?? 0) / 100),
            memPeak: pct((num(stats.mem_peak_pct) ?? 0) / 100),
          }),
        },
      )}
    />
  );
}

/** Two measures side by side (solo vs concurrent, driver vs workers). */
function CompareKind({ viz }: { viz: Viz }) {
  const kind = String(viz.kind);
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const { text, series, base } = useKind(kind);
  const n = (v: Json | undefined) => number(num(v) ?? 0, lang);
  const metrics = [
    { x: t("chart.cpu"), prefix: "cpu" },
    { x: t("chart.memory"), prefix: "mem" },
  ];
  if (kind === "cluster_contention") {
    return (
      <SeriesChart
        model={base(
          metrics.map((m) => ({
            x: m.x,
            solo: num(viz[`${m.prefix}_solo`]),
            concurrent: num(viz[`${m.prefix}_concurrent`]),
            p95: num(viz[`${m.prefix}_concurrent_p95`]),
          })),
          [
            series("solo", TOKEN.track),
            series("concurrent", TOKEN.info),
            series("p95", TOKEN.warn),
          ],
          {
            x: { label: t("chart.axisResource"), type: "category" },
            y: { label: text("y"), unit: "pct", max: 100 },
            note: text("note", {
              runs: n(viz.peak_concurrent_runs),
              minutes: n(viz.contended_minutes),
              jobs: n(viz.contended_job_count),
            }),
          },
        )}
      />
    );
  }
  const hasWorkers = num(viz.worker_cpu) != null || num(viz.worker_mem) != null;
  return (
    <SeriesChart
      model={base(
        metrics.map((m) => ({
          x: m.x,
          driver: num(viz[`driver_${m.prefix}`]),
          worker: num(viz[`worker_${m.prefix}`]),
        })),
        [
          series("driver", TOKEN.warn),
          ...(hasWorkers ? [series("worker", TOKEN.info)] : []),
        ],
        {
          x: { label: t("chart.axisResource"), type: "category" },
          y: { label: text("y"), unit: "pct", max: 100 },
          note: text(`symptom.${String(viz.symptom ?? "default")}`, {
            workers: n(viz.worker_count),
            defaultValue: text("symptom.default", {
              workers: n(viz.worker_count),
            }),
          }),
        },
      )}
    />
  );
}

/** Warehouse scale-out: the max_clusters change and when queries queue. */
function ScaleOutKind({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const format = useFormat();
  const { text, series, base } = useKind("wh_scale_out");
  const q = nums(viz.queries_by_hour);
  const queued = nums(viz.queued_by_hour);
  return (
    <>
      <Panel
        title={text("settingTitle")}
        note={text("settingNote", {
          queued: format(num(viz.queued_pct) ?? 0, "pct"),
          p95: format(num(viz.p95_queue_wait_s) ?? 0, "seconds"),
        })}
      >
        <SettingChange
          label={text("setting")}
          from={n(viz.current_max_clusters, lang)}
          to={n(viz.proposed_max_clusters, lang)}
        />
      </Panel>
      {q.length === 24 && (
        <SeriesChart
          model={base(
            q.map((v, h) => ({
              x: h,
              ok: Math.max(0, v - (queued[h] ?? 0)),
              queued: queued[h] ?? 0,
            })),
            [
              series("ok", TOKEN.info, { stack: true }),
              series("queued", TOKEN.warn, { stack: true }),
            ],
            {
              x: { label: t("chart.axisHour"), type: "hour" },
              note: text("note", {
                minutes:
                  num(viz.auto_stop_minutes) != null
                    ? format(num(viz.auto_stop_minutes)!, "minutes")
                    : t("notAvailable"),
              }),
            },
          )}
        />
      )}
    </>
  );
}
const n = (v: Json | undefined, lang: string) => number(num(v) ?? 0, lang);

/** Serverless candidate: query load against the warm floor a classic warehouse bills. */
function BurstKind({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const { text, series, base } = useKind("burst_compare");
  const load = nums(viz.load);
  if (!load.length) return <p className="muted">{t("chartUnavailable")}</p>;
  return (
    <SeriesChart
      model={base(
        load.map((v, i) => ({ x: String(i + 1), load: v })),
        [series("load", TOKEN.info, { mark: "area" })],
        {
          x: { label: t("chart.axisPeriod"), type: "category" },
          y: { label: text("y"), unit: "fraction", max: 1 },
          refY:
            num(viz.warm_floor) != null
              ? [{ value: num(viz.warm_floor)!, label: text("floor") }]
              : undefined,
          note: text("note", {
            idle: number(num(viz.idle_pct) ?? 0, lang, 1),
            cold: number(num(viz.cold_start_s) ?? 0, lang, 1),
          }),
        },
      )}
    />
  );
}

/** Localized text for a generic payload: catalog message, else the stored English. */
function useMessage(viz: Viz) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const msg = typeof viz.msg === "string" ? viz.msg : null;
  const params = Object.fromEntries(
    Object.entries(isRecord(viz.params) ? viz.params : {}).map(([k, v]) => [
      k,
      typeof v === "number" ? number(v, lang, 1) : String(v ?? ""),
    ]),
  );
  return (path: string, fallback: string, extra?: Record<string, Json>) => {
    if (!msg) return fallback;
    const local = Object.fromEntries(
      Object.entries(extra ?? {}).map(([k, v]) => [
        k,
        typeof v === "number" ? number(v, lang, 1) : String(v ?? ""),
      ]),
    );
    return t(`viz.${msg}.${path}`, {
      ...params,
      ...local,
      defaultValue: fallback,
    });
  };
}

/** Per-day stacked bars described by the payload itself (title, series, marker). */
function DailyStackEvidence({ viz }: { viz: Viz }) {
  const { t } = useTranslation();
  const say = useMessage(viz);
  const series = records(viz.series);
  const days = records(viz.days).filter((d) => typeof d.d === "string");
  const unit = String(viz.unit ?? "");
  const money = unit === "$" || unit === "$/mo";
  const marker = isRecord(viz.marker) ? viz.marker : null;
  const ref = isRecord(viz.ref) ? viz.ref : null;
  const keyOf = (s: Record<string, Json>, i: number) =>
    typeof s.key === "string" ? s.key : String(i);
  return (
    <SeriesChart
      model={{
        id: "daily_stack",
        title: say("title", String(viz.title ?? "")),
        note: typeof viz.note === "string" ? say("note", viz.note) : undefined,
        x: { label: t("chart.axisDay"), type: "date" },
        y: {
          label: say("unit", unit),
          unit: money ? "usd" : unit === "h" ? "hours" : "count",
        },
        rows: days.map((d) => ({
          x: String(d.d),
          ...Object.fromEntries(
            series.map((s, i) => [keyOf(s, i), nums(d.v)[i] ?? 0]),
          ),
        })),
        series: series.map((s, i) => ({
          key: keyOf(s, i),
          label: say(`series.${keyOf(s, i)}`, String(s.label ?? "")),
          help: say(`help.${keyOf(s, i)}`, ""),
          color: TOKEN[String(s.color)] ?? TOKEN.info,
          stack: true,
        })),
        refX:
          marker && typeof marker.d === "string"
            ? [
                {
                  x: marker.d,
                  label: `${say("marker", String(marker.label ?? ""))} · ${marker.d}`,
                },
              ]
            : undefined,
        refY:
          ref && num(ref.value) != null
            ? [
                {
                  value: num(ref.value)!,
                  label: say("ref", String(ref.label ?? "")),
                },
              ]
            : undefined,
      }}
    />
  );
}

/** Horizontal bars, largest first, each with its measured fact line. */
function RankedBarsEvidence({ viz }: { viz: Viz }) {
  const { i18n } = useTranslation();
  const lang = i18n.language;
  const say = useMessage(viz);
  const rows = records(viz.rows);
  const unit = String(viz.unit ?? "");
  const max = Math.max(0, ...rows.map((r) => num(r.value) ?? 0));
  const value = (v: number) =>
    unit === "$/mo"
      ? say("perMonth", `${money(v, lang)}/mo`, { value: money(v, lang) })
      : unit === "¢/turn"
        ? say("perTurn", `${number(v, lang, 2)}¢/turn`, {
            value: number(v, lang, 2),
          })
        : `${number(v, lang, 2)} ${unit}`;
  return (
    <Panel
      title={say("title", String(viz.title ?? ""))}
      note={typeof viz.note === "string" ? say("note", viz.note) : undefined}
    >
      <ul className="share-list ranked-list">
        {rows.map((r, i) => {
          const label =
            typeof r.msg === "string"
              ? say(
                  `row.${r.msg}`,
                  String(r.label ?? ""),
                  isRecord(r.params) ? r.params : {},
                )
              : String(r.label ?? "");
          const sub =
            typeof r.sub_msg === "string"
              ? say(
                  `sub.${r.sub_msg}`,
                  String(r.sub ?? ""),
                  isRecord(r.sub_params) ? r.sub_params : {},
                )
              : String(r.sub ?? "");
          const v = num(r.value) ?? 0;
          return (
            <li key={i}>
              <div>
                <span
                  className="share-label"
                  lang={typeof r.msg === "string" ? undefined : "en"}
                >
                  {label}
                </span>
                <strong>{value(v)}</strong>
              </div>
              <div className="share-track" aria-hidden="true">
                <span
                  style={{
                    width: `${max > 0 ? (v / max) * 100 : 0}%`,
                    background: TOKEN[String(r.color)] ?? TOKEN.info,
                  }}
                />
              </div>
              {sub && <p>{sub}</p>}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

const VIEWS: Record<string, (props: { viz: Viz }) => JSX.Element> = {
  job_runs: DailyKind,
  sql_failures: DailyKind,
  ai_gateway_errors: DailyKind,
  pipeline_failures: DailyKind,
  sql_maintenance: DailyKind,
  write_no_read: DailyKind,
  serving_traffic: DailyKind,
  instance_pool: DailyKind,
  sharing_materialization: DailyKind,
  storage_po: DailyKind,
  cpu_area: TimelineKind,
  cluster_resources: TimelineKind,
  cluster_contention: CompareKind,
  driver_worker_split: CompareKind,
  wh_scale_out: ScaleOutKind,
  burst_compare: BurstKind,
  daily_stack: DailyStackEvidence,
  ranked_bars: RankedBarsEvidence,
};

export const SPEC_KINDS = new Set(Object.keys(VIEWS));
export default function SpecEvidence({ viz }: { viz: Viz }) {
  const View = VIEWS[String(viz.kind)];
  return View ? <View viz={viz} /> : null;
}
