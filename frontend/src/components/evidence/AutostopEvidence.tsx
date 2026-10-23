import { useTranslation } from "react-i18next";
import { ArrowRight } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { hour, money, number } from "../../lib/format";
import { Notice } from "../ui";
import {
  DataTable,
  HourDrill,
  IntensityLegend,
  isRecord,
  Legend,
  level,
  num,
  nums,
  Panel,
  records,
  TooltipBox,
  type Viz,
  WeekHeatmap,
  weekday,
} from "./shared";

export function SettingChange({
  label,
  from,
  to,
}: {
  label: string;
  from: string;
  to: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="setting-change" role="group" aria-label={label}>
      <div>
        <span className="setting-label">{t("ev.today")}</span>
        <span className="setting-value">{from}</span>
      </div>
      <span className="arrow" aria-hidden="true">
        <ArrowRight size={16} />
      </span>
      <div className="is-proposed">
        <span className="setting-label">{t("ev.proposed")}</span>
        <span className="setting-value">{to}</span>
      </div>
    </div>
  );
}

export default function AutostopEvidence({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const current = num(viz.current_minutes);
  const chosen = num(viz.chosen_minutes) ?? 0;
  const days = num(viz.lookback_days) ?? 30;
  const maxStops = num(viz.max_extra_stops_per_day) ?? 0;
  const setting = (v: number | null) =>
    v ? t("ev.minutes", { value: number(v, lang) }) : t("ev.off");

  const tiers = isRecord(viz.tiers) ? viz.tiers : null;
  const enable = tiers && isRecord(tiers.enable) ? tiers.enable : null;
  const tighten = tiers && isRecord(tiers.tighten) ? tiers.tighten : null;
  const shares = [
    enable && {
      key: "enable",
      label: t("ev.tierEnable", {
        from: setting(num(enable.from_minutes)),
        to: setting(num(enable.to_minutes)),
      }),
      amount: num(enable.monthly_savings_usd) ?? 0,
      note: t("ev.tierEnableNote"),
    },
    tighten && {
      key: "tighten",
      label: t("ev.tierTighten", {
        from: setting(num(tighten.from_minutes)),
        to: setting(num(tighten.to_minutes)),
      }),
      amount: num(tighten.monthly_savings_usd) ?? 0,
      note: t("ev.tierTightenNote", {
        restarts: number(num(tighten.extra_stops_per_day) ?? 0, lang, 1),
      }),
    },
  ].filter((x): x is NonNullable<typeof x> => !!x && x.amount > 0);
  const shareTotal = shares.reduce((a, s) => a + s.amount, 0);

  const candidates = records(viz.candidates).map((c) => {
    const minutes = num(c.minutes) ?? 0;
    return {
      label: setting(minutes),
      minutes,
      hours: Math.max(0, (num(c.net_reclaimed_min) ?? 0) / 60),
      perDay: num(c.extra_stops_per_day) ?? 0,
      acceptable: c.acceptable === true,
    };
  });
  const anyOver = candidates.some((c) => !c.acceptable && c.minutes !== chosen);
  const anyWithin = candidates.some(
    (c) => c.acceptable && c.minutes !== chosen,
  );
  // Trust the measured candidate over the flag: a proposal above the restart limit is
  // always called out, even when the payload did not mark it high-churn.
  const chosenOver =
    viz.high_churn === true ||
    candidates.some((c) => c.minutes === chosen && !c.acceptable);
  const gaps = records(viz.gap_buckets).map((g) => ({
    label: String(g.label ?? ""),
    hi: num(g.hi),
    n: num(g.n) ?? 0,
  }));
  const week = nums(viz.queries_by_dow_hour);
  const weekMax = Math.max(0, ...week);
  const weekTotal = week.reduce((a, b) => a + b, 0);
  const weekdayShare = weekTotal
    ? Math.round(
        (week.slice(0, 120).reduce((a, b) => a + b, 0) / weekTotal) * 100,
      )
    : 0;

  return (
    <div className="ev-stack">
      <SettingChange
        label={t("ev.setting")}
        from={setting(current)}
        to={setting(chosen)}
      />
      {!current && <Notice tone="warning">{t("ev.autostopOffNote")}</Notice>}
      {chosenOver && <Notice tone="warning">{t("ev.highChurn")}</Notice>}
      {shares.length > 0 && (
        <Panel title={t("ev.savingSource")}>
          <ul className="share-list">
            {shares.map((s) => {
              const pct = shareTotal ? (s.amount / shareTotal) * 100 : 0;
              return (
                <li key={s.key}>
                  <div>
                    <span className="share-label">{s.label}</span>
                    <strong>
                      {money(s.amount, lang)}
                      {shares.length > 1 &&
                        ` · ${t("ev.share", { percent: number(pct, lang) })}`}
                    </strong>
                  </div>
                  {shares.length > 1 && (
                    <div className="share-track" aria-hidden="true">
                      <span
                        className={s.key === "enable" ? "is-accent" : ""}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  )}
                  <p>{s.note}</p>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}
      {candidates.length > 0 && (
        <Panel
          title={t("ev.candidatesTitle")}
          note={t("ev.candidatesNote", { days })}
        >
          <div
            className="chart"
            role="img"
            aria-label={`${t("ev.candidatesTitle")}: ${candidates
              .map(
                (c) =>
                  `${c.label} ${t("ev.hours", { value: number(c.hours, lang, 1) })}, ${t("ev.perDay", { value: number(c.perDay, lang, 1) })}`,
              )
              .join("; ")}`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={candidates}
                margin={{ top: 18, right: 12, bottom: 0, left: 0 }}
              >
                <CartesianGrid vertical={false} stroke="var(--line)" />
                <XAxis
                  dataKey="label"
                  interval={0}
                  height={40}
                  tickLine={false}
                  axisLine={false}
                  tick={({ x, y, payload }) => {
                    const c = candidates.find((d) => d.label === payload.value);
                    return (
                      <g transform={`translate(${x},${y})`}>
                        <text
                          dy={12}
                          textAnchor="middle"
                          className="recharts-text"
                        >
                          {payload.value}
                        </text>
                        <text
                          dy={27}
                          textAnchor="middle"
                          className="recharts-text"
                        >
                          {t("ev.perDay", {
                            value: number(c?.perDay ?? 0, lang, 1),
                          })}
                        </text>
                      </g>
                    );
                  }}
                />
                <YAxis
                  domain={[0, "auto"]}
                  width={52}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v) =>
                    t("ev.hours", { value: number(Number(v), lang) })
                  }
                />
                <Tooltip
                  cursor={{ fill: "var(--control-hover)" }}
                  content={({ active, payload }) => {
                    const c = active
                      ? candidates.find(
                          (d) => d.label === payload?.[0]?.payload?.label,
                        )
                      : null;
                    return c ? (
                      <TooltipBox>
                        <strong>{c.label}</strong>
                        <span>
                          {t("ev.reclaimedHours")}:{" "}
                          {t("ev.hours", { value: number(c.hours, lang, 1) })}
                        </span>
                        <span>
                          {t("ev.extraRestarts")}: {number(c.perDay, lang, 1)}
                        </span>
                      </TooltipBox>
                    ) : null;
                  }}
                />
                <Bar
                  dataKey="hours"
                  isAnimationActive={false}
                  radius={[2, 2, 0, 0]}
                >
                  {candidates.map((c) => (
                    <Cell
                      key={c.label}
                      fill={
                        c.minutes === chosen
                          ? "var(--accent)"
                          : c.acceptable
                            ? "var(--chart-observed)"
                            : "var(--chart-prior)"
                      }
                    />
                  ))}
                  <LabelList
                    dataKey="hours"
                    position="top"
                    className="recharts-text"
                    formatter={(v: number) => number(Number(v), lang, 1)}
                  />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <Legend
            items={[
              { label: t("ev.proposed"), swatch: "swatch-chosen" },
              ...(anyWithin
                ? [
                    {
                      label: t("ev.withinLimit", {
                        max: number(maxStops, lang, 1),
                      }),
                      swatch: "swatch-observed",
                    },
                  ]
                : []),
              ...(anyOver
                ? [
                    {
                      label: t("ev.overLimit", {
                        max: number(maxStops, lang, 1),
                      }),
                      swatch: "swatch-prior",
                    },
                  ]
                : []),
            ]}
          />
          <DataTable
            rows={candidates}
            caption={t("ev.candidatesTitle")}
            columns={[
              {
                key: "setting",
                label: t("ev.setting"),
                render: (c) => c.label,
              },
              {
                key: "hours",
                label: t("ev.reclaimedHours"),
                numeric: true,
                render: (c) => number(c.hours, lang, 1),
              },
              {
                key: "restarts",
                label: t("ev.extraRestarts"),
                numeric: true,
                render: (c) => number(c.perDay, lang, 2),
              },
            ]}
          />
        </Panel>
      )}
      {gaps.some((g) => g.n > 0) && (
        <Panel
          title={t("ev.gapsTitle")}
          note={t("ev.gapsNote", {
            median: number(num(viz.median_gap_min) ?? 0, lang, 1),
            p90: number(num(viz.p90_gap_min) ?? 0, lang, 1),
            days,
          })}
        >
          <div
            className="chart"
            role="img"
            aria-label={`${t("ev.gapsTitle")}: ${gaps
              .map((g) => `${g.label} ${number(g.n, lang)}`)
              .join("; ")}`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={gaps}
                margin={{ top: 18, right: 12, bottom: 0, left: 0 }}
              >
                <CartesianGrid vertical={false} stroke="var(--line)" />
                <XAxis
                  dataKey="label"
                  interval="preserveStartEnd"
                  minTickGap={6}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  domain={[0, "auto"]}
                  width={52}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v) => number(Number(v), lang)}
                />
                <Tooltip
                  cursor={{ fill: "var(--control-hover)" }}
                  formatter={(v) => [number(Number(v), lang), t("ev.gaps")]}
                />
                <Bar
                  dataKey="n"
                  isAnimationActive={false}
                  radius={[2, 2, 0, 0]}
                >
                  {gaps.map((g) => (
                    <Cell
                      key={g.label}
                      fill={
                        g.hi != null && g.hi <= chosen
                          ? "var(--chart-observed)"
                          : "var(--chart-idle)"
                      }
                    />
                  ))}
                  <LabelList
                    dataKey="n"
                    position="top"
                    className="recharts-text"
                    formatter={(v: number) =>
                      Number(v) ? number(Number(v), lang) : ""
                    }
                  />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <Legend
            items={[
              { label: t("ev.gapsShort"), swatch: "swatch-observed" },
              { label: t("ev.gapsLong"), swatch: "swatch-idle" },
            ]}
          />
          <DataTable
            rows={gaps}
            caption={t("ev.gapsTitle")}
            columns={[
              { key: "len", label: t("ev.gapLength"), render: (g) => g.label },
              {
                key: "n",
                label: t("ev.gaps"),
                numeric: true,
                render: (g) => number(g.n, lang),
              },
            ]}
          />
        </Panel>
      )}
      {week.length === 168 && (
        <Panel
          title={t("ev.weekTitle")}
          note={t("ev.weekNote", {
            days,
            weekday: weekdayShare,
            weekend: weekTotal ? 100 - weekdayShare : 0,
          })}
        >
          <WeekHeatmap
            label={t("ev.weekTitle")}
            rows={Array.from({ length: 7 }, (_, d) => ({
              label: weekday(d, lang),
              cells: week.slice(d * 24, d * 24 + 24).map((v, h) => ({
                className: `heat-level-${level(v, weekMax)}`,
                title: `${weekday(d, lang)} · ${hour(h, lang)} UTC · ${t("ev.queries")}: ${number(v, lang)}`,
              })),
            }))}
          />
          <IntensityLegend />
        </Panel>
      )}
      {nums(viz.queries_by_hour).length === 24 && (
        <Panel
          title={t("ev.dayTitle")}
          note={t(
            nums(viz.queries_by_hour_minute).length === 1440
              ? "ev.dayNote"
              : "ev.dayNoteStatic",
          )}
        >
          <HourDrill
            title={t("ev.dayTitle")}
            byHour={nums(viz.queries_by_hour)}
            byMinute={nums(viz.queries_by_hour_minute)}
            unit={t("ev.queries")}
          />
        </Panel>
      )}
    </div>
  );
}
