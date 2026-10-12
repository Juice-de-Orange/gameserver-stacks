import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ClipboardList } from "lucide-react";
import { apiJson } from "../api";
import { Alert, DataTable, EmptyState, SectionTitle } from "../components/ui";
import { fmtDateTime } from "../lib/format";

interface AuditRow {
  id: number;
  created_at: string;
  username: string;
  action: string;
  target: string;
  payload: string;
}

export default function Audit() {
  const { t } = useTranslation("audit");
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    apiJson("/audit")
      .then((r: any) => setRows(r))
      .catch((e) => setErr(e.message));
  }, []);

  return (
    <div className="space-y-4">
      <SectionTitle icon={ClipboardList} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}
      <DataTable<AuditRow>
        rows={rows}
        rowKey={(r) => r.id}
        empty={<EmptyState icon={ClipboardList}>{t("empty")}</EmptyState>}
        columns={[
          {
            header: t("column.time"),
            cell: (r) => fmtDateTime(r.created_at, true),
            className: "whitespace-nowrap text-fg-dim",
          },
          { header: t("column.user"), cell: (r) => <span className="text-fg">{r.username}</span> },
          { header: t("column.action"), cell: (r) => <span className="font-mono text-xs text-success-text">{r.action}</span> },
          { header: t("column.target"), cell: (r) => <span className="text-fg-muted">{r.target}</span> },
          {
            header: t("column.detail"),
            cell: (r) => <span title={r.payload}>{r.payload}</span>,
            className: "max-w-[20rem] truncate text-xs text-fg-dim",
          },
        ]}
      />
    </div>
  );
}
