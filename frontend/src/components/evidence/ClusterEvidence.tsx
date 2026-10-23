import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Json } from "../../lib/types";
import { date, hour, number } from "../../lib/format";
import {
  DataTable,
  dayLabel,
  HourDrill,
  IntensityLegend,
  Legend,
  level,
  num,
  nums,
  Panel,
  records,
  TooltipBox,
  useDuration,
  type Viz,
  WeekHeatmap,
  weekday,
} from "./shared";

export function ClusterDailyEvidence({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const today = new Date().toISOString().slice(0, 10);
  const days = records(viz.days).map((d) => ({
    d: String(d.d ?? ""),
    a: num(d.a) ?? 0,
    i: num(d.i) ?? 0,
    pk: num(d.pk) ?? 0,
  }));
  const last = typeof viz.last_active === "string" ? viz.last_active : null;
  const lookback = num(viz.lookback_days) ?? days.length;
  const active = days.reduce((s, d) => s + d.a, 0);
  const idle = days.reduce((s, d) => s + d.i, 0);
  const hours = (v: number) => t("ev.hours", { value: number(v, lang, 1) });
  return (
    <Panel
      title={t("ev.dailyTitle")}
      note={t("ev.dailyNote", {
        days: lookback,
        threshold: number((num(viz.idle_threshold) ?? 0.1) * 100, lang),
        peak: number((num(viz.peak_overall) ?? 0) * 100, lang),
      })}
    >
      <div
        className="chart"
        role="img"
        aria-label={`${t("ev.dailyTitle")}: ${t("ev.inUse")} ${hours(active)}, ${t("ev.idle")} ${hours(idle)}${last ? `, ${t("ev.lastActive")} ${date(last, lang, true)}` : ""}`}
      >
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={days}
            margin={{ top: 18, right: 12, bottom: 0, left: 0 }}
          >
            <CartesianGrid vertical={false} stroke="var(--line)" />
            <XAxis
              dataKey="d"
              tickFormatter={(d) => date(String(d), lang, true)}
              tickLine={false}
              axisLine={false}
              minTickGap={30}
            />
            <YAxis
              domain={[0, 24]}
              ticks={[0, 6, 12, 18, 24]}
              width={44}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v) => hours(Number(v))}
            />
            <Tooltip
              cursor={{ fill: "var(--control-hover)" }}
              content={({ active: on, payload }) => {
                const d = on
                  ? (payload?.[0]?.payload as (typeof days)[number] | undefined)
                  : undefined;
                return d ? (
                  <TooltipBox>
                    <strong>
                      {date(d.d, lang, true)}
                      {d.d >= today ? ` · ${t("ev.partialDay")}` : ""}
                    </strong>
                    <span>
                      {t("ev.inUse")}: {hours(d.a)}
                    </span>
                    <span>
                      {t("ev.idle")}: {hours(d.i)}
                    </span>
                    <span>
                      {t("ev.peakCpu")}: {number(d.pk * 100, lang)}%
                    </span>
                  </TooltipBox>
                ) : null;
              }}
            />
            <Bar
              dataKey="a"
              stackId="day"
              fill="var(--chart-active)"
              isAnimationActive={false}
            />
            <Bar
              dataKey="i"
              stackId="day"
              fill="var(--chart-idle)"
              isAnimationActive={false}
            />
            {last && days.some((d) => d.d === last) && (
              <ReferenceLine
                x={last}
                stroke="var(--chart-prior)"
                strokeDasharray="4 3"
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <Legend
        items={[
          { label: t("ev.inUse"), swatch: "swatch-active" },
          { label: t("ev.idle"), swatch: "swatch-idle" },
          ...(last ? [{ label: t("ev.lastActive"), swatch: "dashed" }] : []),
        ]}
      />
      <DataTable
        rows={days}
        caption={t("ev.dailyTitle")}
        columns={[
          { key: "d", label: t("day"), render: (d) => date(d.d, lang, true) },
          {
            key: "a",
            label: t("ev.inUse"),
            numeric: true,
            render: (d) => number(d.a, lang, 2),
          },
          {
            key: "i",
            label: t("ev.idle"),
            numeric: true,
            render: (d) => number(d.i, lang, 2),
          },
          {
            key: "pk",
            label: t("ev.peakCpu"),
            numeric: true,
            render: (d) => `${number(d.pk * 100, lang)}%`,
          },
        ]}
      />
    </Panel>
  );
}

export function ClusterWeekEvidence({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const duration = useDuration();
  const active = nums(viz.active_by_dow_hour);
  const up = nums(viz.up_by_dow_hour);
  const states = up.length === 168;
  const days = num(viz.lookback_days) ?? 30;
  const max = Math.max(0, ...active);
  const totalActive = active.reduce((a, b) => a + b, 0);
  const totalIdle = states
    ? up.reduce((s, v, k) => s + Math.max(0, v - active[k]), 0)
    : 0;
  const byHour = Array.from({ length: 24 }, (_, h) =>
    Array.from({ length: 7 }, (_, d) => active[d * 24 + h]).reduce(
      (a, b) => a + b,
      0,
    ),
  );
  return (
    <>
      <Panel
        title={t("ev.clusterWeekTitle")}
        note={
          states
            ? t("ev.clusterWeekNote", {
                days,
                active: duration(totalActive),
                idle: duration(totalIdle),
              })
            : t("ev.summedOver", { days })
        }
      >
        <WeekHeatmap
          label={t("ev.clusterWeekTitle")}
          rows={Array.from({ length: 7 }, (_, d) => ({
            label: weekday(d, lang),
            cells: Array.from({ length: 24 }, (_, h) => {
              const k = d * 24 + h;
              const at = `${weekday(d, lang)} · ${hour(h, lang)} UTC`;
              if (!states)
                return {
                  className: `heat-level-${level(active[k], max)}`,
                  title: `${at} · ${t("ev.minutesInUse")}: ${number(active[k], lang)}`,
                };
              const idle = Math.max(0, up[k] - active[k]);
              return {
                className:
                  up[k] <= 0 && active[k] <= 0
                    ? "state-0"
                    : active[k] >= idle
                      ? "state-1"
                      : "state-2",
                title: `${at} · ${t("ev.inUse")}: ${duration(active[k])} · ${t("ev.idle")}: ${duration(idle)}`,
              };
            }),
          }))}
        />
        {states ? (
          <Legend
            items={[
              { label: t("ev.inUse"), swatch: "swatch-active" },
              { label: t("ev.idle"), swatch: "swatch-idle" },
              { label: t("ev.stopped"), swatch: "swatch-off" },
            ]}
          />
        ) : (
          <IntensityLegend />
        )}
      </Panel>
      <Panel
        title={t("ev.clusterDayTitle")}
        note={t(
          nums(viz.active_by_hour_minute).length === 1440
            ? "ev.dayNote"
            : "ev.dayNoteStatic",
        )}
      >
        <HourDrill
          title={t("ev.clusterDayTitle")}
          byHour={byHour}
          byMinute={nums(viz.active_by_hour_minute)}
          unit={t("ev.minutesInUse")}
        />
      </Panel>
    </>
  );
}

/** Day × hour state grid (warehouse auto-stop, keep-alive, pipelines). */
type Run = { state: number | null; from: number; to: number };
const runsOf = (row: (number | null)[]) =>
  row.reduce<Run[]>((runs, state, h) => {
    const last = runs[runs.length - 1];
    if (last && last.state === state) last.to = h + 1;
    else runs.push({ state, from: h, to: h + 1 });
    return runs;
  }, []);
export function StateHeatmapEvidence({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const grid = (
    Array.isArray(viz.grid) ? viz.grid.filter(Array.isArray) : []
  ).map((row) =>
    (row as Json[]).map((cell) =>
      typeof cell === "number"
        ? Math.min(2, Math.max(0, Math.round(cell)))
        : null,
    ),
  );
  const labels = Array.isArray(viz.day_labels) ? viz.day_labels : [];
  const dates = Array.isArray(viz.day_dates) ? viz.day_dates : [];
  const calendar = dates.length === grid.length;
  const day = (i: number) =>
    calendar
      ? new Intl.DateTimeFormat(lang, {
          weekday: "short",
          day: "numeric",
          month: "short",
          timeZone: "UTC",
        }).format(new Date(`${String(dates[i])}T12:00:00Z`))
      : dayLabel(String(labels[i] ?? i + 1), lang);
  const names = [t("stopped"), t("active"), t("idle")];
  const stateName = (s: number | null) =>
    s == null ? t("ev.noData") : names[s];
  const context = String(viz.context ?? "warehouse");
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<{ d: number; h: number } | null>(null);
  const count = (row: (number | null)[], s: number) =>
    row.filter((c) => c === s).length;
  const totals = [0, 1, 2].map((s) =>
    grid.reduce((a, row) => a + count(row, s), 0),
  );
  const reclaimable = num(viz.reclaimable_hours);
  const at = (d: number, h: number) =>
    `${day(d)} · ${hour(h, lang)}–${hour((h + 1) % 24, lang)} UTC · ${stateName(grid[d][h])}`;
  const span = (r: Run) => `${hour(r.from, lang)}–${hour(r.to % 24, lang)}`;
  return (
    <Panel
      title={t(`ev.stateTitle.${context}`, { defaultValue: t("heatmap") })}
      note={t(calendar ? "ev.stateNoteCalendar" : "ev.stateNoteProfile", {
        idle: number(totals[2], lang),
        active: number(totals[1], lang),
        days: number(grid.length, lang),
      })}
    >
      {reclaimable != null && (
        <p className="ev-note">
          {t(`ev.reclaimable.${context}`, {
            value: number(reclaimable, lang, 1),
            defaultValue: t("ev.reclaimable.warehouse", {
              value: number(reclaimable, lang, 1),
            }),
          })}
        </p>
      )}
      <div className="heatmap state-grid" onMouseLeave={() => setHover(null)}>
        <div className="heatmap-row heatmap-hours" aria-hidden="true">
          <span />
          {Array.from({ length: 24 }, (_, h) => (
            <span key={h}>{h % 6 === 0 ? hour(h, lang) : ""}</span>
          ))}
          <span className="heatmap-total">{t("idle")}</span>
        </div>
        {grid.map((row, d) => (
          <div
            className={`heatmap-row${selected === d ? " is-selected" : ""}`}
            key={d}
          >
            <button
              type="button"
              className="heatmap-day"
              aria-pressed={selected === d}
              aria-label={t("ev.dayDetail", {
                day: day(d),
                idle: count(row, 2),
                active: count(row, 1),
              })}
              onClick={() => setSelected(selected === d ? null : d)}
            >
              {day(d)}
            </button>
            {row.map((cell, h) => (
              <span
                key={h}
                aria-hidden="true"
                className={`heat-cell ${cell == null ? "state-none" : `state-${cell}`}`}
                onMouseEnter={() => setHover({ d, h })}
                onClick={() => setSelected(d)}
              />
            ))}
            <span className="heatmap-total" aria-hidden="true">
              {t("ev.hoursShort", { value: count(row, 2) })}
            </span>
          </div>
        ))}
      </div>
      <p className="heatmap-readout" aria-hidden="true">
        {hover ? at(hover.d, hover.h) : t("ev.heatmapHint")}
      </p>
      <Legend
        items={[
          { label: t("active"), swatch: "swatch-active" },
          { label: t("idle"), swatch: "swatch-idle" },
          { label: t("stopped"), swatch: "swatch-off" },
          ...(grid.some((r) => r.includes(null))
            ? [{ label: t("ev.noData"), swatch: "swatch-none" }]
            : []),
        ]}
      />
      {selected != null && grid[selected] && (
        <section className="day-detail" aria-live="polite">
          <div className="drill-head">
            <strong>{day(selected)}</strong>
            <button
              type="button"
              className="subtle"
              onClick={() => setSelected(null)}
            >
              {t("ev.closeDay")}
            </button>
          </div>
          <p className="ev-note">
            {t("ev.daySummary", {
              active: count(grid[selected], 1),
              idle: count(grid[selected], 2),
              stopped: count(grid[selected], 0),
            })}
          </p>
          <ol className="run-list">
            {runsOf(grid[selected]).map((r) => (
              <li key={r.from}>
                <i
                  className={
                    r.state == null
                      ? "swatch-none"
                      : `swatch-${["off", "active", "idle"][r.state]}`
                  }
                  aria-hidden="true"
                />
                <span className="run-span">{span(r)} UTC</span>
                <span>{stateName(r.state)}</span>
                <span className="run-length">
                  {t("ev.hoursShort", { value: r.to - r.from })}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}
      <DataTable
        rows={grid.map((row, d) => ({ row, d }))}
        caption={t("heatmap")}
        columns={[
          { key: "day", label: t("day"), render: (r) => day(r.d) },
          {
            key: "active",
            label: t("active"),
            numeric: true,
            render: (r) => t("ev.hoursShort", { value: count(r.row, 1) }),
          },
          {
            key: "idle",
            label: t("idle"),
            numeric: true,
            render: (r) => t("ev.hoursShort", { value: count(r.row, 2) }),
          },
          {
            key: "stopped",
            label: t("stopped"),
            numeric: true,
            render: (r) => t("ev.hoursShort", { value: count(r.row, 0) }),
          },
          {
            key: "idleRuns",
            label: t("ev.idleSpans"),
            render: (r) =>
              runsOf(r.row)
                .filter((x) => x.state === 2)
                .map(span)
                .join(", ") || "—",
          },
        ]}
      />
    </Panel>
  );
}
