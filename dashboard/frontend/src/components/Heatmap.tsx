import { useTranslation } from "react-i18next";
import { currentLanguage } from "../i18n";
import { fmtHm } from "../lib/format";

/** Short weekday names, Monday first, in the active language. Computed when rendering: a list at
 *  module level would freeze the language at import. 2024-01-01 was a Monday. */
function weekdays(): string[] {
  const format = new Intl.DateTimeFormat(currentLanguage(), { weekday: "short", timeZone: "UTC" });
  return Array.from({ length: 7 }, (_, i) => format.format(new Date(Date.UTC(2024, 0, 1 + i))));
}

/** 7×24 activity heatmap (weekday × hour). Shared by the stats pages. */
export function Heatmap({ grid }: { grid: number[][] }) {
  useTranslation(); // re-render when the language changes
  const WD = weekdays();
  const max = Math.max(1, ...grid.flat());
  return (
    // -mx/px: the map may scroll edge to edge; the cells are tinted for the server via --primary.
    <div className="-mx-1 overflow-x-auto px-1 pb-1">
      <div className="inline-flex min-w-max flex-col gap-0.5">
        <div className="flex gap-0.5 pl-7">
          {Array.from({ length: 24 }).map((_, h) => (
            <div key={h} className="w-3.5 text-center text-[8px] text-fg-dim">
              {h % 6 === 0 ? h : ""}
            </div>
          ))}
        </div>
        {grid.map((row, wd) => (
          <div key={wd} className="flex items-center gap-0.5">
            <div className="w-7 text-[10px] font-medium text-fg-dim">{WD[wd]}</div>
            {row.map((v, h) => {
              const a = v === 0 ? 0 : 0.14 + 0.86 * (v / max);
              return (
                <div
                  key={h}
                  className="h-3.5 w-3.5 rounded-[3px]"
                  style={{ background: v === 0 ? "rgb(var(--ink-700) / 0.6)" : `rgb(var(--primary) / ${a})` }}
                  title={`${WD[wd]} ${h}:00 — ${fmtHm(v)}`}
                />
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
