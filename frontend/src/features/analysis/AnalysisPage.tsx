import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useApi, useScope } from "../../lib/api";
import type {
  Config,
  Day,
  Finding,
  Health,
  Json,
  Rule,
  Spend,
  Workspace,
} from "../../lib/types";
import { date, money, month, number } from "../../lib/format";
import {
  Badge,
  Empty,
  Heading,
  Metrics,
  Notice,
  ScopedLink,
  State,
  Table,
} from "../../components/ui";
import { isRecord, NumericEvidence, SpendChart } from "../../components/Charts";

function Slice<T>({
  rows,
  children,
}: {
  rows: T[];
  children: (rows: T[]) => ReactNode;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(rows.length / 25));
  const current = Math.min(page, pages);
  return (
    <>
      {children(rows.slice((current - 1) * 25, current * 25))}
      {pages > 1 && (
        <div className="pagination">
          <span>{t("page", { page: current, pages })}</span>
          <button disabled={current === 1} onClick={() => setPage(current - 1)}>
            {t("previous")}
          </button>
          <button
            disabled={current === pages}
            onClick={() => setPage(current + 1)}
          >
            {t("next")}
          </button>
        </div>
      )}
    </>
  );
}
function Findings({ labs }: { labs: boolean }) {
  const { t, i18n } = useTranslation();
  const query = useApi<{
    items: Finding[];
    total_addressable_monthly_usd: number;
    total_unattributable_monthly_usd: number;
    recommendation_count: number;
    workspace_count: number;
  }>(labs ? "/experimental" : "/tagging");
  return (
    <>
      <Heading
        title={t(labs ? "labs" : "tagging")}
        description={t(labs ? "labsNote" : "taggingNote")}
      />
      <State query={query}>
        {(d) => (
          <>
            <Metrics
              items={[
                {
                  label: t("monthlySpend"),
                  value: labs
                    ? d.total_addressable_monthly_usd
                    : d.total_unattributable_monthly_usd,
                  note: t("monthly"),
                },
                {
                  label: t("recommendations"),
                  value: d.recommendation_count,
                  kind: "number",
                  note: "",
                },
                {
                  label: t("workspaces"),
                  value: d.workspace_count,
                  kind: "number",
                  note: "",
                },
              ]}
            />
            {d.items.length ? (
              <section className="panel resource-panel">
                <Slice rows={d.items}>
                  {(items) => (
                    <Table
                      rows={items}
                      rowKey={(r) => r.finding_key}
                      caption={t(labs ? "labs" : "tagging")}
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
                          key: "title",
                          label: t("recommendation"),
                          render: (r) => r.rule_title,
                        },
                        {
                          key: "spend",
                          label: t("monthlySpend"),
                          numeric: true,
                          render: (r) =>
                            money(r.monthly_waste_usd, i18n.language),
                        },
                      ]}
                    />
                  )}
                </Slice>
              </section>
            ) : (
              <Empty />
            )}
          </>
        )}
      </State>
    </>
  );
}
function Workspaces() {
  const { t, i18n } = useTranslation();
  const overview = useApi<Workspace[]>("/workspaces", { scoped: false });
  const spend = useApi<Spend>("/spend", { scoped: false });
  const merged = new Map((overview.data ?? []).map((w) => [w.workspace_id, w]));
  for (const w of spend.data?.workspaces ?? []) {
    if (!merged.has(w.workspace_id))
      merged.set(w.workspace_id, {
        ...w,
        monthly_savings_usd: 0,
        monthly_savings_addressable_usd: 0,
        recommendation_count: 0,
        monthly_waste_usd: 0,
      });
  }
  return (
    <>
      <Heading title={t("workspaces")} description={t("scopeNote")} />
      <State query={overview}>
        {(rows) =>
          rows.length ? (
            <section className="panel resource-panel">
              <Slice rows={Array.from(merged.values())}>
                {(items) => (
                  <Table
                    rows={items}
                    rowKey={(r) => r.workspace_id}
                    caption={t("workspaces")}
                    columns={[
                      {
                        key: "name",
                        label: t("workspace"),
                        render: (r) => (
                          <>
                            <ScopedLink
                              className="resource-link"
                              to={`/?workspace_id=${encodeURIComponent(r.workspace_id)}`}
                            >
                              {r.workspace_name ?? r.workspace_id}
                            </ScopedLink>
                            <span className="cell-sub">
                              <code>{r.workspace_id}</code>
                            </span>
                          </>
                        ),
                      },
                      {
                        key: "spend",
                        label: t("monthlySpend"),
                        numeric: true,
                        render: (r) => {
                          const s = spend.data?.workspaces.find(
                            (w) => w.workspace_id === r.workspace_id,
                          );
                          return s
                            ? money(s.monthly_spend_usd, i18n.language)
                            : "—";
                        },
                      },
                      {
                        key: "savings",
                        label: t("monthlyOpportunity"),
                        numeric: true,
                        render: (r) =>
                          money(
                            r.monthly_savings_addressable_usd,
                            i18n.language,
                          ),
                      },
                      {
                        key: "count",
                        label: t("recommendations"),
                        numeric: true,
                        render: (r) =>
                          number(r.recommendation_count, i18n.language),
                      },
                    ]}
                  />
                )}
              </Slice>
            </section>
          ) : (
            <Empty />
          )
        }
      </State>
    </>
  );
}
function HealthPage() {
  const { t, i18n } = useTranslation();
  const query = useApi<Health>("/data-health", { scoped: false });
  return (
    <>
      <Heading title={t("health")} description={t("healthNote")} />
      <State query={query}>
        {(d) => (
          <>
            <Notice tone={d.degraded || d.unknown ? "warning" : "info"}>
              {t(d.unknown ? "pending" : d.degraded ? "degraded" : "healthy")} ·{" "}
              {date(d.checked_at, i18n.language)} UTC
            </Notice>
            {d.last_run && (
              <section className="panel run-panel">
                <h2>{t("lastRun")}</h2>
                <p>
                  {d.last_run.finished_at
                    ? date(d.last_run.finished_at, i18n.language) + " UTC"
                    : t("notAvailable")}{" "}
                  ·{" "}
                  {t(
                    d.last_run.ok === true
                      ? "PASS"
                      : d.last_run.ok === false
                        ? "FAIL"
                        : "pending",
                  )}
                </p>
                {d.last_run.failed_flows.map((f) => (
                  <code key={f}>{f} </code>
                ))}
              </section>
            )}
            <section className="panel resource-panel">
              <Table
                rows={d.sources}
                rowKey={(r) => r.name}
                caption={t("health")}
                columns={[
                  {
                    key: "source",
                    label: t("source"),
                    render: (r) => (
                      <>
                        <code>{r.name}</code>
                        <span className="cell-sub">
                          {t(r.required ? "required" : "optional")}
                        </span>
                      </>
                    ),
                  },
                  {
                    key: "status",
                    label: t("status"),
                    render: (r) => <Badge value={r.status} />,
                  },
                  {
                    key: "rules",
                    label: t("blockedRules"),
                    render: (r) => (
                      <span className="code-list">
                        {r.blocks_rules.map((id) => (
                          <code key={id}>{id}</code>
                        ))}
                      </span>
                    ),
                  },
                  {
                    key: "reason",
                    label: t("reason"),
                    render: (r) =>
                      r.message ? (
                        <details>
                          <summary>{t("sourceError")}</summary>
                          <pre>{r.message}</pre>
                        </details>
                      ) : (
                        "—"
                      ),
                  },
                ]}
              />
            </section>
          </>
        )}
      </State>
    </>
  );
}
function Rules() {
  const { t } = useTranslation();
  const query = useApi<Rule[]>("/rules", { scoped: false });
  const [search, setSearch] = useState("");
  return (
    <>
      <Heading title={t("rules")} description={t("rulesNote")} />
      <label className="standalone-search">
        <span>{t("search")}</span>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      <State query={query}>
        {(rows) => (
          <section className="panel resource-panel">
            <Slice
              rows={rows.filter((r) =>
                `${r.id} ${r.title}`
                  .toLowerCase()
                  .includes(search.toLowerCase()),
              )}
            >
              {(items) => (
                <Table
                  rows={items}
                  rowKey={(r) => r.id}
                  caption={t("rules")}
                  columns={[
                    {
                      key: "title",
                      label: t("recommendation"),
                      render: (r) => (
                        <>
                          <strong>{r.title}</strong>
                          <span className="cell-sub">
                            <code>{r.id}</code> · v{r.version}
                          </span>
                        </>
                      ),
                    },
                    {
                      key: "resource",
                      label: t("resource"),
                      render: (r) => <code>{r.resource_type}</code>,
                    },
                    {
                      key: "status",
                      label: t("status"),
                      render: (r) => (
                        <Badge
                          value={r.enabled ? "PASS" : "disabled"}
                          label={t(r.enabled ? "enabled" : "disabled")}
                        />
                      ),
                    },
                    {
                      key: "config",
                      label: t("thresholds"),
                      render: (r) => (
                        <details>
                          <summary>{t("thresholds")}</summary>
                          <NumericEvidence value={r.thresholds} />
                        </details>
                      ),
                    },
                  ]}
                />
              )}
            </Slice>
          </section>
        )}
      </State>
    </>
  );
}
interface Anomaly {
  anomaly_key: string;
  resource_name: string | null;
  resource_type: string | null;
  sku_name: string | null;
  workspace_name: string | null;
  workspace_id: string;
  anomaly_type: string;
  baseline_median_usd: number;
  peak_cost_usd: number;
  excess_usd: number;
  first_flag_date: string;
  last_flag_date: string;
  series: number[];
}
function Anomalies() {
  const { t, i18n } = useTranslation();
  const query = useApi<{
    unavailable: boolean;
    missing_sections: string[];
    total_excess_usd: number;
    anomaly_count: number;
    workspace_count: number;
    eval_start: string;
    eval_end: string;
    daily: Day[];
    sku_anomalies: Anomaly[];
    resource_anomalies: Anomaly[];
  }>("/anomalies");
  return (
    <>
      <Heading title={t("anomalies")} description={t("anomaliesNote")} />
      <State query={query}>
        {(d) =>
          d.unavailable ? (
            <Empty />
          ) : (
            <>
              {d.missing_sections.length > 0 && (
                <Notice tone="warning">{t("unavailableSections")}</Notice>
              )}
              <Metrics
                items={[
                  {
                    label: t("excess"),
                    value: d.total_excess_usd,
                    note: `${d.eval_start ? date(d.eval_start, i18n.language, true) : "—"} – ${d.eval_end ? date(d.eval_end, i18n.language, true) : "—"}`,
                  },
                  {
                    label: t("anomalies"),
                    value: d.anomaly_count,
                    kind: "number",
                    note: t("evaluation"),
                  },
                  {
                    label: t("workspaces"),
                    value: d.workspace_count,
                    kind: "number",
                    note: t("evaluation"),
                  },
                ]}
              />
              <section className="panel">
                <h2>{t("dailySpend")}</h2>
                <SpendChart data={d.daily} />
              </section>
              {[
                { title: t("sku"), rows: d.sku_anomalies },
                { title: t("resources"), rows: d.resource_anomalies },
              ].map((section) => (
                <section className="panel resource-panel" key={section.title}>
                  <div className="section-heading">
                    <h2>{section.title}</h2>
                  </div>
                  {section.rows.length ? (
                    <Slice rows={section.rows}>
                      {(items) => (
                        <Table
                          rows={items}
                          rowKey={(r) => r.anomaly_key}
                          caption={section.title}
                          columns={[
                            {
                              key: "name",
                              label: t("resource"),
                              render: (r) => (
                                <>
                                  {r.resource_name ?? r.sku_name}
                                  <span className="cell-sub">
                                    {r.workspace_name ?? r.workspace_id}
                                  </span>
                                </>
                              ),
                            },
                            {
                              key: "type",
                              label: t("anomaly"),
                              render: (r) => <Badge value={r.anomaly_type} />,
                            },
                            {
                              key: "base",
                              label: t("baselineDaily"),
                              numeric: true,
                              render: (r) =>
                                money(r.baseline_median_usd, i18n.language),
                            },
                            {
                              key: "peak",
                              label: t("peak"),
                              numeric: true,
                              render: (r) =>
                                money(r.peak_cost_usd, i18n.language),
                            },
                            {
                              key: "excess",
                              label: t("excess"),
                              numeric: true,
                              render: (r) => money(r.excess_usd, i18n.language),
                            },
                            {
                              key: "series",
                              label: t("evidence"),
                              render: (r) => (
                                <details>
                                  <summary>{t("showData")}</summary>
                                  <NumericEvidence value={r.series} />
                                </details>
                              ),
                            },
                          ]}
                        />
                      )}
                    </Slice>
                  ) : (
                    <Empty />
                  )}
                </section>
              ))}
            </>
          )
        }
      </State>
    </>
  );
}
interface InventoryApp {
  app_id: string;
  app_name: string;
  workspace_name: string;
  workspace_id: string;
  compute_size: string;
  monthly_spend_usd: number;
  dbus: number;
  days_billed: number;
  last_deploy_time: string | null;
  last_deployed_by: string | null;
}
function Apps() {
  const { t, i18n } = useTranslation();
  const config = useApi<Config>("/config", { scoped: false });
  const query = useApi<{
    unavailable: boolean;
    missing_sections: string[];
    monthly_spend_usd: number;
    app_count: number;
    workspace_count: number;
    daily: Day[];
    apps: InventoryApp[];
  }>("/apps", { enabled: config.data?.apps_tab_enabled === true });
  if (config.data && !config.data.apps_tab_enabled)
    return (
      <>
        <Heading title={t("apps")} description={t("appNote")} />
        <Notice>{t("disabled")}</Notice>
      </>
    );
  return (
    <>
      <Heading title={t("apps")} description={t("appNote")} />
      <State query={query}>
        {(d) =>
          d.unavailable ? (
            <Empty />
          ) : (
            <>
              {d.missing_sections.length > 0 && (
                <Notice tone="warning">{t("unavailableSections")}</Notice>
              )}
              <Metrics
                items={[
                  {
                    label: t("spend"),
                    value: d.monthly_spend_usd,
                    note: t("monthly"),
                  },
                  {
                    label: t("appCount"),
                    value: d.app_count,
                    kind: "number",
                    note: "",
                  },
                  {
                    label: t("workspaces"),
                    value: d.workspace_count,
                    kind: "number",
                    note: "",
                  },
                ]}
              />
              <section className="panel">
                <h2>{t("dailySpend")}</h2>
                <SpendChart data={d.daily} />
              </section>
              <section className="panel resource-panel">
                {d.apps.length ? (
                  <Slice rows={d.apps}>
                    {(items) => (
                      <Table
                        rows={items}
                        rowKey={(r) => r.workspace_id + r.app_id}
                        caption={t("apps")}
                        columns={[
                          {
                            key: "name",
                            label: t("name"),
                            render: (r) => (
                              <>
                                {r.app_name ?? r.app_id}
                                <span className="cell-sub">
                                  {r.workspace_name ?? r.workspace_id}
                                </span>
                              </>
                            ),
                          },
                          {
                            key: "size",
                            label: t("size"),
                            render: (r) => r.compute_size,
                          },
                          {
                            key: "days",
                            label: t("billedDays"),
                            numeric: true,
                            render: (r) => number(r.days_billed, i18n.language),
                          },
                          {
                            key: "dbus",
                            label: t("dbus"),
                            numeric: true,
                            render: (r) => number(r.dbus, i18n.language, 2),
                          },
                          {
                            key: "spend",
                            label: t("monthlySpend"),
                            numeric: true,
                            render: (r) =>
                              money(r.monthly_spend_usd, i18n.language),
                          },
                          {
                            key: "owner",
                            label: t("owner"),
                            render: (r) => r.last_deployed_by ?? t("unknown"),
                          },
                        ]}
                      />
                    )}
                  </Slice>
                ) : (
                  <Empty />
                )}
              </section>
            </>
          )
        }
      </State>
    </>
  );
}
function GenieCosts() {
  const { t, i18n } = useTranslation();
  const config = useApi<Config>("/config", { scoped: false });
  const query = useApi<{
    unavailable: boolean;
    missing_sections: string[];
    monthly_spend_usd: number;
    total_dbus: number;
    workspace_count: number;
    daily: Day[];
    top_users: {
      principal: string;
      monthly_spend_usd: number;
      free_dbus: number;
      paid_dbus: number;
    }[];
    surfaces: {
      surface: string;
      channel: string;
      dbus: number;
      spend_usd: number;
      users: number;
    }[];
    spaces: {
      space_id: string;
      workspace_id: string;
      monthly_spend_usd: number;
      users: number;
    }[];
    workspaces: {
      workspace_id: string;
      workspace_name?: string;
      monthly_spend_usd: number;
      users: number;
    }[];
    free_tier: { free_dbus: number; paid_dbus: number };
  }>("/genie", { enabled: config.data?.genie_costs_tab_enabled === true });
  if (config.data && !config.data.genie_costs_tab_enabled)
    return (
      <>
        <Heading title={t("genie")} description={t("genieNote")} />
        <Notice>{t("disabled")}</Notice>
      </>
    );
  return (
    <>
      <Heading title={t("genie")} description={t("genieNote")} />
      <State query={query}>
        {(d) =>
          d.unavailable ? (
            <Empty />
          ) : (
            <>
              {d.missing_sections.length > 0 && (
                <Notice tone="warning">{t("unavailableSections")}</Notice>
              )}
              <Metrics
                items={[
                  {
                    label: t("spend"),
                    value: d.monthly_spend_usd,
                    note: t("monthly"),
                  },
                  {
                    label: t("freeTier"),
                    value: d.free_tier.free_dbus,
                    kind: "number",
                    note: "DBU",
                  },
                  {
                    label: t("paid"),
                    value: d.free_tier.paid_dbus,
                    kind: "number",
                    note: "DBU",
                  },
                ]}
              />
              <section className="panel">
                <h2>{t("dailySpend")}</h2>
                <SpendChart data={d.daily} />
              </section>
              <section className="panel resource-panel">
                <div className="section-heading">
                  <h2>{t("bySurface")}</h2>
                </div>
                <Table
                  rows={d.surfaces}
                  rowKey={(r) => r.surface + r.channel + r.spend_usd}
                  caption={t("bySurface")}
                  columns={[
                    {
                      key: "surface",
                      label: t("product"),
                      render: (r) => (
                        <code>
                          {r.surface} · {r.channel}
                        </code>
                      ),
                    },
                    {
                      key: "users",
                      label: t("users"),
                      numeric: true,
                      render: (r) => number(r.users, i18n.language),
                    },
                    {
                      key: "dbus",
                      label: "DBU",
                      numeric: true,
                      render: (r) => number(r.dbus, i18n.language, 2),
                    },
                    {
                      key: "spend",
                      label: "USD",
                      numeric: true,
                      render: (r) => money(r.spend_usd, i18n.language),
                    },
                  ]}
                />
              </section>
              <Notice>{t("genieCoverage")}</Notice>
              {[
                {
                  title: t("spaces"),
                  rows: d.spaces.map((r) => ({
                    id: `${r.workspace_id}/${r.space_id}`,
                    name: r.space_id,
                    workspace: r.workspace_id,
                    spend: r.monthly_spend_usd,
                    users: r.users,
                  })),
                },
                {
                  title: t("workspaces"),
                  rows: d.workspaces.map((r) => ({
                    id: r.workspace_id,
                    name: r.workspace_name ?? r.workspace_id,
                    workspace: r.workspace_id,
                    spend: r.monthly_spend_usd,
                    users: r.users,
                  })),
                },
              ].map((section) => (
                <section className="panel resource-panel" key={section.title}>
                  <div className="section-heading">
                    <h2>{section.title}</h2>
                  </div>
                  <Slice rows={section.rows}>
                    {(rows) => (
                      <Table
                        rows={rows}
                        rowKey={(r) => r.id}
                        caption={section.title}
                        columns={[
                          {
                            key: "name",
                            label: t("name"),
                            render: (r) => (
                              <>
                                {r.name}
                                <span className="cell-sub">{r.workspace}</span>
                              </>
                            ),
                          },
                          {
                            key: "users",
                            label: t("users"),
                            numeric: true,
                            render: (r) => number(r.users, i18n.language),
                          },
                          {
                            key: "spend",
                            label: t("monthlySpend"),
                            numeric: true,
                            render: (r) => money(r.spend, i18n.language),
                          },
                        ]}
                      />
                    )}
                  </Slice>
                </section>
              ))}
              <section className="panel resource-panel">
                <div className="section-heading">
                  <h2>{t("topUsers")}</h2>
                </div>
                <Slice rows={d.top_users}>
                  {(items) => (
                    <Table
                      rows={items}
                      rowKey={(r) => r.principal}
                      caption={t("topUsers")}
                      columns={[
                        {
                          key: "user",
                          label: t("name"),
                          render: (r) => r.principal,
                        },
                        {
                          key: "free",
                          label: t("freeTier") + " · DBU",
                          numeric: true,
                          render: (r) => number(r.free_dbus, i18n.language, 2),
                        },
                        {
                          key: "paid",
                          label: t("paid") + " · DBU",
                          numeric: true,
                          render: (r) => number(r.paid_dbus, i18n.language, 2),
                        },
                        {
                          key: "cost",
                          label: t("monthlySpend"),
                          numeric: true,
                          render: (r) =>
                            money(r.monthly_spend_usd, i18n.language),
                        },
                      ]}
                    />
                  )}
                </Slice>
              </section>
            </>
          )
        }
      </State>
    </>
  );
}
function Realized() {
  const { t, i18n } = useTranslation();
  const query = useApi<{
    unavailable: boolean;
    saved_usd: number;
    run_rate_usd: number;
    change_count: number;
    tracking_since: string | null;
    data_through: string | null;
    by_month: { month: string; saved_usd: number }[];
    by_kind: Record<string, { count: number; monthly_usd: number }>;
    not_counted: Record<string, { count: number; monthly_usd: number }>;
    partial_example: Record<string, Json> | null;
  }>("/savings-realized");
  return (
    <>
      <Heading title={t("realized")} description={t("trackedNote")} />
      <State query={query}>
        {(d) =>
          d.unavailable ? (
            <Notice tone="warning">{t("noTracker")}</Notice>
          ) : (
            <>
              <Metrics
                items={[
                  {
                    label: t("saved"),
                    value: d.saved_usd,
                    note: `${t("since")}: ${d.tracking_since ? date(d.tracking_since, i18n.language, true) : "—"}`,
                  },
                  {
                    label: t("runRate"),
                    value: d.run_rate_usd,
                    note: t("monthly"),
                  },
                  {
                    label: t("changes"),
                    value: d.change_count,
                    kind: "number",
                    note: `${t("updated")}: ${d.data_through ? date(d.data_through, i18n.language, true) : "—"}`,
                  },
                ]}
              />
              <section className="panel resource-panel">
                <Table
                  rows={d.by_month}
                  rowKey={(r) => r.month}
                  caption={t("saved")}
                  columns={[
                    {
                      key: "date",
                      label: t("month"),
                      render: (r) => month(r.month, i18n.language),
                    },
                    {
                      key: "value",
                      label: t("saved") + " · USD",
                      numeric: true,
                      render: (r) => money(r.saved_usd, i18n.language),
                    },
                  ]}
                />
              </section>
              {[
                { title: t("changes"), data: d.by_kind },
                { title: t("notCounted"), data: d.not_counted },
              ].map((section) => (
                <section className="panel resource-panel" key={section.title}>
                  <div className="section-heading">
                    <h2>{section.title}</h2>
                  </div>
                  <Table
                    rows={Object.entries(section.data)}
                    rowKey={(r) => r[0]}
                    caption={section.title}
                    columns={[
                      {
                        key: "key",
                        label: t("reason"),
                        render: (r) =>
                          t(`tracking.${r[0]}`, { defaultValue: r[0] }),
                      },
                      {
                        key: "count",
                        label: t("changes"),
                        numeric: true,
                        render: (r) => number(r[1].count, i18n.language),
                      },
                      {
                        key: "value",
                        label: t("monthlySpend"),
                        numeric: true,
                        render: (r) => money(r[1].monthly_usd, i18n.language),
                      },
                    ]}
                  />
                </section>
              ))}
              {d.partial_example && (
                <section className="panel">
                  <h2>{t("evidence")}</h2>
                  <NumericEvidence value={d.partial_example} />
                </section>
              )}
            </>
          )
        }
      </State>
    </>
  );
}
function Serverless() {
  const { t, i18n } = useTranslation();
  const query = useApi<{
    total_monthly_savings_usd: number;
    affected_resource_count: number;
    readiness: {
      spend_available: boolean;
      serverless_spend_usd: number;
      classic_spend_usd: number;
    };
    issues: {
      key: string;
      title: string;
      resource_count: number;
      monthly_savings_usd: number;
      examples: {
        resource_name: string;
        resource_id: string;
        group_id: string | null;
        monthly_savings_usd: number;
        detail: string;
      }[];
    }[];
    fleet: Record<string, Json>;
  }>("/serverless-case");
  return (
    <>
      <Heading title={t("serverless")} description={t("serverlessNote")} />
      <State query={query}>
        {(d) => (
          <>
            <Metrics
              items={[
                {
                  label: t("serverlessCompute"),
                  value: d.readiness.spend_available
                    ? d.readiness.serverless_spend_usd
                    : null,
                  note: t("monthly"),
                },
                {
                  label: t("classic"),
                  value: d.readiness.spend_available
                    ? d.readiness.classic_spend_usd
                    : null,
                  note: t("monthly"),
                },
                {
                  label: t("resources"),
                  value: d.affected_resource_count,
                  kind: "number",
                  note: "",
                },
              ]}
            />
            <Notice>{t("fitNote")}</Notice>
            {d.issues.map((issue) => (
              <section className="panel resource-panel" key={issue.key}>
                <div className="section-heading">
                  <h2>{t(`issues.${issue.key}`)}</h2>
                  <span>
                    {t("resources")}: {issue.resource_count}
                  </span>
                </div>
                <Table
                  rows={issue.examples}
                  rowKey={(r) => r.resource_id}
                  caption={t(`issues.${issue.key}`)}
                  columns={[
                    {
                      key: "resource",
                      label: t("resource"),
                      render: (r) =>
                        r.group_id ? (
                          <ScopedLink
                            to={`/resources/${r.group_id}`}
                            className="resource-link"
                          >
                            {r.resource_name}
                          </ScopedLink>
                        ) : (
                          r.resource_name
                        ),
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
            ))}
            <section className="panel">
              <details>
                <summary>{t("rawEvidence")}</summary>
                <NumericEvidence value={d.fleet} />
              </details>
            </section>
          </>
        )}
      </State>
    </>
  );
}
export default function AnalysisPage({ kind }: { kind: string }) {
  const scope = useScope();
  const pages: Record<string, ReactNode> = {
    workspaces: <Workspaces />,
    rules: <Rules />,
    "data-health": <HealthPage />,
    labs: <Findings labs />,
    tagging: <Findings labs={false} />,
    "cost-anomalies": <Anomalies />,
    apps: <Apps />,
    "genie-costs": <GenieCosts />,
    "savings-realized": <Realized />,
    serverless: <Serverless />,
  };
  return <div key={`${kind}:${scope}`}>{pages[kind] ?? <Empty />}</div>;
}
