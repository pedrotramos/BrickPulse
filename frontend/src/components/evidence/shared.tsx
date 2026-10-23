import {
  type CSSProperties,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import type { Json } from "../../lib/types";
import { date, hour, number } from "../../lib/format";
import { Table } from "../ui";

export type Viz = Record<string, Json>;
export function isRecord(
  value: Json | undefined,
): value is Record<string, Json> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export const num = (value: Json | undefined) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
export const nums = (value: Json | undefined) =>
  Array.isArray(value) && value.every((x) => typeof x === "number")
    ? (value as number[])
    : [];
export const records = (value: Json | undefined) =>
  Array.isArray(value) ? value.filter(isRecord) : [];
const sum = (values: number[]) => values.reduce((a, b) => a + b, 0);

/** Weekday name for dow 0 = Monday … 6 = Sunday (the backend's convention). */
export const weekday = (dow: number, locale: string) =>
  new Intl.DateTimeFormat(locale, { weekday: "short", timeZone: "UTC" }).format(
    Date.UTC(2024, 0, 1 + dow),
  );
const ENGLISH_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const dayLabel = (label: string, locale: string) =>
  ENGLISH_DAYS.includes(label)
    ? weekday(ENGLISH_DAYS.indexOf(label), locale)
    : /^\d{4}-\d{2}-\d{2}/.test(label)
      ? date(label.slice(0, 10), locale, true)
      : label;

/** Minutes as "45 min" or, from two hours up, "3,5 h". */
export function useDuration() {
  const { t, i18n } = useTranslation();
  return (minutes: number) =>
    minutes >= 120
      ? t("ev.hours", { value: number(minutes / 60, i18n.language, 1) })
      : t("ev.minutes", { value: number(minutes, i18n.language) });
}

export function Panel({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="ev-panel">
      <h4>{title}</h4>
      {note && <p className="ev-note">{note}</p>}
      {children}
    </section>
  );
}
export function Legend({
  items,
}: {
  items: { label: string; swatch: string }[];
}) {
  return (
    <div className="ev-legend">
      {items.map((item) => (
        <span key={item.label}>
          <i className={item.swatch} aria-hidden="true" />
          {item.label}
        </span>
      ))}
    </div>
  );
}
export function DataTable<T>({
  rows,
  columns,
  caption,
}: {
  rows: T[];
  columns: {
    key: string;
    label: string;
    render: (row: T) => ReactNode;
    numeric?: boolean;
  }[];
  caption: string;
}) {
  const { t } = useTranslation();
  return (
    <details className="data-disclosure">
      <summary>{t("showData")}</summary>
      <Table
        rows={rows.map((row, i) => ({ row, id: String(i) }))}
        rowKey={(r) => r.id}
        caption={caption}
        columns={columns.map((c) => ({
          ...c,
          render: (r: { row: T; id: string }) => c.render(r.row),
        }))}
      />
    </details>
  );
}
export function TooltipBox({ children }: { children: ReactNode }) {
  return <div className="chart-tooltip">{children}</div>;
}

/** Five intensity steps; any non-zero value is at least step 1. */
export const level = (value: number, max: number) =>
  value <= 0 || max <= 0
    ? 0
    : Math.min(4, Math.max(1, Math.ceil((value / max) * 4)));

export function WeekHeatmap({
  rows,
  label,
}: {
  rows: { label: string; cells: { className: string; title: string }[] }[];
  label: string;
}) {
  const { i18n } = useTranslation();
  return (
    <div className="heatmap" role="img" aria-label={label}>
      <div className="heatmap-row heatmap-hours" aria-hidden="true">
        <span />
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h}>{h % 6 === 0 ? hour(h, i18n.language) : ""}</span>
        ))}
      </div>
      {rows.map((row, i) => (
        <div className="heatmap-row" key={i}>
          <span>{row.label}</span>
          {row.cells.map((cell, h) => (
            <span
              key={h}
              title={cell.title}
              className={`heat-cell ${cell.className}`}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
export function IntensityLegend() {
  const { t } = useTranslation();
  return (
    <div className="ev-legend" aria-hidden="true">
      <span>{t("ev.none")}</span>
      {[0, 1, 2, 3, 4].map((l) => (
        <i key={l} className={`heat-cell heat-level-${l}`} />
      ))}
      <span>{t("ev.more")}</span>
    </div>
  );
}

/** Hour-of-day bars; with a 1440-minute profile each hour drills into its minutes. */
export function HourDrill({
  title,
  byHour,
  byMinute,
  unit,
}: {
  title: string;
  byHour: number[];
  byMinute: number[];
  unit: string;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const [selected, setSelected] = useState<number | null>(null);
  const last = useRef<number | null>(null);
  const back = useRef<HTMLButtonElement>(null);
  const bars = useRef<HTMLDivElement>(null);
  const drillable = byMinute.length === 1440;
  useEffect(() => {
    if (selected != null) back.current?.focus();
    else if (last.current != null)
      bars.current
        ?.querySelector<HTMLButtonElement>(`[data-hour="${last.current}"]`)
        ?.focus();
  }, [selected]);
  if (byHour.length !== 24) return null;
  const minutes = selected != null && drillable;
  const values = minutes
    ? byMinute.slice(selected * 60, selected * 60 + 60)
    : byHour;
  const max = Math.max(0, ...values);
  const peakAt = values.indexOf(max);
  const label = (i: number) =>
    minutes
      ? `${hour(selected, lang)}:${String(i).padStart(2, "0")}`
      : hour(i, lang);
  const height = (v: number) =>
    `${max > 0 ? Math.max(v > 0 ? 2 : 0, (v / max) * 100) : 0}%`;
  const summary = t("ev.chartSummary", {
    title: minutes
      ? t("ev.minuteTitle", { hour: hour(selected, lang) })
      : title,
    total: number(sum(values), lang),
    peak: number(max, lang),
    at: label(peakAt),
  });
  const interactive = drillable && !minutes;
  return (
    <div className="hour-drill">
      {minutes && (
        <div className="drill-head">
          <strong>{t("ev.minuteTitle", { hour: hour(selected, lang) })}</strong>
          <button
            ref={back}
            type="button"
            className="subtle"
            onClick={() => setSelected(null)}
          >
            {t("ev.backToHours")}
          </button>
        </div>
      )}
      <div
        className={`hour-scroll${interactive ? " is-interactive" : ""}`}
        style={{ "--bars": values.length } as CSSProperties}
      >
        <div
          ref={bars}
          className="hour-bars"
          {...(interactive
            ? { role: "group", "aria-label": title }
            : { role: "img", "aria-label": summary })}
        >
          {values.map((v, i) =>
            interactive ? (
              <button
                key={i}
                type="button"
                data-hour={i}
                title={`${label(i)} · ${number(v, lang)}`}
                aria-label={t("ev.showMinutes", {
                  hour: label(i),
                  value: number(v, lang),
                })}
                onClick={() => {
                  last.current = i;
                  setSelected(i);
                }}
              >
                <span style={{ height: height(v) }} />
              </button>
            ) : (
              <span key={i} title={`${label(i)} · ${number(v, lang)}`}>
                <span style={{ height: height(v) }} />
              </span>
            ),
          )}
        </div>
        <div className="hour-axis" aria-hidden="true">
          {values.map((_, i) => (
            <span key={i}>
              {minutes
                ? i % 10 === 0
                  ? `:${String(i).padStart(2, "0")}`
                  : ""
                : i % 6 === 0
                  ? label(i)
                  : ""}
            </span>
          ))}
        </div>
      </div>
      <p className="ev-note">
        {t("ev.peak", { value: `${number(max, lang)} · ${label(peakAt)} UTC` })}
      </p>
      <DataTable
        rows={values.map((value, i) => ({ value, i }))}
        caption={title}
        columns={[
          {
            key: "at",
            label: minutes ? t("minute") : t("hour"),
            render: (r) => `${label(r.i)} UTC`,
          },
          {
            key: "value",
            label: unit,
            numeric: true,
            render: (r) => number(r.value, lang),
          },
        ]}
      />
    </div>
  );
}
