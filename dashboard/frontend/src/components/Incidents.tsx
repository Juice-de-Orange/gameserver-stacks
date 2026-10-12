// Incidents of a server — boot loops, refused restarts, watchdog interventions.
//
// A banner only covers the last 24 hours. A crash loop from the day before yesterday or a series
// of blocked restarts is exactly the trail one looks for after an outage, hence the full list.
import { useTranslation } from "react-i18next";
import { AlertTriangle, Ban, CheckCircle2, Moon, RefreshCw, RotateCcw } from "lucide-react";
import { usePolledData } from "../hooks/usePolledData";
import { Card, EmptyState, cx } from "./ui";
import { fmtDateTime, fmtRelative } from "../lib/format";
import { tb } from "../i18n";

interface Incident {
  id: number;
  at: string;
  kind: string;
  detail: string;
}

// Known incident kinds: weight and icon. The label is the catalog key `kind.<kind>` (namespace
// `incidents`), translated when rendering. Unknown kinds are shown neutrally, not swallowed.
const KIND: Record<string, { tone: "bad" | "warn" | "good" | "info"; Icon: typeof AlertTriangle }> = {
  boot_crash_loop: { tone: "bad", Icon: RefreshCw },
  restart_blocked: { tone: "warn", Icon: Ban },
  rcon_hang: { tone: "warn", Icon: AlertTriangle },
  auto_restart: { tone: "warn", Icon: RotateCcw },
  restart_failed: { tone: "bad", Icon: AlertTriangle },
  recovered: { tone: "good", Icon: CheckCircle2 },
  idle_autostop: { tone: "info", Icon: Moon },
  // The mandatory backup before a scheduled restart failed, and the restart therefore did NOT
  // happen. "bad", because both together weigh more than a refused restart — the way back is
  // missing AND the server is still on the old state.
  backup_failed: { tone: "bad", Icon: AlertTriangle },
  backup_quiesce_failed: { tone: "warn", Icon: AlertTriangle },
};

const TONE = {
  bad: "border-danger-border bg-danger-soft text-danger-text",
  warn: "border-warning/40 bg-warning/10 text-warning-text",
  good: "border-success/40 bg-success-soft text-success-text",
  info: "border-line bg-surface-sunken text-fg-muted",
};

export function IncidentList({ serverKey, limit = 25 }: { serverKey: string; limit?: number }) {
  const { t } = useTranslation("incidents");
  const { data } = usePolledData<Incident[]>(
    serverKey ? `/servers/${serverKey}/incidents?limit=${limit}` : null,
    120000,
  );

  return (
    <Card className="space-y-2">
      <div className="text-xs uppercase tracking-wide text-fg-muted">{t("title")}</div>
      {!data || data.length === 0 ? (
        <EmptyState icon={CheckCircle2}>{t("empty")}</EmptyState>
      ) : (
        <div className="space-y-1.5">
          {data.map((i) => {
            const known = KIND[i.kind];
            const meta = known ?? { tone: "info" as const, Icon: AlertTriangle };
            const label = known ? t(`kind.${i.kind}`) : i.kind;
            const { Icon } = meta;
            return (
              <div key={i.id} className={cx("rounded-xl border p-2.5 text-sm", TONE[meta.tone])}>
                <div className="flex items-start gap-2">
                  <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="min-w-0">
                    <div className="font-medium">
                      {label}
                      <span className="ml-2 text-xs font-normal opacity-70">
                        {fmtRelative(i.at, true)} · {fmtDateTime(i.at, true)}
                      </span>
                    </div>
                    {i.detail && <div className="mt-0.5 break-words opacity-90">{tb(i.detail)}</div>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
