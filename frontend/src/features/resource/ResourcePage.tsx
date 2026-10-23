import { useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowUpRight } from "lucide-react";
import { useApi } from "../../lib/api";
import type { Finding, Group } from "../../lib/types";
import { date, money } from "../../lib/format";
import {
  Back,
  Badge,
  Heading,
  Metrics,
  Notice,
  State,
} from "../../components/ui";
import { EvidenceChart, isRecord } from "../../components/Charts";
import { MetricTable } from "../../components/evidence/Metric";
import { proseMessage } from "../../lib/prose";
import GeniePanel from "./GeniePanel";
function resourceUrl(f: Finding) {
  try {
    const host = new URL(f.workspace_url ?? "");
    if (
      host.protocol !== "https:" ||
      !(
        host.hostname.endsWith(".databricks.com") ||
        host.hostname.endsWith(".azuredatabricks.net")
      )
    )
      return null;
    const id = encodeURIComponent(f.resource_id);
    const routes: Record<string, string> = {
      cluster: `/compute/clusters/${id}`,
      warehouse: `/sql/warehouses/${id}`,
      job: `/jobs/${id}`,
      pipeline: `/pipelines/${id}`,
      serving_endpoint: `/ml/endpoints/${id}`,
    };
    return routes[f.resource_type]
      ? `${host.origin}${routes[f.resource_type]}`
      : null;
  } catch {
    return null;
  }
}
/** A rule's caveat about its own data, localized when the sentence is known. */
function DataWarning({ text }: { text: string }) {
  const { t } = useTranslation();
  const known = proseMessage(text);
  return known ? <p>{t(known.key, known.params)}</p> : <p lang="en">{text}</p>;
}
function FindingDetail({
  finding: f,
  best = false,
  headless = false,
}: {
  finding: Finding;
  best?: boolean;
  headless?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const presentation = f.presentation;
  const viz = f.evidence.viz;
  const extras = Array.isArray(f.evidence.viz_extra)
    ? f.evidence.viz_extra.filter(isRecord)
    : [];
  const link = resourceUrl(f);
  const entries = Object.entries(f.evidence).filter(
    ([key]) =>
      ![
        "viz",
        "viz_extra",
        "messages",
        "data_basis",
        "confidence",
        "data_warning",
      ].includes(key),
  );
  return (
    <article className="finding" id={f.finding_key}>
      {!headless && (
        <div className="finding-heading">
          <div className="finding-title">
            {best && <span className="best-option">{t("bestOption")}</span>}
            <h2>{f.rule_title}</h2>
          </div>
          <div>
            <Badge value={f.priority} />
            <Badge value={f.savings_status} />
          </div>
        </div>
      )}
      <div className="finding-meta">
        <span>
          {t("confidence")}:{" "}
          <strong>{t(String(f.evidence.confidence ?? "unknown"))}</strong>
        </span>
        <span>
          {t("effort")}: <strong>{t(f.effort_band)}</strong>
        </span>
        <span>
          {t("owner")}: <strong>{f.resource_owner ?? t("unknown")}</strong>
        </span>
        <span>
          {t("snapshot")}: {date(f.computed_at, i18n.language)} UTC
        </span>
      </div>
      <Metrics
        items={[
          {
            label: t("potential"),
            value: f.monthly_savings_usd,
            note: t("monthly"),
            accent: true,
          },
          {
            label: t("range"),
            value: f.monthly_savings_low_usd,
            note: `${t("range")}: ${money(f.monthly_savings_low_usd, i18n.language)} – ${money(f.monthly_savings_high_usd, i18n.language)}`,
          },
          {
            label: t("observedSpend"),
            value: f.monthly_waste_usd,
            note: t("monthly"),
          },
        ]}
      />
      <div className="guidance">
        <section>
          <h3>{t("why")}</h3>
          <p>{presentation?.why ?? f.why}</p>
        </section>
        <section>
          <h3>{t("how")}</h3>
          <p>{presentation?.how ?? f.how}</p>
          {link ? (
            <a
              className="button primary"
              target="_blank"
              rel="noopener noreferrer"
              href={link}
            >
              {t("openDatabricks")}
              <span className="sr-only"> ({t("newTab")})</span>
              <ArrowUpRight size={15} aria-hidden="true" />
            </a>
          ) : (
            <p className="section-note">{t("noLink")}</p>
          )}
        </section>
      </div>
      {presentation && (
        <details className="raw-evidence">
          <summary>{t("originalGuidance")}</summary>
          <p className="section-note">{t("originalNote")}</p>
          <div lang="en" className="prose">
            <h3>{t("why")}</h3>
            <p>{presentation.original_why}</p>
            <h3>{t("how")}</h3>
            <p>{presentation.original_how}</p>
          </div>
        </details>
      )}
      <section className="evidence-section">
        <h3>{t("evidence")}</h3>
        {typeof f.evidence.data_warning === "string" && (
          <Notice tone="warning">
            <DataWarning text={f.evidence.data_warning} />
          </Notice>
        )}
        {isRecord(viz) && <EvidenceChart viz={viz} />}{" "}
        {extras.map((v, i) => (
          <EvidenceChart key={i} viz={v} />
        ))}
        {typeof f.evidence.data_basis === "string" &&
          f.evidence.data_basis.trim() && (
            <details className="raw-evidence">
              <summary>{t("ev.howMeasured")}</summary>
              <p className="section-note">{t("ev.howMeasuredNote")}</p>
              <p lang="en" className="basis-text">
                {f.evidence.data_basis}
              </p>
            </details>
          )}
        <details className="raw-evidence" open={!isRecord(viz)}>
          <summary>{t("rawEvidence")}</summary>
          <p className="section-note">{t("technicalNote")}</p>
          <MetricTable entries={entries} caption={t("rawEvidence")} />
        </details>
        {presentation?.sources?.length ? (
          <p className="source-note">
            {t("source")}:{" "}
            {presentation.sources.map((s) => (
              <code key={s}>{s} </code>
            ))}
          </p>
        ) : null}
      </section>
      <Notice tone="warning">
        <h3>{t("caveats")}</h3>
        <p>{presentation?.caution ?? t("optionWarning")}</p>
      </Notice>
      {(f.monthly_savings_contract_usd != null ||
        f.monthly_cloud_savings_usd != null) && (
        <dl className="inline-facts">
          {f.monthly_savings_contract_usd != null && (
            <div>
              <dt>{t("contract")}</dt>
              <dd>{money(f.monthly_savings_contract_usd, i18n.language)}</dd>
            </div>
          )}
          {f.monthly_cloud_savings_usd != null && (
            <div>
              <dt>{t("cloud")}</dt>
              <dd>{money(f.monthly_cloud_savings_usd, i18n.language)}</dd>
            </div>
          )}
        </dl>
      )}
    </article>
  );
}
export default function ResourcePage() {
  const { groupId, recommendationId } = useParams();
  const { hash } = useLocation();
  const { t, i18n } = useTranslation();
  const group = useApi<Group>(`/recommendation-groups/${groupId}`, {
    enabled: !!groupId,
  });
  const finding = useApi<Finding>(`/recommendations/${recommendationId}`, {
    enabled: !!recommendationId,
  });
  if (recommendationId)
    return (
      <>
        <Back />
        <State query={finding}>
          {(f) => (
            <>
              <Heading
                title={f.resource_name}
                description={t("resourceDescription")}
              />
              <div className="resource-id">
                <code>
                  {f.resource_type} · {f.resource_id}
                </code>
                <span>{f.workspace_name ?? f.workspace_id}</span>
              </div>
              <FindingDetail finding={f} />
            </>
          )}
        </State>
        <GeniePanel />
      </>
    );
  return (
    <>
      <Back />
      <State query={group}>
        {(g) => (
          <>
            <Heading
              title={g.resource_name}
              description={t("resourceDescription")}
            />
            <div className="resource-id">
              <code>
                {g.resource_type} · {g.resource_id}
              </code>
              <span>{g.workspace_name ?? g.workspace_id}</span>
            </div>
            {g.option_count > 1 && <Notice>{t("optionWarning")}</Notice>}
            {g.serverless_fit && (
              <Notice>
                <strong>
                  {t("serverless")}: {t(g.serverless_fit.verdict)}
                </strong>
                <p>{t("fitNote")}</p>
                {(g.serverless_fit.headline || g.serverless_fit.reason) && (
                  <details className="data-disclosure">
                    <summary>{t("fitDetails")}</summary>
                    <p lang="en" className="basis-text">
                      {g.serverless_fit.headline && (
                        <strong>{g.serverless_fit.headline} </strong>
                      )}
                      {g.serverless_fit.reason}
                    </p>
                  </details>
                )}
              </Notice>
            )}
            {g.options[0] && (
              <FindingDetail
                finding={g.options[0]}
                best={g.options.length > 1}
              />
            )}
            {g.options.length > 1 && (
              <section className="option-stack">
                <h2>{t("otherOptions")}</h2>
                {g.options.slice(1).map((f) => (
                  <details
                    key={f.finding_key}
                    className="option-collapsed"
                    open={hash === `#${f.finding_key}` || undefined}
                  >
                    <summary>
                      <strong>{f.rule_title}</strong>
                      <span className="option-amount">
                        {money(f.monthly_savings_usd, i18n.language)}
                      </span>
                      <span className="option-meta">
                        {t("effort")}: {t(f.effort_band)}
                      </span>
                      <Badge value={f.priority} />
                      <Badge value={f.savings_status} />
                    </summary>
                    <FindingDetail finding={f} headless />
                  </details>
                ))}
              </section>
            )}
          </>
        )}
      </State>
      <GeniePanel />
    </>
  );
}
