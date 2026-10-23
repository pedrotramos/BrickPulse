import { useTranslation } from "react-i18next";
import { money, number } from "../../lib/format";
import { SettingChange } from "./AutostopEvidence";
import { num, Panel, records, type Viz } from "./shared";

/** "X_LARGE" → "X-Large", "2X_LARGE" → "2X-Large", the Databricks UI spelling. */
export const sizeLabel = (size: string) =>
  size
    .toLowerCase()
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((w) =>
      /^\d*x$/.test(w) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1),
    )
    .join("-");

export default function RightsizeEvidence({ viz }: { viz: Viz }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const format = (value: number, unit: string) =>
    unit === "%"
      ? `${number(value, lang, 2)}%`
      : `${number(value, lang, unit === "slots" ? 1 : 2)} ${unit}`;
  const headroom = records(viz.headroom).map((h) => {
    const value = num(h.value) ?? 0;
    const limit = num(h.limit) ?? 0;
    const key = String(h.key ?? "");
    return {
      key,
      label: t(`ev.headroom.${key}`, { defaultValue: String(h.label ?? key) }),
      note: t(`ev.headroom.${key}Note`, {
        defaultValue: String(h.meaning ?? ""),
      }),
      value,
      limit,
      unit: String(h.unit ?? ""),
      pct: limit > 0 ? (value / limit) * 100 : null,
    };
  });
  const floor = typeof viz.floor_size === "string" ? viz.floor_size : null;
  const floorUsd = num(viz.floor_savings_usd);
  const p95 = num(viz.p95_runtime_s);
  const peak = num(viz.peak_concurrency);
  return (
    <div className="ev-stack">
      {typeof viz.current_size === "string" &&
        typeof viz.proposed_size === "string" && (
          <SettingChange
            label={t("ev.size")}
            from={sizeLabel(viz.current_size)}
            to={sizeLabel(viz.proposed_size)}
          />
        )}
      {floor && floorUsd != null && (
        <p className="ev-note">
          {t("ev.floorNote", {
            size: sizeLabel(floor),
            amount: money(floorUsd, lang),
          })}
        </p>
      )}
      {headroom.length > 0 && (
        <Panel title={t("ev.headroomTitle")} note={t("ev.headroomNote")}>
          <ul className="gauge-list">
            {headroom.map((h) => (
              <li key={h.key || h.label}>
                <span className="gauge-label">
                  {h.label}
                  <small>
                    {t("ev.limit", { value: format(h.limit, h.unit) })}
                  </small>
                </span>
                <span className="gauge-track" aria-hidden="true">
                  <span
                    className={h.pct != null && h.pct > 100 ? "is-over" : ""}
                    style={{
                      width: `${Math.min(100, Math.max(0, h.pct ?? 0))}%`,
                    }}
                  />
                </span>
                <span className="gauge-value">
                  {format(h.value, h.unit)}
                  {h.pct != null && (
                    <small>
                      {t("ev.ofLimit", {
                        percent: number(h.pct, lang, h.pct < 10 ? 1 : 0),
                      })}
                    </small>
                  )}
                </span>
                {h.note && <p className="gauge-note">{h.note}</p>}
              </li>
            ))}
          </ul>
        </Panel>
      )}
      {(p95 != null || peak != null) && (
        <Panel title={t("ev.watchTitle")}>
          <dl className="inline-facts">
            {p95 != null && (
              <div>
                <dt>{t("ev.p95Runtime")}</dt>
                <dd>{t("ev.seconds", { value: number(p95, lang, 1) })}</dd>
              </div>
            )}
            {peak != null && (
              <div>
                <dt>{t("ev.peakConcurrency")}</dt>
                <dd>{number(peak, lang)}</dd>
              </div>
            )}
          </dl>
        </Panel>
      )}
    </div>
  );
}
