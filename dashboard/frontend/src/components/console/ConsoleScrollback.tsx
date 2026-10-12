import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../ui";
import type { DockLine, LineSource } from "../liveconsole";

// Colour per line source — server activity stays subtle, one's own commands and actions stand out.
const SRC_CLS: Record<LineSource, string> = {
  log: "text-fg-dim",
  "cmd-echo": "text-success-text",
  "cmd-out": "text-fg",
  "cmd-info": "text-fg-muted",
  "cmd-err": "text-red-200",
  action: "text-warning-text",
};

function hhmmss(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * Scrollable history of the merged console lines. Auto-scrolls only while the user is "pinned"
 * to the bottom — whoever scrolls up is not dragged away. `compact` fills the available flex
 * space (dock); otherwise a fixed height (full page). Without `empty` the default hint is used,
 * in the active language.
 */
export function ConsoleScrollback({
  lines,
  compact = false,
  empty,
}: {
  lines: DockLine[];
  compact?: boolean;
  empty?: string;
}) {
  const { t } = useTranslation("consoleScrollback");
  const boxRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const el = boxRef.current;
    if (el && pinned.current) el.scrollTo(0, el.scrollHeight);
  }, [lines]);

  function onScroll() {
    const el = boxRef.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  return (
    <div
      ref={boxRef}
      onScroll={onScroll}
      className={cx(
        "overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed",
        compact ? "min-h-0 flex-1" : "h-[55vh] sm:h-[26rem]",
      )}
    >
      {lines.length === 0 ? (
        <span className="text-fg-dim">{empty ?? t("empty")}</span>
      ) : (
        lines.map((l) => (
          <div key={l.id} className="border-b border-surface-card/40 py-0.5">
            <span className="select-none text-fg-dim">[{hhmmss(l.ts)}] </span>
            <span className={SRC_CLS[l.source]}>{l.text}</span>
          </div>
        ))
      )}
    </div>
  );
}
