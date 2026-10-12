import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Archive,
  Clock,
  Cpu,
  HardDrive,
  MemoryStick,
  Package,
  Play,
  Power,
  Radio,
  RotateCw,
  Users,
} from "lucide-react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import { useOverview } from "../components/overview";
import { useActiveServer, useServers } from "../components/ServerContext";
import { NoServer } from "../components/NoServer";
import { tb } from "../i18n";
import { GAME_LABEL } from "../lib/game";
import { memoryShare } from "../lib/memory";
import { useConfirm, useToast } from "../components/feedback";
import { useLiveConsole } from "../components/liveconsole";
import { Alert, Badge, Button, Card, GhostButton, SectionTitle, Skeleton, StatTile } from "../components/ui";
import { HistoryCharts } from "../components/HistoryCharts";
import { fmtBytes, fmtDateTime, fmtDuration, fmtGiB, fmtPct, fmtRelative } from "../lib/format";

export default function Status() {
  const { t } = useTranslation("status");
  const { can } = useAuth();
  const { data, error, refresh } = useOverview();
  const confirm = useConfirm();
  const toast = useToast();
  const { emit } = useLiveConsole();
  const active = useActiveServer();
  const { loading: serversLoading } = useServers();
  // NO fallback to a guessed key. `useActiveServer()` returns `null` as soon as the registry is
  // empty (`GET /api/servers` fails once, or `can_view_status` is missing), and with
  // `data === null` `running` is false — a fallback key would put a start button on the page
  // that acts on a server the page never named. The target of an intervening action must not
  // be guessed.
  const serverKey = active?.key ?? null;
  const [busy, setBusy] = useState("");

  // Unknown until the first overview answer: no Start button for a server that may be running.
  const known = !!data?.container;
  const running = !!data?.container?.running;

  async function action(a: "start" | "stop" | "restart") {
    if (!serverKey) {
      toast.error(t("action.noServer"));
      return;
    }
    if (a !== "start") {
      const ok = await confirm({
        title: t(`confirm.${a}.title`),
        body: t("confirm.body"),
        danger: true,
        confirmLabel: t(`confirm.${a}.label`),
      });
      if (!ok) return;
    }
    emit(t(`action.${a}.consoleLine`));
    setBusy(a);
    try {
      const res = await api(`/servers/${serverKey}/${a}`, { method: "POST" });
      if (res.ok) {
        toast.success(t(`action.${a}.done`));
        setTimeout(refresh, 2500);
      } else {
        toast.error(await errText(res, t("action.failed")));
      }
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy("");
    }
  }

  if (!active && !serversLoading) return <NoServer />;

  // The container's memory against its own limit (`lib/memory.ts`); the heap ceiling (-Xmx) is
  // a different quantity and only named.
  const mem = memoryShare(data?.stats);
  const memBytes = mem.used;
  const memPct = mem.share != null ? Math.round(mem.share * 100) : null;
  const heapMax = data?.stats?.mem_max_heap ?? null;

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={Activity}
        sub={active ? `${t("subtitle")} · ${active.display_name} (${GAME_LABEL[active.game_type]})` : t("subtitle")}
        right={
          // No lifecycle buttons without a known server — they would have no definite target.
          can("can_restart") && serverKey && known && (
            <>
              {!running && (
                <Button disabled={!!busy} onClick={() => action("start")}>
                  <Play className="h-4 w-4" /> {t("button.start")}
                </Button>
              )}
              {running && (
                <GhostButton disabled={!!busy} onClick={() => action("restart")}>
                  <RotateCw className="h-4 w-4" /> {t("button.restart")}
                </GhostButton>
              )}
              {running && (
                <GhostButton disabled={!!busy} onClick={() => action("stop")}>
                  <Power className="h-4 w-4" /> {t("button.stop")}
                </GhostButton>
              )}
            </>
          )
        }
      >
        {t("title")}
      </SectionTitle>

      {error && <Alert>{error}</Alert>}

      {data?.last_incident && (
        <Alert kind="warn">
          <span className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              {t("incident", { when: fmtRelative(data.last_incident.at, true), detail: tb(data.last_incident.detail) })}
            </span>
          </span>
        </Alert>
      )}

      {data?.mod_health && data.mod_health.updates > 0 && (
        <Alert kind="warn">
          <span className="flex items-start gap-2">
            <Package className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              <Trans
                t={t}
                i18nKey="modUpdates"
                count={data.mod_health.updates}
                components={{ l: <Link to="/mods" className="underline hover:text-fg" /> }}
              />
            </span>
          </span>
        </Alert>
      )}

      {!data ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label={t("tile.state")}
              icon={Power}
              tone={running ? "toxic" : "blood"}
              value={!known ? "…" : running ? t("common:state.online") : t("common:state.offline")}
              sub={`${data.container?.status ?? "—"}${data.container?.health ? ` · ${data.container.health}` : ""}`}
            />
            <StatTile
              label={t("common:word.players")}
              icon={Users}
              tone="toxic"
              value={`${data.players?.count ?? "—"} / ${data.max_players ?? "—"}`}
              sub={data.players?.names?.join(", ") || t("tile.nobodyOnline")}
            />
            <StatTile
              label={t("tile.ram")}
              icon={MemoryStick}
              tone={memPct && memPct > 90 ? "rust" : "bone"}
              value={memBytes ? fmtGiB(memBytes) : "—"}
              sub={[
                mem.cap ? t("tile.ramOfLimit", { percent: fmtPct(memPct), limit: fmtGiB(mem.cap) }) : memBytes ? t("tile.noLimit") : "",
                heapMax ? t("tile.heap", { heap: fmtGiB(heapMax, 0) }) : "",
              ].filter(Boolean).join(" · ")}
            />
            <StatTile
              label="CPU"
              icon={Cpu}
              value={fmtPct(data.stats?.cpu_percent)}
              sub={data.stats?.cpu_count ? t("tile.cores", { count: data.stats.cpu_count }) : ""}
            />
            <StatTile label={t("common:word.uptime")} icon={Clock} value={fmtDuration(data.uptime_seconds)} />
            <StatTile
              label="RCON"
              icon={Radio}
              tone={data.rcon?.reachable ? "toxic" : "blood"}
              value={data.rcon?.reachable ? `${data.rcon.latency_ms ?? "?"} ms` : t("tile.rconOffline")}
              sub={data.rcon?.reachable ? t("tile.reachable") : t("tile.unreachable")}
            />
            <StatTile label={t("tile.mods")} icon={Package} value={data.mods_count ?? "—"} />
            <StatTile
              label={t("tile.diskFree")}
              icon={HardDrive}
              tone={data.disk && data.disk.percent && data.disk.percent > 90 ? "rust" : "bone"}
              value={data.disk ? fmtBytes(data.disk.free) : "—"}
              sub={data.disk ? t("tile.diskUsed", { percent: fmtPct(data.disk.percent) }) : ""}
            />
          </div>

          <Card className="!p-4">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-fg-muted">
              <span className="flex items-center gap-2">
                <Archive className="h-4 w-4 text-fg-dim" />
                {t("summary.lastBackup")}{" "}
                <span className="text-fg">
                  {data.last_backup ? fmtRelative(data.last_backup.created) : t("summary.noBackup")}
                </span>
                {data.last_backup && <Badge tone="info">{fmtBytes(data.last_backup.size)}</Badge>}
              </span>
              <span className="flex items-center gap-2">
                <Clock className="h-4 w-4 text-fg-dim" />
                {t("summary.nextBackup")}{" "}
                <span className="text-fg">
                  {data.next_backup ? fmtDateTime(data.next_backup) : "—"}
                </span>
              </span>
              <span className="flex items-center gap-2">
                <RotateCw className="h-4 w-4 text-fg-dim" />
                {t("summary.nextRestart")}{" "}
                <span className="text-fg">
                  {data.next_restart ? fmtDateTime(data.next_restart) : t("summary.noRestart")}
                </span>
              </span>
            </div>
          </Card>

          <HistoryCharts />
        </>
      )}
    </div>
  );
}
