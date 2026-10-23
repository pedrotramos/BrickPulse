import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** How a measured value is displayed. */
export type Unit =
  | "usd"
  | "usd_month"
  | "pct"
  | "fraction"
  | "seconds"
  | "minutes"
  | "hours"
  | "days"
  | "count"
  | "gb"
  | "dbu"
  | "date"
  | "datetime"
  | "bool"
  | "text"
  | "code"
  | "list"
  | "object";

type Localized = Record<string, string>;
type Entry = {
  u: Unit;
  s: string;
  l: Localized;
  h: Localized;
  v?: Record<string, Localized>;
};
type Glossary = Record<string, Entry>;

export type Term = {
  key: string;
  label: string;
  help?: string;
  source?: string;
  unit: Unit;
  /** Localized name of an enum value, when the glossary knows it. */
  value?: (raw: string) => string | undefined;
};

// ~340 entries in three languages: loaded with the first evidence view, not the shell.
let glossary: Glossary | null = null;
let pending: Promise<Glossary> | null = null;
const load = () =>
  (pending ??= import("./glossary.json").then(
    (m) => (glossary = m.default as unknown as Glossary),
  ));

/** Unit implied by a key's suffix, for keys the glossary does not cover. */
export function inferUnit(key: string): Unit {
  if (/(_usd|_cost)$/.test(key)) return "usd";
  if (/_pct$/.test(key)) return "pct";
  if (/(_share|_frac|_fraction|_rate)$/.test(key)) return "fraction";
  if (/_s$/.test(key)) return "seconds";
  if (/(_min|_minutes|_mins)$/.test(key)) return "minutes";
  if (/(_hours|_h)$/.test(key)) return "hours";
  if (/_days$/.test(key)) return "days";
  if (/_gb$/.test(key)) return "gb";
  if (/_dbus?$/.test(key)) return "dbu";
  if (/(_time|_at|_date)$/.test(key)) return "datetime";
  return "count";
}

const humanize = (key: string) => {
  const words = key.replace(/[_.]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
};

export function useGlossary() {
  const { i18n } = useTranslation();
  const [data, setData] = useState(glossary);
  useEffect(() => {
    if (!data) void load().then(setData);
  }, [data]);
  const lang = i18n.language;
  return (key: string): Term => {
    const entry = data?.[key];
    if (!entry) return { key, label: humanize(key), unit: inferUnit(key) };
    return {
      key,
      label: entry.l[lang] ?? entry.l.en,
      help: entry.h[lang] ?? entry.h.en,
      source: entry.s,
      unit: entry.u,
      value: (raw) => entry.v?.[raw]?.[lang] ?? entry.v?.[raw]?.en,
    };
  };
}
