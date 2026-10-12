// The preflight's list of findings — every detected crash cause in plain words.
//
// Important: rendering goes by the **stable codes** from the backend, never by substrings of
// the message — matching on text would break silently with every change of wording.
import { useTranslation } from "react-i18next";
import { AlertTriangle, ShieldAlert } from "lucide-react";
import { Card, cx } from "./ui";
import { tb } from "../i18n";

export interface Finding {
  code: string;
  level: "blocker" | "warning";
  message: string;
  subject?: string;
  detail?: string;
  fix?: string;
}

export interface PreflightData {
  ok: boolean;
  blockers: Finding[];
  warnings: Finding[];
  world_reset_files?: number;
}

// A short addition per code — explains WHY it is dangerous, not only THAT. Keys, translated
// when rendering.
const WHY: Record<string, string> = {
  MAP_CELL_CONFLICT: "why.MAP_CELL_CONFLICT",
  MAP_WITHOUT_PROVIDER: "why.MAP_WITHOUT_PROVIDER",
  ORPHAN_MAP_DOWNLOAD: "why.ORPHAN_MAP_DOWNLOAD",
  WORLD_MAP_REMOVED: "why.WORLD_MAP_REMOVED",
  BASE_MAP_NOT_LAST: "why.BASE_MAP_NOT_LAST",
  BASE_MAP_MISSING: "why.BASE_MAP_MISSING",
  MISSING_DEPENDENCY: "why.MISSING_DEPENDENCY",
  BUILD41_ONLY: "why.BUILD41_ONLY",
  WORKSHOP_DIR_UNREADABLE: "why.WORKSHOP_DIR_UNREADABLE",
};

function Row({ f }: { f: Finding }) {
  const { t } = useTranslation("modFindings");
  const blocker = f.level === "blocker";
  return (
    <div
      className={cx(
        "rounded-xl border p-3 text-sm",
        blocker
          ? "border-danger-border bg-danger-soft text-danger-text"
          : "border-warning/40 bg-warning/10 text-warning-text",
      )}
    >
      <div className="flex items-start gap-2">
        {blocker ? (
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
        ) : (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        )}
        <div className="min-w-0">
          <div className="font-medium">{tb(f.message)}</div>
          {f.detail && <div className="mt-0.5 text-xs opacity-90">{tb(f.detail)}</div>}
          {WHY[f.code] && <div className="mt-1 text-xs opacity-75">{t(WHY[f.code])}</div>}
          <div className="mt-1 font-mono text-[10px] uppercase tracking-wide opacity-60">
            {f.code}
            {f.subject ? ` · ${f.subject}` : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

export function ModFindings({ data }: { data: PreflightData | null }) {
  const { t } = useTranslation("modFindings");
  if (!data) return null;
  const all = [...data.blockers, ...data.warnings];

  return (
    <Card className="space-y-3">
      <div className="text-xs uppercase tracking-wide text-fg-muted">{t("heading")}</div>

      {all.length === 0 ? (
        <div className="text-sm text-fg-muted">
          {t("none")}
        </div>
      ) : (
        <div className="space-y-2">
          {all.map((f, i) => (
            <Row key={`${f.code}-${f.subject ?? i}`} f={f} />
          ))}
        </div>
      )}

      {!data.ok && (
        <div className="text-xs text-fg-dim">
          {t("locked")}
        </div>
      )}
      {data.ok && !!data.world_reset_files && (
        <div className="text-xs text-fg-muted">
          {t("worldReset", { count: data.world_reset_files })}
        </div>
      )}
    </Card>
  );
}
