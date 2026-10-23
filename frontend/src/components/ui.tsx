import { type ReactNode, useEffect } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Database,
  RefreshCw,
} from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";
import { ApiError, isTransient, useScope, withScope } from "../lib/api";
import { money, number } from "../lib/format";

/** An in-app link that keeps the selected workspace; only the picker changes it. */
export function ScopedLink({
  to,
  children,
  className,
  ...rest
}: {
  to: string;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  const scope = useScope();
  return (
    <Link to={withScope(to, scope)} className={className} {...rest}>
      {children}
    </Link>
  );
}
export const PAGE_EVENT = "brickpulse:page";
export function Heading({
  title,
  description,
  aside,
}: {
  title: string;
  description?: string;
  aside?: ReactNode;
}) {
  useEffect(() => {
    document.title = `${title} · BrickPulse`;
    window.dispatchEvent(new CustomEvent(PAGE_EVENT, { detail: title }));
  }, [title]);
  return (
    <header className="page-heading">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {aside}
    </header>
  );
}
export function State<T>({
  query,
  children,
}: {
  query: UseQueryResult<T, ApiError>;
  children: (data: T) => ReactNode;
}) {
  const { t } = useTranslation();
  if (query.isPending)
    return (
      <div className="skeleton" role="status" aria-label={t("loading")}>
        <span />
        <span />
        <span />
        <span />
      </div>
    );
  if (query.isError && query.data === undefined)
    return (
      <ErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    );
  return (
    <>
      {query.isError && (
        <Notice tone="warning">
          <span role="alert">{t(errorCode(query.error))}</span>{" "}
          <button
            className="subtle inline-retry"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw size={13} />
            {t("retry")}
          </button>
        </Notice>
      )}
      {children(query.data as T)}
    </>
  );
}
const errorCode = (error: unknown) =>
  error instanceof ApiError ? error.code : "loadError";
function ErrorState({
  error,
  onRetry,
  retrying,
}: {
  error: unknown;
  onRetry: () => void;
  retrying: boolean;
}) {
  const { t } = useTranslation();
  const code = errorCode(error);
  return (
    <div className="empty error" role="alert">
      <AlertCircle size={26} />
      <h2>{t(code)}</h2>
      <p>{t(`${code}Hint`, { defaultValue: t("errorHint") })}</p>
      {code === "unauthorized" ? (
        <button onClick={() => window.location.reload()}>
          <RefreshCw size={15} />
          {t("reload")}
        </button>
      ) : code === "forbidden" ? (
        <ScopedLink to="/data-health">
          {t("health")} <ArrowRight size={14} aria-hidden="true" />
        </ScopedLink>
      ) : error instanceof ApiError &&
        !isTransient(error) &&
        code !== "loadError" ? (
        <ScopedLink to="/">
          {t("overview")} <ArrowRight size={14} aria-hidden="true" />
        </ScopedLink>
      ) : (
        <button onClick={onRetry} disabled={retrying}>
          <RefreshCw size={15} />
          {t("retry")}
        </button>
      )}
    </div>
  );
}
export function metricStatus(query: UseQueryResult<unknown, ApiError>) {
  if (query.isPending) return "pending" as const;
  if (query.isError && query.data === undefined) return "error" as const;
  return undefined;
}
export function Empty({
  filtered = false,
  onReset,
}: {
  filtered?: boolean;
  onReset?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="empty">
      <Database size={26} />
      <h2>{t(filtered ? "noMatched" : "noData")}</h2>
      <p>{t(filtered ? "resetHint" : "emptyHint")}</p>
      {onReset ? (
        <button onClick={onReset}>{t("clearFilters")}</button>
      ) : (
        <ScopedLink to="/data-health">
          {t("health")} <ArrowRight size={14} aria-hidden="true" />
        </ScopedLink>
      )}
    </div>
  );
}
export function Notice({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "warning";
}) {
  return <div className={`notice ${tone}`}>{children}</div>;
}
export function Badge({ value, label }: { value: string; label?: string }) {
  const { t } = useTranslation();
  return (
    <span className={`badge badge-${value.toLowerCase()}`}>
      {label ?? t(value, { defaultValue: value })}
    </span>
  );
}
export function Metrics({
  items,
}: {
  items: {
    label: string;
    value?: number | null;
    note: string;
    kind?: "money" | "number";
    accent?: boolean;
    status?: "pending" | "error";
    onRetry?: () => void;
  }[];
}) {
  const { t, i18n } = useTranslation();
  return (
    <dl className="metrics">
      {items.map((item) => (
        <div
          className={item.accent ? "metric accent" : "metric"}
          key={item.label}
        >
          <dt>{item.label}</dt>
          <dd className="metric-value">
            {item.status === "pending" ? (
              <span className="metric-pending">
                <span className="sr-only">{t("loadingValue")}</span>
              </span>
            ) : item.status === "error" ? (
              <span className="metric-error">
                {t("valueError")}
                {item.onRetry && (
                  <button
                    className="subtle inline-retry"
                    onClick={item.onRetry}
                  >
                    <RefreshCw size={13} />
                    {t("retry")}
                  </button>
                )}
              </span>
            ) : item.value == null ? (
              <span className="unavailable">{t("notAvailable")}</span>
            ) : item.kind === "number" ? (
              number(item.value, i18n.language)
            ) : (
              money(item.value, i18n.language)
            )}
          </dd>
          {item.note && <dd className="metric-note">{item.note}</dd>}
        </div>
      ))}
    </dl>
  );
}
export function Table<T>({
  rows,
  columns,
  rowKey,
  caption,
}: {
  rows: T[];
  columns: {
    key: string;
    label: string;
    render: (row: T) => ReactNode;
    numeric?: boolean;
  }[];
  rowKey: (row: T) => string;
  caption: string;
}) {
  return (
    <div
      className="table-scroll"
      tabIndex={0}
      role="region"
      aria-label={caption}
    >
      <table>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                scope="col"
                key={c.key}
                className={c.numeric ? "numeric" : ""}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.key} className={c.numeric ? "numeric" : ""}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function Back() {
  const { t } = useTranslation();
  return (
    <ScopedLink to="/recommendations" className="back">
      <ArrowLeft size={15} />
      {t("opportunities")}
    </ScopedLink>
  );
}
