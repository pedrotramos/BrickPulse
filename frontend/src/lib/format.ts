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
