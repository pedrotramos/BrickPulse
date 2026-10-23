import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { date, hour } from "../../lib/format";
import type { Unit } from "../../lib/glossary";
import { TermHelp, useFormat } from "./Metric";
import { DataTable, Panel, TooltipBox } from "./shared";

export type ChartSeries = {
  key: string;
  label: string;
  help?: string;
  /** System-table column the series is read from. */
  source?: string;
  color: string;
  mark?: "bar" | "line" | "area";
  stack?: boolean;
  /** Starts switched off; the reader can turn it on from the legend. */
  off?: boolean;
};
export type ChartModel = {
  id: string;
  title: string;
  note?: string;
  x: { label: string; type: "date" | "time" | "hour" | "category" };
  y: { label: string; unit: Unit; max?: number };
  rows: Record<string, number | string | null>[];
  series: ChartSeries[];
  refY?: { value: number; label: string }[];
  refX?: { x: string; label: string }[];
};

/** Design tokens the backend names in generic payloads. */
export const TOKEN: Record<string, string> = {
  info: "var(--chart-active)",
  warn: "var(--chart-idle)",
  danger: "var(--chart-danger)",
  savings: "var(--chart-savings)",
  violet: "var(--chart-violet)",
  track: "var(--chart-prior)",
};

/** One measured chart: title, axis names, a legend that toggles series, a data table. */
export function SeriesChart({ model }: { model: ChartModel }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const format = useFormat();
  const [hidden, setHidden] = useState(
    () => new Set(model.series.filter((s) => s.off).map((s) => s.key)),
  );
  const shown = model.series.filter((s) => !hidden.has(s.key));
  const xText = (v: unknown, long = false) => {
    const s = String(v);
    if (model.x.type === "date") return date(s.slice(0, 10), lang, !long);
    if (model.x.type === "time")
      return long ? `${date(s, lang)} UTC` : date(s.slice(0, 10), lang, true);
    if (model.x.type === "hour")
      return `${hour(Number(v), lang)}${long ? " UTC" : ""}`;
    return s;
  };
  // The widest tick sets the axis width, so large values never clip at the edge.
  const peak = Math.max(
    model.y.max ?? 0,
    ...model.rows.map((r) => {
      const stacked = shown
        .filter((s) => s.stack)
        .reduce((a, s) => a + (Number(r[s.key]) || 0), 0);
      return Math.max(stacked, ...shown.map((s) => Number(r[s.key]) || 0));
    }),
  );
  const yWidth = Math.min(
    96,
    Math.max(36, format(peak, model.y.unit, true).length * 7 + 12),
  );
  const toggle = (key: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else if (model.series.length - next.size > 1) next.add(key);
      return next;
    });
  return (
    <Panel title={model.title} note={model.note}>
      <div className="series-legend">
        {model.series.map((s) => (
          <span key={s.key} className="series-legend-item">
            {model.series.length > 1 ? (
              <button
                type="button"
                className="series-toggle"
                aria-pressed={!hidden.has(s.key)}
                onClick={() => toggle(s.key)}
              >
                <i
                  className={s.mark === "line" ? "line" : ""}
                  style={{ background: s.color }}
                  aria-hidden="true"
                />
                {s.label}
              </button>
            ) : (
              <span className="series-name">
                <i
                  className={s.mark === "line" ? "line" : ""}
                  style={{ background: s.color }}
                  aria-hidden="true"
                />
                {s.label}
              </span>
            )}
            <TermHelp
              term={{ key: "", help: s.help, source: s.source }}
              label={s.label}
            />
          </span>
        ))}
      </div>
      <p className="axis-title axis-title-y" aria-hidden="true">
        {model.y.label}
      </p>
      <div
        className="chart"
        role="img"
        aria-label={t("ev.chartAria", {
          title: model.title,
          series: shown.map((s) => s.label).join(", "),
          x: model.x.label,
          y: model.y.label,
        })}
      >
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={model.rows}
            margin={{ top: 8, right: 18, bottom: 0, left: 0 }}
            barCategoryGap="18%"
          >
            <CartesianGrid vertical={false} stroke="var(--line)" />
            <XAxis
              dataKey="x"
              tickFormatter={(v) => xText(v)}
              interval="preserveStartEnd"
              minTickGap={28}
              tickLine={false}
              axisLine={{ stroke: "var(--line)" }}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              padding={{ left: 4, right: 4 }}
            />
            <YAxis
              domain={[0, model.y.max ?? "auto"]}
              width={yWidth}
              tickFormatter={(v) => format(Number(v), model.y.unit, true)}
              tickLine={false}
              axisLine={false}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              allowDecimals={model.y.unit !== "count"}
            />
            <Tooltip
              cursor={{ fill: "var(--control-hover)" }}
              content={({ active, payload, label }) =>
                active && payload?.length ? (
                  <TooltipBox>
                    <strong>{xText(label, true)}</strong>
                    {shown.map((s) => {
                      const v = payload[0].payload[s.key];
                      return (
                        <span key={s.key} className="tooltip-row">
                          <i
                            style={{ background: s.color }}
                            aria-hidden="true"
                          />
                          {s.label}:{" "}
                          {v == null
                            ? t("notAvailable")
                            : format(Number(v), model.y.unit)}
                        </span>
                      );
                    })}
                  </TooltipBox>
                ) : null
              }
            />
            {model.refY?.map((r) => (
              <ReferenceLine
                key={r.label}
                y={r.value}
                stroke="var(--chart-prior)"
                strokeDasharray="4 4"
                ifOverflow="extendDomain"
              />
            ))}
            {model.refX?.map((r) => (
              <ReferenceLine
                key={r.label}
                x={r.x}
                stroke="var(--chart-prior)"
                strokeDasharray="4 4"
              />
            ))}
            {shown.map((s) =>
              s.mark === "line" ? (
                <Line
                  key={s.key}
                  dataKey={s.key}
                  name={s.label}
                  type="linear"
                  stroke={s.color}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ) : s.mark === "area" ? (
                <Area
                  key={s.key}
                  dataKey={s.key}
                  name={s.label}
                  type="linear"
                  stroke={s.color}
                  fill={s.color}
                  fillOpacity={0.16}
                  strokeWidth={1.75}
                  isAnimationActive={false}
                />
              ) : (
                <Bar
                  key={s.key}
                  dataKey={s.key}
                  name={s.label}
                  fill={s.color}
                  stackId={s.stack ? "stack" : undefined}
                  maxBarSize={32}
                  isAnimationActive={false}
                />
              ),
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="axis-title axis-title-x" aria-hidden="true">
        {model.x.label}
      </p>
      {(model.refY?.length || model.refX?.length) && (
        <div className="ev-legend" aria-hidden="true">
          {[...(model.refY ?? []), ...(model.refX ?? [])].map((r) => (
            <span key={r.label}>
              <i className="dashed" />
              {r.label}
            </span>
          ))}
        </div>
      )}
      <DataTable
        rows={model.rows}
        caption={model.title}
        columns={[
          {
            key: "x",
            label: model.x.label,
            render: (r) => xText(r.x, true),
          },
          ...model.series.map((s) => ({
            key: s.key,
            label: s.label,
            numeric: true,
            render: (r: Record<string, number | string | null>) =>
              r[s.key] == null ? "—" : format(Number(r[s.key]), model.y.unit),
          })),
        ]}
      />
    </Panel>
  );
}
