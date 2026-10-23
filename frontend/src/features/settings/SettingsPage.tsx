import { useTranslation } from "react-i18next";
import { setLocale } from "../../lib/i18n";
import type { Config, Locale } from "../../lib/types";
import { Heading, Notice } from "../../components/ui";
export default function SettingsPage({ config }: { config?: Config }) {
  const { t, i18n } = useTranslation();
  return (
    <>
      <Heading title={t("settings")} description={t("settingsNote")} />
      <section className="panel settings-panel">
        <div className="setting">
          <div>
            <label htmlFor="setting-language">
              <strong>{t("language")}</strong>
            </label>
            <p id="setting-language-note">{t("prefsNote")}</p>
          </div>
          <select
            id="setting-language"
            aria-describedby="setting-language-note"
            value={i18n.language}
            onChange={(e) => setLocale(e.target.value as Locale)}
          >
            <option value="pt-BR" lang="pt-BR">
              Português (Brasil)
            </option>
            <option value="en" lang="en">
              English
            </option>
            <option value="es" lang="es">
              Español
            </option>
          </select>
        </div>
        <dl className="settings-list">
          <div>
            <dt>{t("currency")}</dt>
            <dd>USD</dd>
          </div>
          <div>
            <dt>{t("window")}</dt>
            <dd>{config ? `${config.lookback_days} ${t("days")}` : "—"}</dd>
          </div>
          <div>
            <dt>{t("mode")}</dt>
            <dd>
              <code>{config?.compute_mode ?? "—"}</code>
            </dd>
          </div>
          <div>
            <dt>{t("pricingSource")}</dt>
            <dd>
              {config?.pricing_mode
                ? t(`pricingMode.${config.pricing_mode}`, {
                    defaultValue: config.pricing_mode,
                  })
                : "—"}
            </dd>
          </div>
          <div>
            <dt>{t("version")}</dt>
            <dd>{config?.version ?? "—"}</dd>
          </div>
        </dl>
        <p className="section-note">{t("pricingModeNote")}</p>
        <Notice>
          <h2>{t("execution")}</h2>
          <p>{t("executionNote")}</p>
        </Notice>
      </section>
    </>
  );
}
