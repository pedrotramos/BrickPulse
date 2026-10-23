import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Group } from "../lib/types";
import { money } from "../lib/format";
import { Badge, ScopedLink, Table } from "./ui";
export default function ResourceTable({ items }: { items: Group[] }) {
  const { t, i18n } = useTranslation();
  return (
    <Table
      rows={items}
      rowKey={(r) => r.group_id}
      caption={t("opportunities")}
      columns={[
        {
          key: "resource",
          label: t("resource"),
          render: (r) => (
            <>
              <ScopedLink
                className="resource-link"
                to={`/resources/${r.group_id}`}
              >
                {r.resource_name}
                <ChevronRight size={14} aria-hidden="true" />
              </ScopedLink>
              <span className="cell-sub">
                {r.workspace_name ?? r.workspace_id} ·{" "}
                {t(`resourceType.${r.resource_type}`, {
                  defaultValue: r.resource_type,
                })}
              </span>
            </>
          ),
        },
        {
          key: "action",
          label: t("action"),
          render: (r) => <span className="action-label">{r.action_label}</span>,
        },
        {
          key: "priority",
          label: t("priority"),
          render: (r) => <Badge value={r.priority} />,
        },
        { key: "effort", label: t("effort"), render: (r) => t(r.effort_band) },
        {
          key: "savings",
          label: t("monthlyOpportunity"),
          numeric: true,
          render: (r) => (
            <span className="money opportunity">
              {money(r.monthly_savings_usd, i18n.language)}
            </span>
          ),
        },
      ]}
    />
  );
}
