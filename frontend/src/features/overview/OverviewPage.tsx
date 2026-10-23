import type { ReactNode } from "react";
import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApi, useScope } from "../../lib/api";
import type { Overview, Spend, Page, Rule } from "../../lib/types";
import { date, money } from "../../lib/format";
import {
  Heading,
  Metrics,
  metricStatus,
  Notice,
  ScopedLink,
  State,
} from "../../components/ui";
import { SpendChart } from "../../components/Charts";
import ResourceTable from "../../components/ResourceTable";
import GeniePanel from "../resource/GeniePanel";
function ShareList({
  items,
  total,
}: {
  items: { key: string; label: ReactNode; amount: number; note: string }[];
  total: number;
}) {
  const { i18n } = useTranslation();
  return (
    <ul className="share-list">
      {items.map((item) => (
        <li key={item.key}>
          <div>
            <span className="share-label">{item.label}</span>
            <strong>{money(item.amount, i18n.language)}</strong>
          </div>
          <div className="share-track" aria-hidden="true">
            <span
              style={{
                width: `${total > 0 ? Math.min(100, (item.amount / total) * 100) : 0}%`,
              }}
            />
          </div>
          <p>{item.note}</p>
        </li>
      ))}
    </ul>
  );
}
function Breakdowns({ data }: { data: Overview }) {
  const { t } = useTranslation();
  const rules = useApi<Rule[]>("/rules", { scoped: false });
  const titles = new Map(rules.data?.map((r) => [r.id, r.title]));
  const total = data.total_monthly_savings_addressable_usd;
  const categories = data.savings_by_category ?? [];
  const levers = (data.savings_by_rule ?? []).slice(0, 5);
  // A single in-scope workspace has no leaderboard to show.
  const workspaces =
    data.workspaces.length > 1
      ? data.workspaces
          .filter((w) => w.monthly_savings_addressable_usd > 0)
          .slice(0, 5)
      : [];
  if (!categories.length && !levers.length) return null;
  return (
    <>
      <div className="breakdown-grid">
        {categories.length > 0 && (
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>{t("byCategory")}</h2>
                <p>{t("monthly")}</p>
              </div>
            </div>
            <ShareList
              total={total}
              items={categories.map((c) => ({
                key: c.key,
                label: t(`category.${c.key}`, { defaultValue: c.key }),
                amount: c.monthly_savings_usd,
                note: t("resourceCount", { count: c.resource_count }),
              }))}
            />
          </section>
        )}
        {levers.length > 0 && (
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>{t("topLevers")}</h2>
                <p>{t("topLeversNote")}</p>
              </div>
            </div>
            <ShareList
              total={total}
              items={levers.map((r) => ({
                key: r.key,
                label: (
                  <ScopedLink
                    to={`/recommendations?rule_ids=${encodeURIComponent(r.key)}`}
                  >
                    {titles.get(r.key) ?? r.key}
                  </ScopedLink>
                ),
                amount: r.monthly_savings_usd,
                note: t("resourceCount", { count: r.resource_count }),
              }))}
            />
          </section>
        )}
        {workspaces.length > 0 && (
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>{t("topWorkspaces")}</h2>
                <p>{t("monthly")}</p>
              </div>
            </div>
            <ShareList
              total={total}
              items={workspaces.map((w) => ({
                key: w.workspace_id,
                label: (
                  <ScopedLink
                    to={`/?workspace_id=${encodeURIComponent(w.workspace_id)}`}
                  >
                    {w.workspace_name ?? w.workspace_id}
                  </ScopedLink>
                ),
                amount: w.monthly_savings_addressable_usd,
                note: t("resourceCount", { count: w.recommendation_count }),
              }))}
            />
          </section>
        )}
      </div>
      <p className="section-note breakdown-note">{t("breakdownNote")}</p>
    </>
  );
}
export default function OverviewPage() {
  const { t, i18n } = useTranslation();
  const scope = useScope();
  const overview = useApi<Overview>("/account/overview");
  const spend = useApi<Spend>("/spend");
  const page = useApi<Page>("/recommendation-groups/page?page_size=5");
  const tracked = useApi<{
    unavailable: boolean;
    saved_usd: number;
    data_through: string | null;
    window_start: string | null;
    window_end: string | null;
  }>("/savings-realized");
  return (
    <>
      <Heading title={t("overview")} description={t("overviewDescription")} />
      <State query={overview}>
        {(data) => (
          <>
            <Metrics
              items={[
                {
                  label: t("spend"),
                  value: spend.data?.unavailable
                    ? null
                    : spend.data?.total_monthly_spend_usd,
                  note: t("monthly"),
                  status: metricStatus(spend),
                  onRetry: () => void spend.refetch(),
                },
                {
                  label: t("potential"),
                  value: data.total_monthly_savings_addressable_usd,
                  note: `${t("range")}: ${money(data.total_monthly_savings_low_usd, i18n.language)} – ${money(data.total_monthly_savings_high_usd, i18n.language)}`,
                  accent: true,
                },
                {
                  label: t("tracked"),
                  value: tracked.data?.unavailable
                    ? null
                    : tracked.data?.saved_usd,
                  note: `${
                    tracked.data?.window_start && tracked.data?.window_end
                      ? `${date(tracked.data.window_start, i18n.language, true)} – ${date(tracked.data.window_end, i18n.language, true)} · `
                      : tracked.data
                        ? `${t("window")}: ${t("notAvailable")} · `
                        : ""
                  }${t("trackedNote")}`,
                  status: metricStatus(tracked),
                  onRetry: () => void tracked.refetch(),
                },
              ]}
            />
            <div className="overview-charts">
              <section className="panel">
                <div className="section-heading">
                  <div>
                    <h2>{t("dailySpend")}</h2>
                    <p>{t("dailySpendNote")}</p>
                  </div>
                  <span className="data-unit">USD</span>
                </div>
                <State query={spend}>
                  {(s) =>
                    s.unavailable ? (
                      <Notice tone="warning">
                        {t("notAvailable")} ·{" "}
                        <ScopedLink to="/data-health">{t("health")}</ScopedLink>
                      </Notice>
                    ) : (
                      <SpendChart data={s.by_day} />
                    )
                  }
                </State>
              </section>
              <section className="panel confidence-panel">
                <div className="section-heading">
                  <div>
                    <h2>{t("opportunityBreakdown")}</h2>
                    <p>{t("monthly")}</p>
                  </div>
                </div>
                <div className="confidence-total">
                  <span>{t("potential")}</span>
                  <strong>
                    {money(
                      data.total_monthly_savings_addressable_usd,
                      i18n.language,
                    )}
                  </strong>
                </div>
                <ul className="confidence-list">
                  {data.savings_tiers.map((tier) => (
                    <li key={tier.tier}>
                      <div>
                        <span>
                          {t(tier.tier === "booked" ? "booked" : tier.tier)}
                        </span>
                        <strong>
                          {money(tier.monthly_savings_usd, i18n.language)}
                        </strong>
                      </div>
                      <div
                        className={`data-bar tier-${tier.tier}`}
                        style={{
                          width: `${data.total_monthly_savings_addressable_usd ? Math.max(0, Math.min(100, (tier.monthly_savings_usd / data.total_monthly_savings_addressable_usd) * 100)) : 0}%`,
                        }}
                      />
                    </li>
                  ))}
                </ul>
                <p className="section-note">{t("noComparison")}</p>
              </section>
            </div>
            <Breakdowns data={data} />
            <section className="panel resource-panel">
              <div className="section-heading">
                <div>
                  <h2>{t("topActions")}</h2>
                  <p>{t("topActionsNote")}</p>
                </div>
                <ScopedLink to="/recommendations" className="text-action">
                  {t("viewAll")}
                  <ArrowRight size={15} />
                </ScopedLink>
              </div>
              <State query={page}>
                {(p) => <ResourceTable items={p.items} />}
              </State>
            </section>
            <div className="overview-footnotes">
              <p>
                {scope ? t("scopeNoteWorkspace") : t("scopeNote")}
                {!scope &&
                  !!spend.data?.out_of_scope_workspaces &&
                  ` ${t("outOfScope", { count: spend.data.out_of_scope_workspaces })}`}
              </p>
              <p>
                {t("source")}: <code>system.billing.usage × list_prices</code>
                {spend.data?.lookback_days != null && (
                  <>
                    {" "}
                    · {t("window")}: {spend.data.lookback_days} {t("days")}
                  </>
                )}
              </p>
            </div>
            {(spend.data?.total_monthly_spend_contract_usd != null ||
              data.priced?.contract_available) && (
              <section className="panel rates-panel">
                <h2>{t("contract")}</h2>
                <p>{t("contractNote")}</p>
                <p className="section-note">
                  {t("source")}: <code>system.billing.account_prices</code>
                </p>
                {spend.data?.contract_priced_share != null && (
                  <p className="section-note">
                    {t("contractCoverage", {
                      percent: Math.round(
                        spend.data.contract_priced_share * 100,
                      ),
                    })}
                  </p>
                )}
                <Metrics
                  items={[
                    ...(spend.data?.total_monthly_spend_contract_usd != null
                      ? [
                          {
                            label: t("contractSpend"),
                            value: spend.data.total_monthly_spend_contract_usd,
                            note: t("monthly"),
                          },
                        ]
                      : []),
                    ...(data.priced?.contract_available
                      ? [
                          {
                            label: t("potential"),
                            value: data.priced.monthly_savings_addressable_usd,
                            note: t("monthly"),
                          },
                          {
                            label: t("contract"),
                            value: data.priced.contract_part_usd,
                            note: t("potential"),
                          },
                          {
                            label: t("list"),
                            value: data.priced.list_part_usd,
                            note: t("potential"),
                          },
                        ]
                      : []),
                  ]}
                />
              </section>
            )}
            {data.priced?.cloud_available && (
              <Notice>
                {t("cloud")}:{" "}
                {money(data.priced.monthly_cloud_savings_usd, i18n.language)} ·{" "}
                {t("potential")}
              </Notice>
            )}
            <GeniePanel />
          </>
        )}
      </State>
    </>
  );
}
