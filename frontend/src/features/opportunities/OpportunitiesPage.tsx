import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useApi } from "../../lib/api";
import type { Finding, Page, Rule } from "../../lib/types";
import { money } from "../../lib/format";
import {
  Badge,
  Empty,
  Heading,
  Metrics,
  ScopedLink,
  State,
  Table,
} from "../../components/ui";
import ResourceTable from "../../components/ResourceTable";
export default function OpportunitiesPage({
  actions = false,
}: {
  actions?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const urlSearch = params.get("q") ?? "";
  const [search, setSearch] = useState(urlSearch);
  // Back/forward and "clear filters" change the URL; keep the field in step with it.
  useEffect(() => setSearch(urlSearch), [urlSearch]);
  const rules = useApi<Rule[]>("/rules", { scoped: false });
  const change = (key: string, value: string) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        value ? next.set(key, value) : next.delete(key);
        if (key !== "page") next.delete("page");
        return next;
      },
      { replace: true },
    );
  useEffect(() => {
    const timeout = setTimeout(() => {
      if (search !== (params.get("q") ?? "")) change("q", search);
    }, 300);
    return () => clearTimeout(timeout);
  }, [search]);
  const queryParams = new URLSearchParams(params);
  queryParams.set("page_size", params.get("page_size") ?? "25");
  const query = useApi<Page>(`/recommendation-groups/page?${queryParams}`, {
    enabled: !actions,
  });
  const nba = useApi<Finding[]>("/next-best-actions?limit=50", {
    enabled: actions,
  });
  const requested = Number.parseInt(params.get("page") ?? "1", 10);
  const page = Number.isFinite(requested) && requested > 0 ? requested : 1;
  const pages = Math.max(1, query.data?.pagination.pages ?? 1);
  useEffect(() => {
    // A shared or stale link can point past the last page once results shrink.
    if (query.data && page > pages)
      change("page", pages > 1 ? String(pages) : "");
  }, [query.data, page, pages]);
  const reset = () => {
    setSearch("");
    setParams((current) => {
      const next = new URLSearchParams();
      if (current.get("workspace_id"))
        next.set("workspace_id", current.get("workspace_id")!);
      return next;
    });
  };
  if (actions)
    return (
      <>
        <Heading title={t("actions")} description={t("topActionsNote")} />
        <State query={nba}>
          {(items) =>
            items.length ? (
              <section className="panel resource-panel">
                <Table
                  rows={items}
                  rowKey={(r) => r.recommendation_id}
                  caption={t("actions")}
                  columns={[
                    {
                      key: "resource",
                      label: t("resource"),
                      render: (r) => (
                        <>
                          <ScopedLink
                            to={`/recommendations/${r.finding_key}`}
                            className="resource-link"
                          >
                            {r.resource_name}
                          </ScopedLink>
                          <span className="cell-sub">
                            {r.workspace_name ?? r.workspace_id}
                          </span>
                        </>
                      ),
                    },
                    {
                      key: "action",
                      label: t("action"),
                      render: (r) => r.rule_title,
                    },
                    {
                      key: "priority",
                      label: t("priority"),
                      render: (r) => <Badge value={r.priority} />,
                    },
                    {
                      key: "effort",
                      label: t("effort"),
                      render: (r) => t(r.effort_band),
                    },
                    {
                      key: "value",
                      label: t("monthlyOpportunity"),
                      numeric: true,
                      render: (r) =>
                        money(r.monthly_savings_usd, i18n.language),
                    },
                  ]}
                />
              </section>
            ) : (
              <Empty />
            )
          }
        </State>
      </>
    );
  const activeFilters = [
    "q",
    "resource_type",
    "effort",
    "status",
    "priority",
    "rule_ids",
    "view",
  ].some((k) => params.has(k));
  return (
    <>
      <Heading title={t("opportunities")} description={t("topActionsNote")} />
      {query.data && (
        <Metrics
          items={[
            {
              label: t("resources"),
              value: query.data.summary.resources,
              kind: "number",
              note: "",
            },
            {
              label: t("potential"),
              value: query.data.summary.potential_usd,
              note: t("monthly"),
              accent: true,
            },
            {
              label: t("quick_wins"),
              value: query.data.summary.quick_wins.value_usd,
              note: t("quickWinsNote", {
                count: query.data.summary.quick_wins.count,
              }),
            },
          ]}
        />
      )}
      <section className="panel resource-panel">
        <div
          className="preset-tabs"
          role="group"
          aria-label={t("opportunities")}
        >
          {["all", "quick_wins", "booked", "high_priority", "advisory"].map(
            (view) => (
              <button
                key={view}
                className={
                  (params.get("view") ?? "all") === view ? "selected" : ""
                }
                onClick={() => change("view", view === "all" ? "" : view)}
                aria-pressed={(params.get("view") ?? "all") === view}
              >
                {t(view === "advisory" ? "advisoryView" : view)}
                {query.data && <span>{query.data.summary.views[view]}</span>}
              </button>
            ),
          )}
        </div>
        <form className="filters" onSubmit={(e) => e.preventDefault()}>
          <label className="search">
            <Search size={16} />
            <input
              type="search"
              aria-label={t("search")}
              placeholder={t("search")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <select
            aria-label={t("resource")}
            value={params.get("resource_type") ?? ""}
            onChange={(e) => change("resource_type", e.target.value)}
          >
            <option value="">{t("allTypes")}</option>
            {[
              "cluster",
              "warehouse",
              "job",
              "serving_endpoint",
              "pipeline",
              "instance_pool",
              "table",
              "dashboard",
              "alert",
              "app",
              "workspace",
            ].map((v) => (
              <option key={v} value={v}>
                {t(`resourceType.${v}`)}
              </option>
            ))}
          </select>
          <select
            aria-label={t("effort")}
            value={params.get("effort") ?? ""}
            onChange={(e) => change("effort", e.target.value)}
          >
            <option value="">{t("allEfforts")}</option>
            {["trivial", "low", "medium", "high"].map((v) => (
              <option key={v} value={v}>
                {t(v)}
              </option>
            ))}
          </select>
          <select
            aria-label={t("status")}
            value={params.get("status") ?? ""}
            onChange={(e) => change("status", e.target.value)}
          >
            <option value="">{t("allStatuses")}</option>
            {["bookable", "estimated", "advisory"].map((v) => (
              <option key={v} value={v}>
                {t(v)}
              </option>
            ))}
          </select>
          <select
            aria-label={t("priority")}
            value={params.get("priority") ?? ""}
            onChange={(e) => change("priority", e.target.value)}
          >
            <option value="">{t("allPriorities")}</option>
            {["HIGH", "MEDIUM", "LOW"].map((v) => (
              <option key={v} value={v}>
                {t(v)}
              </option>
            ))}
          </select>
          <select
            className="rule-filter"
            aria-label={t("rules")}
            value={params.get("rule_ids") ?? ""}
            onChange={(e) => change("rule_ids", e.target.value)}
          >
            <option value="">{t("rules")}</option>
            {[...new Set(rules.data?.map((r) => r.resource_type))].map(
              (type) => (
                <optgroup
                  key={type}
                  label={t(`resourceType.${type}`, { defaultValue: type })}
                >
                  {rules.data
                    ?.filter((r) => r.resource_type === type)
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.title}
                      </option>
                    ))}
                </optgroup>
              ),
            )}
          </select>
          {activeFilters && (
            <button className="subtle" onClick={reset} type="button">
              <X size={14} />
              {t("clearFilters")}
            </button>
          )}
        </form>
        <div className="table-toolbar">
          <span aria-live="polite">
            {query.data
              ? t("results", { count: query.data.total })
              : t("loading")}
          </span>
          <label>
            {t("sort")}{" "}
            <select
              value={params.get("sort") ?? "nba"}
              onChange={(e) => change("sort", e.target.value)}
            >
              {["nba", "savings", "effort"].map((v) => (
                <option value={v} key={v}>
                  {t(v)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <State query={query}>
          {(data) => (
            <>
              {data.items.length ? (
                <ResourceTable items={data.items} />
              ) : (
                <Empty
                  filtered={activeFilters}
                  onReset={activeFilters ? reset : undefined}
                />
              )}
              <div className="pagination">
                <label>
                  {t("perPage")}{" "}
                  <select
                    value={params.get("page_size") ?? "25"}
                    onChange={(e) => change("page_size", e.target.value)}
                  >
                    {[10, 25, 50, 100].map((n) => (
                      <option key={n}>{n}</option>
                    ))}
                  </select>
                </label>
                <span>{t("page", { page: Math.min(page, pages), pages })}</span>
                <div>
                  <button
                    aria-label={t("previous")}
                    disabled={page <= 1}
                    onClick={() => change("page", String(page - 1))}
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <button
                    aria-label={t("next")}
                    disabled={page >= pages}
                    onClick={() => change("page", String(page + 1))}
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            </>
          )}
        </State>
      </section>
    </>
  );
}
