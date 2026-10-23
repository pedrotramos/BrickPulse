import { useTranslation } from "react-i18next";
import {
  ResponsiveContainer,
  CartesianGrid,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
} from "recharts";
import type { Day, Json } from "../lib/types";
import { money, date, number } from "../lib/format";
import { Empty, Table } from "./ui";
import AutostopEvidence from "./evidence/AutostopEvidence";
import SpecEvidence, { SPEC_KINDS } from "./evidence/GenericEvidence";
import { MetricTable } from "./evidence/Metric";
import RightsizeEvidence from "./evidence/RightsizeEvidence";
import {
  ClusterDailyEvidence,
  ClusterWeekEvidence,
  StateHeatmapEvidence,
} from "./evidence/ClusterEvidence";
import { isRecord, Legend, nums, records, type Viz } from "./evidence/shared";

export { isRecord };

const AVERAGE_DAYS = 7;
export function SpendChart({ data, title }: { data: Day[]; title?: string }) {
  const { t, i18n } = useTranslation();
  if (!data.length) return <Empty />;
  // The current UTC day is still accruing: it is drawn dashed and kept out of the
  // average, so a half-billed day never reads as a drop in spend.
  const today = new Date().toISOString().slice(0, 10);
  const base = data.map((day) => ({
    date: day.usage_date,
    // A day without a value is a gap in the source, not zero spend.
    value: day.spend_usd ?? day.cost_usd ?? null,
    partial: day.usage_date.slice(0, 10) >= today,
  }));
  const complete = base.filter((r) => !r.partial);
  const rows = base.map((row, i) => {
    const window = base.slice(Math.max(0, i - AVERAGE_DAYS + 1), i + 1);
    const full =
      !row.partial &&
      window.length === AVERAGE_DAYS &&
      window.every((r) => !r.partial && r.value != null);
    const lastComplete = complete[complete.length - 1];
    return {
      ...row,
      settled: row.partial ? null : row.value,
      // The dashed segment starts at the last complete day so the line stays joined.
      accruing:
        row.partial || (row === lastComplete && base.some((r) => r.partial))
          ? row.value
          : null,
      average: full
        ? window.reduce((s, r) => s + (r.value ?? 0), 0) / AVERAGE_DAYS
        : null,
    };
  });
  const hasAverage = rows.some((r) => r.average != null);
  const hasPartial = base.some((r) => r.partial);
  return (
    <div className="chart-block">
      <div className="chart" role="img" aria-label={title ?? t("dailySpend")}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={rows}
            margin={{ top: 10, right: 15, bottom: 0, left: 0 }}
          >
            <CartesianGrid vertical={false} stroke="var(--line)" />
            <XAxis
              dataKey="date"
              tickFormatter={(d) => date(String(d), i18n.language, true)}
              tickLine={false}
              axisLine={false}
              minTickGap={35}
            />
            <YAxis
              domain={[0, "auto"]}
              tickFormatter={(v) => number(Number(v), i18n.language)}
              width={58}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              labelFormatter={(v, payload) =>
                `${date(String(v), i18n.language, true)}${payload?.[0]?.payload?.partial ? ` · ${t("ev.partialDay")}` : ""}`
              }
              formatter={(v, name) => [
                v == null ? t("notAvailable") : money(Number(v), i18n.language),
                name === "average" ? t("sevenDayAverage") : "USD",
              ]}
              filterNull
            />
            <Line
              type="linear"
              dataKey="settled"
              name="value"
              stroke="var(--chart-observed)"
              strokeWidth={2.25}
              dot={false}
              isAnimationActive={false}
            />
            {hasPartial && (
              <Line
                type="linear"
                dataKey="accruing"
                name="value"
                stroke="var(--chart-observed)"
                strokeWidth={2.25}
                strokeDasharray="4 4"
                dot={false}
                legendType="none"
                tooltipType="none"
                isAnimationActive={false}
              />
            )}
            {hasAverage && (
              <Line
                type="linear"
                dataKey="average"
                name="average"
                stroke="var(--chart-prior)"
                strokeWidth={1.75}
                dot={false}
                isAnimationActive={false}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {(hasAverage || hasPartial) && (
        <Legend
          items={[
            { label: t("dailySpend"), swatch: "line swatch-observed" },
            ...(hasAverage
              ? [{ label: t("sevenDayAverage"), swatch: "line swatch-prior" }]
              : []),
          ]}
        />
      )}
      {hasPartial && <p className="ev-note">{t("partialDayNote")}</p>}
      <details className="data-disclosure">
        <summary>{t("showData")}</summary>
        <Table
          rows={rows}
          rowKey={(r) => r.date}
          caption={t("dailySpend")}
          columns={[
            {
              key: "date",
              label: t("day"),
              render: (r) =>
                `${date(r.date, i18n.language, true)}${r.partial ? ` · ${t("ev.partialDay")}` : ""}`,
            },
            {
              key: "value",
              label: "USD",
              numeric: true,
              render: (r) =>
                r.value == null
                  ? t("notAvailable")
                  : money(r.value, i18n.language),
            },
            ...(hasAverage
              ? [
                  {
                    key: "average",
                    label: t("sevenDayAverage"),
                    numeric: true,
                    render: (r: (typeof rows)[number]) =>
                      r.average == null ? "—" : money(r.average, i18n.language),
                  },
                ]
              : []),
          ]}
        />
      </details>
    </div>
  );
}

export function NumericEvidence({ value }: { value: Json | undefined }) {
  const { t, i18n } = useTranslation();
  if (typeof value === "number") return <>{number(value, i18n.language, 4)}</>;
  if (Array.isArray(value))
    return (
      <>
        {value.slice(0, 100).map((v, i) => (
          <span key={i} className="array-value">
            <NumericEvidence value={v} />
            {i < value.length - 1 ? ", " : ""}
          </span>
        ))}
        {value.length > 100 && (
          <span className="section-note">{t("listTruncated")}</span>
        )}
      </>
    );
  if (isRecord(value))
    return (
      <dl className="inline-facts">
        {Object.entries(value).map(([k, v]) => (
          <div key={k}>
            <dt>
              <code>{k}</code>
            </dt>
            <dd>
              <NumericEvidence value={v} />
            </dd>
          </div>
        ))}
      </dl>
    );
  return <>{value == null ? "—" : String(value)}</>;
}

/** One dedicated view per evidence kind whose decision has a shape of its own. */
const DEDICATED: Record<
  string,
  { view: (props: { viz: Viz }) => JSX.Element; ready: (viz: Viz) => boolean }
> = {
  autostop_tradeoff: {
    view: AutostopEvidence,
    ready: (viz) =>
      records(viz.candidates).length > 0 || records(viz.gap_buckets).length > 0,
  },
  wh_rightsize: {
    view: RightsizeEvidence,
    ready: (viz) => records(viz.headroom).length > 0,
  },
  cluster_daily: {
    view: ClusterDailyEvidence,
    ready: (viz) => records(viz.days).length > 0,
  },
  cluster_week_heatmap: {
    view: ClusterWeekEvidence,
    ready: (viz) => nums(viz.active_by_dow_hour).length === 168,
  },
  hour_heatmap: {
    view: StateHeatmapEvidence,
    ready: (viz) => Array.isArray(viz.grid) && viz.grid.some(Array.isArray),
  },
};
export function EvidenceChart({ viz }: { viz: Record<string, Json> }) {
  const { t } = useTranslation();
  const kind = String(viz.kind);
  const dedicated = DEDICATED[kind];
  if (dedicated?.ready(viz)) {
    const View = dedicated.view;
    return <View viz={viz} />;
  }
  if (SPEC_KINDS.has(kind)) return <SpecEvidence viz={viz} />;
  // An unknown kind: its scalar measures still read better as a table than as nothing.
  const scalars = Object.entries(viz).filter(
    ([k, v]) => k !== "kind" && !Array.isArray(v) && !isRecord(v),
  );
  return (
    <>
      <p className="muted">{t("chartUnavailable")}</p>
      {scalars.length > 0 && (
        <MetricTable entries={scalars} caption={t("evidence")} />
      )}
    </>
  );
}
