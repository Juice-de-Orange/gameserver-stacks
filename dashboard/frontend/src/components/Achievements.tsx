import { useTranslation } from "react-i18next";
import { Award, Lock } from "lucide-react";
import { tb } from "../i18n";
import { cx } from "./ui";

export interface Achievement {
  key: string;
  group: string;
  label: string;
  desc: string;
  achieved: boolean;
  value: number;
  target: number;
  progress: number;
}

/** Milestone grid: achieved ones highlighted, open ones dimmed with a progress bar. */
export function Achievements({ items }: { items?: Achievement[] }) {
  const { t } = useTranslation("achievements");
  if (!items || items.length === 0) return null;
  const earned = items.filter((a) => a.achieved).length;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
        <Award className="h-4 w-4 text-warning-text" /> {t("title", { earned, total: items.length })}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {items.map((a) => (
          <div
            key={a.key}
            className={cx(
              "rounded-md border p-2.5",
              a.achieved ? "border-success-border bg-success-soft/40" : "border-line-soft bg-surface-card/40 opacity-80",
            )}
          >
            <div className="flex items-center gap-2">
              {a.achieved ? (
                <Award className="h-4 w-4 shrink-0 text-success-text" />
              ) : (
                <Lock className="h-3.5 w-3.5 shrink-0 text-fg-dim" />
              )}
              <span className={cx("truncate text-sm font-semibold", a.achieved ? "text-fg" : "text-fg-muted")}>
                {tb(a.label)}
              </span>
            </div>
            <div className="mt-0.5 text-xs text-fg-dim">{tb(a.desc)}</div>
            {!a.achieved && (
              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-sunken">
                <div className="h-full bg-success/60" style={{ width: `${Math.round((a.progress || 0) * 100)}%` }} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
