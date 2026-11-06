import { type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
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
import { date, instant, money, month, number } from "../../lib/format";
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
import { DataTable, type DataColumn } from "../../components/DataTable";

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
                <DataTable
                  rows={d.items}
                  rowKey={(r) => r.finding_key}
                  caption={t(labs ? "labs" : "tagging")}
                  search={(r) =>
                    `${r.resource_name} ${r.rule_title} ${r.workspace_name ?? ""} ${r.workspace_id}`
                  }
                  defaultSort={{ key: "spend", dir: "desc" }}
                  columns={[
                    {
                      key: "resource",
                      label: t("resource"),
                      sort: (r) => r.resource_name,
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
                      sort: (r) => r.rule_title,
                      render: (r) => r.rule_title,
                    },
                    {
                      key: "spend",
                      label: t("monthlySpend"),
                      numeric: true,
                      sort: (r) => r.monthly_waste_usd,
                      render: (r) => money(r.monthly_waste_usd, i18n.language),
                    },
                  ]}
                />
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
              <DataTable
                rows={Array.from(merged.values())}
                rowKey={(r) => r.workspace_id}
                caption={t("workspaces")}
                search={(r) => `${r.workspace_name ?? ""} ${r.workspace_id}`}
                columns={[
                  {
                    key: "name",
                    label: t("workspace"),
                    sort: (r) => r.workspace_name ?? r.workspace_id,
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
                    sort: (r) =>
                      spend.data?.workspaces.find(
                        (w) => w.workspace_id === r.workspace_id,
                      )?.monthly_spend_usd ?? null,
                    render: (r) => {
                      const s = spend.data?.workspaces.find(
                        (w) => w.workspace_id === r.workspace_id,
                      );
                      return s ? money(s.monthly_spend_usd, i18n.language) : "—";
                    },
                  },
                  {
                    key: "savings",
                    label: t("monthlyOpportunity"),
                    numeric: true,
                    sort: (r) => r.monthly_savings_addressable_usd,
                    render: (r) =>
                      money(r.monthly_savings_addressable_usd, i18n.language),
                  },
                  {
                    key: "count",
                    label: t("recommendations"),
                    numeric: true,
                    sort: (r) => r.recommendation_count,
                    render: (r) => number(r.recommendation_count, i18n.language),
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
              {instant(d.checked_at, i18n.language)}
            </Notice>
            {d.last_run && (
              <section className="panel run-panel">
                <h2>{t("lastRun")}</h2>
                <p>
                  {d.last_run.finished_at
                    ? instant(d.last_run.finished_at, i18n.language)
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
  return (
    <>
      <Heading title={t("rules")} description={t("rulesNote")} />
      <State query={query}>
        {(rows) => (
          <section className="panel resource-panel">
            <DataTable
 rows={rows}
 search={(r) => `${r.id} ${r.title} ${r.resource_type}`}
                  rowKey={(r) => r.id}
                  caption={t("rules")}
                  columns={[
                    {
                      key: "title",
 sort: (r) => r.title,
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
 sort: (r) => r.resource_type,
                      label: t("resource"),
                      render: (r) => <code>{r.resource_type}</code>,
                    },
                    {
                      key: "status",
 sort: (r) => r.enabled ? 1 : 0,
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
                    <DataTable
 rows={section.rows}
 search={(r) => `${r.resource_name ?? ""} ${r.sku_name ?? ""} ${r.workspace_name ?? ""} ${r.workspace_id} ${r.anomaly_type}`}
 defaultSort={{ key: "excess", dir: "desc" }}
                          rowKey={(r) => r.anomaly_key}
                          caption={section.title}
                          columns={[
                            {
                              key: "name",
 sort: (r) => r.resource_name ?? r.sku_name,
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
 sort: (r) => r.anomaly_type,
                              label: t("anomaly"),
                              render: (r) => <Badge value={r.anomaly_type} />,
                            },
                            {
                              key: "base",
 sort: (r) => r.baseline_median_usd,
                              label: t("baselineDaily"),
                              numeric: true,
                              render: (r) =>
                                money(r.baseline_median_usd, i18n.language),
                            },
                            {
                              key: "peak",
 sort: (r) => r.peak_cost_usd,
                              label: t("peak"),
                              numeric: true,
                              render: (r) =>
                                money(r.peak_cost_usd, i18n.language),
                            },
                            {
                              key: "excess",
 sort: (r) => r.excess_usd,
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
                  <DataTable
 rows={d.apps}
 search={(r) => `${r.app_name ?? ""} ${r.app_id} ${r.workspace_name ?? ""} ${r.workspace_id} ${r.last_deployed_by ?? ""}`}
 defaultSort={{ key: "spend", dir: "desc" }}
                        rowKey={(r) => r.workspace_id + r.app_id}
                        caption={t("apps")}
                        columns={[
                          {
                            key: "name",
 sort: (r) => r.app_name ?? r.app_id,
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
 sort: (r) => r.compute_size,
                            label: t("size"),
                            render: (r) => r.compute_size,
                          },
                          {
                            key: "days",
 sort: (r) => r.days_billed,
                            label: t("billedDays"),
                            numeric: true,
                            render: (r) => number(r.days_billed, i18n.language),
                          },
                          {
                            key: "dbus",
 sort: (r) => r.dbus,
                            label: t("dbus"),
                            numeric: true,
                            render: (r) => number(r.dbus, i18n.language, 2),
                          },
                          {
                            key: "spend",
 sort: (r) => r.monthly_spend_usd,
                            label: t("monthlySpend"),
                            numeric: true,
                            render: (r) =>
                              money(r.monthly_spend_usd, i18n.language),
                          },
                          {
                            key: "owner",
 sort: (r) => r.last_deployed_by,
                            label: t("owner"),
                            render: (r) => r.last_deployed_by ?? t("unknown"),
                          },
                        ]}
/>
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
type GenieUnit = "usd" | "dbu";
/** What every Genie table row carries, per 30-day month. */
interface Amounts {
  billed: number;
  freeValue: number;
  total: number;
  freeDbus: number;
  paidDbus: number;
  dbus: number;
}
function GenieCosts() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const unit: GenieUnit = params.get("unit") === "dbu" ? "dbu" : "usd";
  const setUnit = (next: GenieUnit) =>
    setParams(
      (current) => {
        const copy = new URLSearchParams(current);
        next === "usd" ? copy.delete("unit") : copy.set("unit", next);
        return copy;
      },
      { replace: true },
    );
  // Every table ends in the same three columns, so a row reads the same whether it
  // is a user, a space, a workspace or a product. In USD the free tier shows what
  // its usage would cost at the paid rate, next to what was billed; in DBU it is
  // the free and paid volumes. Either way the last column is their sum.
  const amountColumns = <T extends Amounts>(): DataColumn<T>[] => {
    const usd = (key: keyof Amounts, label: string): DataColumn<T> => ({
      key,
      label,
      numeric: true,
      sort: (r) => r[key],
      render: (r) => money(r[key], i18n.language),
    });
    const dbu = (key: keyof Amounts, label: string): DataColumn<T> => ({
      key,
      label,
      numeric: true,
      sort: (r) => r[key],
      render: (r) => number(r[key], i18n.language, 2),
    });
    return unit === "usd"
      ? [
          usd("billed", t("genieColBilled")),
          usd("freeValue", t("genieColFreeEq")),
          usd("total", t("genieColTotal")),
        ]
      : [
          dbu("freeDbus", t("genieColFreeDbu")),
          dbu("paidDbus", t("genieColPaidDbu")),
          dbu("dbus", t("genieColTotalDbu")),
        ];
  };
  const defaultSort = { key: unit === "usd" ? "total" : "dbus", dir: "desc" } as const;
  const config = useApi<Config>("/config", { scoped: false });
  const query = useApi<{
    unavailable: boolean;
    missing_sections: string[];
    lookback_days: number;
    monthly_spend_usd: number;
    monthly_dbus: number;
    monthly_total_value_usd: number;
    total_dbus: number;
    workspace_count: number;
    daily: Day[];
    top_users: {
      principal: string;
      monthly_spend_usd: number;
      monthly_dbus: number;
      monthly_free_dbus: number;
      monthly_paid_dbus: number;
      monthly_free_equivalent_usd: number;
      monthly_total_value_usd: number;
    }[];
    surfaces: {
      surface: string;
      surface_label: string;
      channel: string;
      is_free: boolean;
      monthly_dbus: number;
      monthly_spend_usd: number;
      monthly_equivalent_usd: number;
      monthly_total_value_usd: number;
      users: number;
    }[];
    spaces: {
      space_id: string;
      workspace_id: string;
      workspace_name?: string;
      monthly_spend_usd: number;
      monthly_dbus: number;
      monthly_free_dbus: number;
      monthly_paid_dbus: number;
      monthly_free_equivalent_usd: number;
      monthly_total_value_usd: number;
      users: number;
    }[];
    space_total: number;
    workspaces: {
      workspace_id: string;
      workspace_name?: string;
      monthly_spend_usd: number;
      monthly_dbus: number;
      monthly_free_dbus: number;
      monthly_paid_dbus: number;
      monthly_free_equivalent_usd: number;
      monthly_total_value_usd: number;
      users: number;
    }[];
    free_tier: {
      total_users: number;
      monthly_free_dbus: number;
      monthly_paid_dbus: number;
      monthly_free_equivalent_usd: number;
    };
  }>("/genie", { enabled: config.data?.genie_costs_tab_enabled === true });
  if (config.data && !config.data.genie_costs_tab_enabled)
    return (
      <>
        <Heading title={t("genie")} description={t("genieNote")} />
        <Notice>{t("disabled")}</Notice>
      </>
    );
  const toggle = (
    <div className="unit-toggle" role="group" aria-label={t("genieUnit")}>
      {(["usd", "dbu"] as const).map((u) => (
        <button
          key={u}
          aria-pressed={unit === u}
          onClick={() => setUnit(u)}
        >
          {u.toUpperCase()}
        </button>
      ))}
    </div>
  );
  return (
    <>
      <Heading
        title={t("genie")}
        description={t("genieNote")}
        aside={toggle}
      />
      <State query={query}>
        {(d) =>
          d.unavailable ? (
            <Empty />
          ) : (
            <>
              {d.missing_sections.length > 0 && (
                <Notice tone="warning">{t("unavailableSections")}</Notice>
              )}
              {d.lookback_days < 30 && (
                <Notice tone="warning">
                  {t("genieShortWindow", { days: d.lookback_days })}
                </Notice>
              )}
              <Metrics
                items={
                  unit === "dbu"
                    ? [
                        {
                          label: t("paid"),
                          value: d.free_tier.monthly_paid_dbus,
                          kind: "number",
                          note: t("geniePaidNote"),
                        },
                        {
                          label: t("freeTier"),
                          value: d.free_tier.monthly_free_dbus,
                          kind: "number",
                          note: t("genieFreeNote"),
                        },
                        {
                          label: t("genieTotalDbu"),
                          value: d.monthly_dbus,
                          kind: "number",
                          note: t("monthlyDbu"),
                        },
                      ]
                    : [
                        {
                          label: t("genieBilled"),
                          value: d.monthly_spend_usd,
                          note: t("genieBilledNote"),
                        },
                        {
                          label: t("genieFreeEquivalent"),
                          value: d.free_tier.monthly_free_equivalent_usd,
                          note: t("genieFreeEquivalentNote"),
                        },
                        {
                          label: t("genieTotalValue"),
                          value: d.monthly_total_value_usd,
                          note: t("genieTotalValueNote"),
                        },
                      ]
                }
              />
              <section className="panel">
                <div className="section-heading">
                  <div>
                    <h2>{t(unit === "dbu" ? "dailyUsage" : "dailySpend")}</h2>
                    <p>{t("genieDescDaily")}</p>
                  </div>
                </div>
                <SpendChart
                  data={d.daily}
                  unit={unit}
                  title={t(unit === "dbu" ? "dailyUsage" : "dailySpend")}
                />
              </section>
              {d.space_total > d.spaces.length && (
                <Notice>
                  {t("genieRowsCut", {
                    shown: d.spaces.length,
                    total: d.space_total,
                  })}
                </Notice>
              )}
              {d.free_tier.total_users > d.top_users.length && (
                <Notice>
                  {t("genieRowsCut", {
                    shown: d.top_users.length,
                    total: d.free_tier.total_users,
                  })}
                </Notice>
              )}
              <section className="panel resource-panel">
                <div className="section-heading">
                  <div>
                    <h2>{t("bySurface")}</h2>
                    <p>{t("genieDescSurface")}</p>
                  </div>
                </div>
                <DataTable
                  key={unit}
                  rows={d.surfaces.map((r) => ({
                    ...r,
                    billed: r.monthly_spend_usd,
                    freeValue: r.monthly_equivalent_usd,
                    total: r.monthly_total_value_usd,
                    freeDbus: r.is_free ? r.monthly_dbus : 0,
                    paidDbus: r.is_free ? 0 : r.monthly_dbus,
                    dbus: r.monthly_dbus,
                  }))}
                  rowKey={(r) => `${r.surface}|${r.channel}|${r.is_free}`}
                  caption={t("bySurface")}
                  search={(r) =>
                    `${r.surface_label} ${r.surface} ${r.channel} ${t(r.is_free ? "tierFree" : "tierPaid")}`
                  }
                  defaultSort={defaultSort}
                  columns={[
                    {
                      key: "surface",
                      label: t("product"),
                      sort: (r) => r.surface_label,
                      render: (r) => (
                        <>
                          {r.surface_label}
                          <span className="cell-sub">
                            <code>{r.surface}</code> · {r.channel}
                          </span>
                        </>
                      ),
                    },
                    {
                      key: "tier",
                      label: t("tier"),
                      sort: (r) => (r.is_free ? 0 : 1),
                      render: (r) => (
                        <Badge
                          value={r.is_free ? "free" : "paid"}
                          label={t(r.is_free ? "tierFree" : "tierPaid")}
                        />
                      ),
                    },
                    {
                      key: "users",
                      label: t("users"),
                      numeric: true,
                      sort: (r) => r.users,
                      render: (r) => number(r.users, i18n.language),
                    },
                    ...amountColumns(),
                  ]}
                />
              </section>
              <Notice>{t("genieCoverage")}</Notice>
              {[
                {
                  title: t("spaces"),
                  description: t("genieDescSpaces"),
                  rows: d.spaces.map((r) => ({
                    id: `${r.workspace_id}/${r.space_id}`,
                    name: r.space_id,
                    workspace: r.workspace_name ?? r.workspace_id,
                    workspaceId: r.workspace_id,
                    users: r.users,
                    billed: r.monthly_spend_usd,
                    freeValue: r.monthly_free_equivalent_usd,
                    total: r.monthly_total_value_usd,
                    freeDbus: r.monthly_free_dbus,
                    paidDbus: r.monthly_paid_dbus,
                    dbus: r.monthly_dbus,
                  })),
                },
                {
                  title: t("workspaces"),
                  description: t("genieDescWorkspaces"),
                  rows: d.workspaces.map((r) => ({
                    id: r.workspace_id,
                    name: r.workspace_name ?? r.workspace_id,
                    workspace: r.workspace_id,
                    workspaceId: r.workspace_id,
                    users: r.users,
                    billed: r.monthly_spend_usd,
                    freeValue: r.monthly_free_equivalent_usd,
                    total: r.monthly_total_value_usd,
                    freeDbus: r.monthly_free_dbus,
                    paidDbus: r.monthly_paid_dbus,
                    dbus: r.monthly_dbus,
                  })),
                },
              ].map((section) => (
                <section className="panel resource-panel" key={section.title}>
                  <div className="section-heading">
                    <div>
                      <h2>{section.title}</h2>
                      <p>{section.description}</p>
                    </div>
                  </div>
                  <DataTable
                    key={unit}
                    rows={section.rows}
                    rowKey={(r) => r.id}
                    caption={section.title}
                    search={(r) => `${r.name} ${r.workspace} ${r.workspaceId}`}
                    defaultSort={defaultSort}
                    columns={[
                      {
                        key: "name",
                        label: t("name"),
                        sort: (r) => r.name,
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
                        sort: (r) => r.users,
                        render: (r) => number(r.users, i18n.language),
                      },
                      ...amountColumns(),
                    ]}
                  />
                </section>
              ))}
              <section className="panel resource-panel">
                <div className="section-heading">
                  <div>
                    <h2>{t("topUsers")}</h2>
                    <p>{t("genieDescUsers")}</p>
                  </div>
                </div>
                <DataTable
                  key={unit}
                  rows={d.top_users.map((r) => ({
                    ...r,
                    billed: r.monthly_spend_usd,
                    freeValue: r.monthly_free_equivalent_usd,
                    total: r.monthly_total_value_usd,
                    freeDbus: r.monthly_free_dbus,
                    paidDbus: r.monthly_paid_dbus,
                    dbus: r.monthly_dbus,
                  }))}
                  rowKey={(r) => r.principal}
                  caption={t("topUsers")}
                  search={(r) => r.principal}
                  defaultSort={defaultSort}
                  columns={[
                    {
                      key: "user",
                      label: t("name"),
                      sort: (r) => r.principal,
                      render: (r) => r.principal,
                    },
                    ...amountColumns(),
                  ]}
                />
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
                <DataTable
                  rows={issue.examples}
                  rowKey={(r) => r.resource_id}
                  caption={t(`issues.${issue.key}`)}
                  columns={[
                    {
                      key: "resource",
 sort: (r) => r.resource_name,
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
 sort: (r) => r.monthly_savings_usd,
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
