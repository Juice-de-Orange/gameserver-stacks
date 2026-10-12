// The config traffic light: shows at any time whether the active server's mod configuration
// holds.
//
// Deliberately small and always visible (status bar) — the question "is my mod configuration
// still working?" should be answered without opening a page first.
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, PencilLine, ShieldAlert } from "lucide-react";
import { usePolledData } from "../hooks/usePolledData";
import { useActiveServerKey } from "./ServerContext";
import { cx } from "./ui";
import { tb } from "../i18n";

export interface ModStatus {
  level: "ok" | "warn" | "blocked" | "none";
  headline: string;
  blockers: number;
  warnings: number;
  mods: number;
  maps: number;
  has_pending_changes: boolean;
  pending_summary: string;
  /** Errors from the Workshop check (network) — escalate the light like a blocker. */
  steam_errors?: number;
  /** Was the Workshop check complete? `false` means "not checked", not "fine". */
  steam_ok?: boolean;
}

export function useModStatus(intervalMs = 60000) {
  const key = useActiveServerKey();
  return usePolledData<ModStatus>(key ? `/servers/${key}/mods/status` : null, intervalMs);
}

// `label` holds a key; it is translated when rendering.
const TONE: Record<string, { cls: string; Icon: typeof CheckCircle2; label: string }> = {
  ok: {
    cls: "border-success/40 bg-success-soft text-success-text",
    Icon: CheckCircle2,
    label: "chip.ok",
  },
  warn: {
    cls: "border-warning/40 bg-warning/10 text-warning-text",
    Icon: AlertTriangle,
    label: "chip.warn",
  },
  blocked: {
    cls: "border-danger-border bg-danger-soft text-danger-text",
    Icon: ShieldAlert,
    label: "chip.blocked",
  },
};

/** Compact chip for the status bar. */
export function ModHealthChip() {
  const { t } = useTranslation("modHealth");
  const { data } = useModStatus();
  if (!data || data.level === "none") return null;

  const tone = TONE[data.level] ?? TONE.ok;
  const { Icon } = tone;
  const count = data.level === "blocked" ? data.blockers : data.level === "warn" ? data.warnings : 0;

  return (
    <Link
      to="/mods"
      className={cx("flex items-center gap-1 rounded border px-1.5 py-0.5", tone.cls)}
      title={tb(data.headline) || t(tone.label)}
    >
      <Icon className="h-3.5 w-3.5" />
      <span className="font-semibold">{t(tone.label)}</span>
      {count > 0 && <span className="font-mono">{count}</span>}
      {data.has_pending_changes && (
        <PencilLine className="h-3.5 w-3.5 opacity-80" aria-label={t("chip.pending")} />
      )}
    </Link>
  );
}

/** Detailed card for the mods page. */
export function ModHealthPanel() {
  const { t } = useTranslation("modHealth");
  const { data } = useModStatus(30000);
  if (!data || data.level === "none") return null;

  const tone = TONE[data.level] ?? TONE.ok;
  const { Icon } = tone;

  return (
    <div className={cx("rounded-2xl border p-4", tone.cls)}>
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 h-5 w-5 shrink-0" />
        <div className="min-w-0">
          <div className="font-semibold">
            {data.level === "ok"
              ? t("panel.ok")
              : data.level === "warn"
                ? t("panel.warn", { count: data.warnings })
                : t("panel.blocked", { count: data.blockers })}
          </div>
          {data.headline && data.level !== "ok" && (
            <div className="mt-0.5 text-sm opacity-90">{tb(data.headline)}</div>
          )}
          <div className="mt-1 text-xs opacity-80">
            {data.has_pending_changes
              ? t("panel.activePending", { mods: data.mods, maps: data.maps, summary: tb(data.pending_summary) })
              : t("panel.active", { mods: data.mods, maps: data.maps })}
          </div>
        </div>
      </div>
    </div>
  );
}
