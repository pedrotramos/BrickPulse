import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation } from "@tanstack/react-query";
import { ApiError, useApi, useScope, request } from "../../lib/api";
import type { Config } from "../../lib/types";
import { Notice, Table } from "../../components/ui";
export default function GeniePanel() {
  const { t, i18n } = useTranslation();
  const config = useApi<Config>("/config", { scoped: false });
  const scope = useScope();
  const [question, setQuestion] = useState("");
  const response = useMutation({
    mutationFn: () =>
      request<{
        text: string;
        query?: string;
        error?: string;
        columns: string[];
        rows: unknown[][];
        truncated: boolean;
      }>("/genie/ask", i18n.language, undefined, {
        question,
        workspace_id: scope || null,
      }),
  });
  if (!config.data?.genie_enabled) return null;
  if (config.data.genie_mode !== "api")
    return !config.data.genie_embed_url ? (
      <section className="panel">
        <h2>{t("askGenie")}</h2>
        <Notice>{t("genieDisabled")}</Notice>
      </section>
    ) : (
      <section className="panel">
        <h2>{t("askGenie")}</h2>
        <p>{t("aiDisclaimer")}</p>
        <iframe
          title={t("askGenie")}
          src={config.data.genie_embed_url}
          className="genie-frame"
          referrerPolicy="same-origin"
        />
      </section>
    );
  return (
    <section className="panel genie-panel">
      <h2>{t("askGenie")}</h2>
      <p>{t("aiDisclaimer")}</p>
      <p className="section-note">{t("genieIdentity")}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          response.mutate();
        }}
      >
        <p className="section-note" id="genie-try">
          {t("genieTry")}
        </p>
        <div
          className="genie-suggestions"
          role="group"
          aria-labelledby="genie-try"
        >
          {["genieQ1", "genieQ2", "genieQ3", "genieQ4"].map((key) => (
            <button
              key={key}
              type="button"
              className="subtle"
              onClick={() => setQuestion(t(key))}
            >
              {t(key)}
            </button>
          ))}
        </div>
        <label>
          {t("question")}
          <textarea
            required
            maxLength={4000}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        <button
          type="submit"
          className="primary"
          disabled={response.isPending || !question.trim()}
        >
          {t(response.isPending ? "asking" : "send")}
        </button>
      </form>
      {response.isError && (
        <Notice tone="warning">
          <span role="alert">
            {t(
              response.error instanceof ApiError
                ? response.error.code
                : "loadError",
            )}
          </span>
        </Notice>
      )}
      {/* The live region exists before the answer so screen readers announce it. */}
      <div aria-live="polite">
        {response.data && (
          <>
            <h3>{t("answer")}</h3>
            {response.data.error ? (
              <Notice tone="warning">{t("loadError")}</Notice>
            ) : (
              <p className="prose">{response.data.text}</p>
            )}
            {response.data.query && (
              <details>
                <summary>{t("generatedSql")}</summary>
                <pre>
                  <code>{response.data.query}</code>
                </pre>
              </details>
            )}
            {!!response.data.rows?.length && (
              <Table
                rows={response.data.rows.map((values, index) => ({
                  values,
                  index,
                }))}
                rowKey={(r) => String(r.index)}
                caption={t("answer")}
                columns={response.data.columns.map((name, index) => ({
                  key: String(index),
                  label: name,
                  render: (r: { values: unknown[]; index: number }) => {
                    const cell = r.values[index];
                    return cell == null
                      ? "—"
                      : typeof cell === "object"
                        ? JSON.stringify(cell)
                        : String(cell);
                  },
                }))}
              />
            )}
            {response.data.truncated && <Notice>{t("truncated")}</Notice>}
          </>
        )}
      </div>
    </section>
  );
}
