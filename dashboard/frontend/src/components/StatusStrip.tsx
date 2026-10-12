import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Clock, Cpu, HardDrive, MemoryStick, Radio, Ticket, UserPlus, Users } from "lucide-react";
import { cx } from "./ui";
import { ModHealthChip } from "./ModHealth";
import { useOverview } from "./overview";
import { useActiveServer } from "./ServerContext";
import { fmtBytes, fmtDuration, fmtGiB, fmtPct } from "../lib/format";
import { memoryShare } from "../lib/memory";

function Item({ icon: Icon, label, value, tone = "bone" }: any) {
  const cls = { bone: "text-fg", toxic: "text-success-text", blood: "text-danger-text", rust: "text-warning-text" }[
    tone as string
  ];
  return (
    <span className="flex items-center gap-1.5 whitespace-nowrap" title={label}>
      <Icon className="h-3.5 w-3.5 text-fg-dim" />
      <span className={cx("font-mono", cls)}>{value}</span>
    </span>
  );
}

export function StatusStrip() {
  const { t } = useTranslation(["statusStrip", "common"]);
  const { data } = useOverview();
  const active = useActiveServer();
  const running = !!data?.container?.running;
  // No enabled server: there is no state to show, and "…" would wait for an answer that never comes.
  if (!active) return null;

  return (
    <div className="flex flex-nowrap items-center gap-x-4 whitespace-nowrap py-1 text-xs text-fg-muted">
      <span className="flex items-center gap-1.5 whitespace-nowrap">
        <span
          className={cx(
            "h-2.5 w-2.5 rounded-full",
            running ? "bg-success animate-live" : "bg-fg-dim",
          )}
        />
        <span className={cx("font-semibold uppercase tracking-wide", running ? "text-success-text" : "text-danger-text")}>
          {data ? (running ? t("common:state.online") : t("common:state.offline")) : "…"}
        </span>
      </span>

      {data && running && (
        <>
          <Item
            icon={Users}
            label={t("common:word.players")}
            value={`${data.players?.count ?? "—"}/${data.max_players ?? "—"}`}
            tone="toxic"
          />
          {/* Detail values from `sm` up only — on a phone, state and player count are what matters. */}
          <span className="hidden items-center gap-x-4 sm:flex">
            {(() => {
              const mem = memoryShare(data.stats);
              const pct = mem.share != null ? Math.round(mem.share * 100) : null;
              return (
                <Item
                  icon={MemoryStick}
                  label="RAM"
                  value={pct != null ? `${fmtPct(pct)} · ${fmtGiB(mem.used)}` : fmtGiB(mem.used)}
                  tone={pct && pct > 90 ? "rust" : "bone"}
                />
              );
            })()}
            <Item icon={Cpu} label="CPU" value={fmtPct(data.stats?.cpu_percent)} />
            <Item icon={Clock} label={t("common:word.uptime")} value={fmtDuration(data.uptime_seconds)} />
            <Item
              icon={Radio}
              label="RCON"
              value={data.rcon?.reachable ? `${data.rcon.latency_ms ?? "?"} ms` : t("rcon.offline")}
              tone={data.rcon?.reachable ? "bone" : "blood"}
            />
            {data.disk && <Item icon={HardDrive} label={t("diskFree")} value={fmtBytes(data.disk.free)} />}
          </span>
        </>
      )}

      {/* Mod health light: always visible, so that a broken configuration is not noticed only
          at the next restart. A click leads straight to the mods page. */}
      <ModHealthChip />

      {/* Admin chips: clickable */}
      {!!data?.pending_registrations && (
        <Link
          to="/users"
          className="flex items-center gap-1 rounded border border-warning/50 bg-warning/10 px-1.5 py-0.5 text-warning-text hover:bg-warning/20"
          title={t("pendingRegistrations")}
        >
          <UserPlus className="h-3.5 w-3.5" /> {data.pending_registrations}
        </Link>
      )}
      {!!data?.open_tickets && (
        <Link
          to="/tickets"
          className="flex items-center gap-1 rounded border border-line-strong bg-surface-sunken px-1.5 py-0.5 hover:bg-line"
          title={t("openTickets")}
        >
          <Ticket className="h-3.5 w-3.5" /> {data.open_tickets}
        </Link>
      )}
    </div>
  );
}
