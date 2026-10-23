import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Json } from "../../lib/types";
import { date, money, number } from "../../lib/format";
import { type Term, type Unit, useGlossary } from "../../lib/glossary";
import { proseMessage } from "../../lib/prose";
import { InfoTip } from "../InfoTip";
import { Table } from "../ui";
import { isRecord } from "./shared";

/** What a measure means and where it is read from, behind an ⓘ button. */
export function TermHelp({
  term,
  label,
}: {
  term: Pick<Term, "key" | "help" | "source">;
  label: string;
}) {
  const { t } = useTranslation();
  if (!term.help && !term.source) return null;
  return (
    <InfoTip label={label}>
      {term.help && <p>{term.help}</p>}
      {term.source && (
        <p className="info-tip-source">
          <span>{t("sourceColumn")}</span>
          <code>{term.source}</code>
        </p>
      )}
      {term.key && (
        <p className="info-tip-source">
          <span>{t("evidenceField")}</span>
          <code>{term.key}</code>
        </p>
      )}
    </InfoTip>
  );
}

export function TermLabel({ term }: { term: Term }) {
  return (
    <span className="term-label">
      {term.label}
      <TermHelp term={term} label={term.label} />
    </span>
  );
}

const percent = (value: number, lang: string) =>
  new Intl.NumberFormat(lang, {
    style: "percent",
    maximumFractionDigits: value !== 0 && Math.abs(value) < 0.01 ? 2 : 1,
  }).format(value);

/** A value in its unit, in the reader's locale. */
export function useFormat() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  return (value: number, unit: Unit, compact = false): string => {
    const digits = Math.abs(value) < 10 && value % 1 !== 0 ? 2 : 1;
    switch (unit) {
      case "usd":
        return money(value, lang, compact);
      case "usd_month":
        return t("perMonthValue", { value: money(value, lang, compact) });
      case "pct":
        return percent(value / 100, lang);
      case "fraction":
        return percent(value, lang);
      case "seconds":
        return value >= 120
          ? t("ev.minutes", { value: number(value / 60, lang, 1) })
          : t("unit.seconds", { value: number(value, lang, 1) });
      case "minutes":
        return value >= 120
          ? t("ev.hours", { value: number(value / 60, lang, 1) })
          : t("ev.minutes", { value: number(value, lang, 1) });
      case "hours":
        return t("ev.hours", { value: number(value, lang, digits) });
      case "days":
        return t("unit.days", { count: value, value: number(value, lang, 1) });
      case "gb":
        return `${number(value, lang, digits)} GB`;
      case "dbu":
        return `${number(value, lang, digits)} DBU`;
      default:
        return compact
          ? new Intl.NumberFormat(lang, {
              notation: "compact",
              maximumFractionDigits: 1,
            }).format(value)
          : number(value, lang, Math.abs(value) < 1 ? 3 : digits);
    }
  };
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?/;

/** Any evidence value: numbers in their unit, enums and known sentences localized. */
export function MetricValue({
  value,
  term,
}: {
  value: Json | undefined;
  term: Term;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const format = useFormat();
  const lookup = useGlossary();
  if (value == null || value === "") return <>—</>;
  if (typeof value === "boolean") return <>{t(value ? "yes" : "no")}</>;
  if (typeof value === "number") {
    if (term.unit === "bool") return <>{t(value ? "yes" : "no")}</>;
    if (term.unit === "date" || term.unit === "datetime")
      return <>{date(new Date(value * 1000).toISOString(), lang)}</>;
    return <>{format(value, term.unit)}</>;
  }
  if (typeof value === "string") {
    const named = term.value?.(value);
    if (named) return <>{named}</>;
    const prose = proseMessage(value);
    if (prose) return <>{t(prose.key, prose.params)}</>;
    if (ISO_DATE.test(value))
      return (
        <>
          {value.length > 10
            ? `${date(value.replace(" ", "T").replace(/(Z|[+-]\d{2}:?\d{2})?$/, "Z"), lang)} UTC`
            : date(value, lang)}
        </>
      );
    if (term.unit === "code" || !/\s/.test(value)) return <code>{value}</code>;
    // An unrecognised sentence from the rule engine: shown as written.
    return <span lang="en">{value}</span>;
  }
  if (Array.isArray(value)) {
    if (!value.length) return <>—</>;
    if (value.every(isRecord))
      return <RecordList rows={value as Record<string, Json>[]} />;
    return (
      <span className="value-chips">
        {value.slice(0, 50).map((v, i) => (
          <span key={i}>
            <MetricValue
              value={v}
              term={{
                ...term,
                unit: term.unit === "list" ? "code" : term.unit,
              }}
            />
          </span>
        ))}
        {value.length > 50 && (
          <span className="section-note">{t("listTruncated")}</span>
        )}
      </span>
    );
  }
  return (
    <dl className="inline-facts">
      {Object.entries(value).map(([k, v]) => {
        const sub = lookup(k);
        return (
          <div key={k}>
            <dt>{sub.label}</dt>
            <dd>
              <MetricValue value={v} term={sub} />
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/** A list of small records (top jobs, top clients…) as a compact table. */
function RecordList({ rows }: { rows: Record<string, Json>[] }) {
  const lookup = useGlossary();
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return (
    <table className="record-list">
      <thead>
        <tr>
          {keys.map((k) => (
            <th key={k} scope="col">
              {lookup(k).label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.slice(0, 25).map((row, i) => (
          <tr key={i}>
            {keys.map((k) => (
              <td key={k}>
                <MetricValue value={row[k]} term={lookup(k)} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Every measured value behind a finding, by readable name, with its source. */
export function MetricTable({
  entries,
  caption,
}: {
  entries: [string, Json][];
  caption: string;
}): ReactNode {
  const { t } = useTranslation();
  const lookup = useGlossary();
  const rows = entries.map(([key, value]) => ({
    key,
    value,
    term: lookup(key),
  }));
  return (
    <Table
      rows={rows}
      rowKey={(r) => r.key}
      caption={caption}
      columns={[
        {
          key: "metric",
          label: t("metric"),
          render: (r) => <TermLabel term={r.term} />,
        },
        {
          key: "value",
          label: t("value"),
          render: (r) => <MetricValue value={r.value} term={r.term} />,
        },
      ]}
    />
  );
}
