import { useTranslation } from "react-i18next";
import { Eraser, Terminal, X } from "lucide-react";
import { Badge } from "../ui";
import { useLiveConsole } from "../liveconsole";
import { useServers } from "../ServerContext";
import { ConsoleScrollback } from "./ConsoleScrollback";
import { ConsoleInput } from "./ConsoleInput";

/** Shared content of the desktop column and the mobile sheet. */
function DockBody({ onClose }: { onClose?: () => void }) {
  const { t } = useTranslation("liveDock");
  const { lines, send, clear, history, canLog, canCmd, logConnected, cmdConnected, serverKey, serverLabel } =
    useLiveConsole();
  const { loading: serversLoading, error: serversError, reload: reloadServers } = useServers();
  const emptyHint = !serverKey
    ? serversLoading
      ? t("hint.loading")
      : serversError
        ? t("hint.failed")
        : t("hint.none")
    : canLog
      ? t("hint.waitingLog")
      : t("hint.ready");
  return (
    <>
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-line-soft bg-surface-raised/80 px-3 py-2">
        {/* The server name belongs here: log and commands go to the ACTIVE server, and a
            command sent to the wrong server cannot be taken back. That is why the name gets all
            the space and the words "live console" are not shown: the desktop column is 22rem
            narrow. The icon plus the "Log"/"RCON" badges say the same.

            Below `sm` the KEY is shown instead of the display name. Reason: in a 320 px sheet
            about 90 px remain after the badges, and `truncate` cuts at the end — "Project
            Zomboid" and "Project Zomboid (Modded)" would both appear as "Project Zom…", losing
            exactly the distinction that matters. The keys are short and unique, and a tooltip
            does not help on touch. */}
        <div
          className="flex min-w-0 items-center gap-2 text-sm font-semibold text-fg"
          role="group"
          aria-label={t("title")}
        >
          <Terminal className="h-4 w-4 shrink-0 text-success-text" />
          <span className="truncate sm:hidden" title={serverLabel}>
            {serverKey || "—"}
          </span>
          <span className="hidden truncate sm:inline" title={serverLabel}>
            {serverLabel}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {canLog && <Badge tone={logConnected ? "online" : "blood"}>Log</Badge>}
          {canCmd && <Badge tone={cmdConnected ? "online" : "blood"}>RCON</Badge>}
          <button
            onClick={clear}
            title={t("clear")}
            aria-label={t("clear")}
            className="flex h-8 w-8 items-center justify-center text-fg-dim hover:text-fg"
          >
            <Eraser className="h-4 w-4" />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              title={t("close")}
              aria-label={t("close")}
              className="flex h-8 w-8 items-center justify-center text-fg-dim hover:text-fg"
            >
              <X className="h-5 w-5" />
            </button>
          )}
        </div>
      </div>
      {/* Without a server the dock deliberately does not connect at all (see liveconsole.tsx).
          Then it has to say so instead of showing "waiting for server activity" and silently
          doing nothing — and it has to tell "still loading" from "failed". The failure needs a
          way out: `reload()` otherwise only runs on sign-in, so a single 500 on `/api/servers`
          would silence the dock for the whole session. */}
      <ConsoleScrollback lines={lines} compact empty={emptyHint} />
      {!serverKey && serversError && (
        <div className="shrink-0 border-t border-line-soft px-3 py-2 text-center">
          <button onClick={() => void reloadServers()} className="text-xs text-success-text underline">
            {t("reloadServers")}
          </button>
        </div>
      )}
      <div className="shrink-0 border-t border-line-soft p-2">
        <ConsoleInput onSend={send} disabled={!canCmd || !serverKey} history={history} />
      </div>
    </>
  );
}

/**
 * Dockable live console. Desktop: a sticky right column (a flex column of its own next to the
 * main page). Mobile: a sheet from the bottom above the tab bar. Renders nothing while closed.
 */
export function LiveDock() {
  const { dockOpen, setDockOpen, canLog, canCmd } = useLiveConsole();
  // Never render without any console permission (not even with a stale localStorage flag).
  if (!dockOpen || (!canLog && !canCmd)) return null;
  return (
    <>
      {/* Desktop: right column (full height, sticky like the sidebar) */}
      <aside className="sticky top-0 hidden h-screen w-[22rem] shrink-0 flex-col border-l border-line-soft bg-surface-raised/80 lg:flex xl:w-[26rem]">
        <DockBody />
      </aside>

      {/* Mobile: a sheet from the bottom, sitting above the BottomNav (~3.7rem + safe area).
          z-50: above the tab bar (z-40), below modals (z-60) and toasts (z-70). */}
      <div className="animate-sheet fixed inset-x-0 bottom-[calc(3.7rem+env(safe-area-inset-bottom))] z-50 flex max-h-[60vh] flex-col border-t border-line bg-surface-raised shadow-panel lg:hidden">
        <DockBody onClose={() => setDockOpen(false)} />
      </div>
    </>
  );
}
