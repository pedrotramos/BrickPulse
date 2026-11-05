export const money = (value: number, locale: string, compact = false) =>
  new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: compact ? 1 : 2,
    ...(compact ? { notation: "compact" as const } : {}),
  }).format(value);
export const number = (value: number, locale: string, digits = 0) =>
  new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(
    value,
  );
export const date = (value: string, locale: string, short = false) => {
  const parsed = new Date(value.length === 10 ? `${value}T12:00:00Z` : value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat(
        locale,
        short
          ? { month: "short", day: "numeric", timeZone: "UTC" }
          : { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" },
      ).format(parsed);
};
/**
 * A moment in time (a refresh finished, a snapshot was taken), shown in the viewer's
 * own time zone with its abbreviation. A value without a zone suffix is UTC, as the
 * API and system tables are. Analytical buckets (billing days, hour-of-day heatmaps,
 * evidence series) stay in UTC through `date`/`hour`: system tables aggregate in UTC,
 * and shifting a bucket would move hours near midnight to another day.
 */
export const instant = (value: string, locale: string) => {
  const iso = value.trim().replace(" ", "T");
  const parsed = new Date(/(Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat(locale, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
      }).format(parsed);
};
export const month = (value: string, locale: string) => {
  const match = /^(\d{4})-(\d{2})/.exec(value);
  return match
    ? new Intl.DateTimeFormat(locale, {
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      }).format(Date.UTC(Number(match[1]), Number(match[2]) - 1, 15))
    : value;
};
export const hour = (value: number, locale: string) =>
  new Intl.DateTimeFormat(locale, { hour: "numeric", timeZone: "UTC" }).format(
    Date.UTC(2000, 0, 1, value),
  );
