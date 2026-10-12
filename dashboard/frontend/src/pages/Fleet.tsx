// The fleet page (start page): every server, the key figures live, start and stop in place —
// and how long it takes until an empty server is switched off automatically.
//
//  1. **One request, not one per server.** `GET /api/servers/fleet` collects in parallel; a
//     per-server `/overview` costs container state, `docker stats` (~1 s per container), an RCON
//     round trip, a backup listing, a disk query and three DB queries — against ONE uvicorn worker.
//  2. **"Don't know" looks different from "nobody there".** The backend sends
//     `players_source: live | unknown | off`; a question mark is not a zero.
//  3. **The countdown is not a second calculation.** It comes from the backend's
//     `services/idle_autostop.py` — the function the hourly job takes its stop decision from.
import { useCallback, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Link, useNavigate } from "react-router-dom";
import { Cpu, HardDrive, LayoutGrid, MemoryStick, Play, Plus, Power, PowerOff, RotateCw, Users } from "lucide-react";
import { apiJson } from "../api";
import { useAuth } from "../auth";
import { Connect, ConnectLine } from "../components/ConnectAddress";
import { GameEmblem } from "../components/game/GameEmblem";
import { GameLabel } from "../components/game/GameLabel";
import { NoServer } from "../components/NoServer";
import { useServers } from "../components/ServerContext";
import { Sparkline } from "../components/Sparkline";
import { useServerActions } from "../components/ServerActions";
import {
  Alert,
  Button,
  Card,
  Column,
  cx,
  DataTable,
  GhostButton,
  SectionTitle,
  Skeleton,
} from "../components/ui";
import { gameLabel, GameType, gameSuffix } from "../lib/game";
import { fmtBytes, fmtCountdown, fmtDuration, fmtRelative } from "../lib/format";
import { newFreshnessGuard } from "../lib/freshness";
import { memoryShare } from "../lib/memory";

// Poll interval. Deliberately slower than the 3 s cache in the backend
// (`services/fleet.CACHE_TTL_S`): a single tab always gets fresh numbers, and several open tabs
// still share one Docker round.
const POLL_MS = 6000;

interface FleetServer {
  id: number | null;
  key: string;
  game_type: GameType;
  display_name: string;
  sort_order: number;
  game_version: string;
  /** Where players connect (the driver's `connect_info`); `null` if the driver cannot say. */
  connect: Connect | null;
  container: { exists?: boolean | null; running?: boolean | null; restarting?: boolean; status?: string; health?: string | null };
  uptime_seconds: number | null;
  stats: {
    cpu_percent?: number;
    mem_bytes?: number;
    mem_limit?: number;
    mem_max_heap?: number | null;
    mem_container_limit?: number | null;
  };
  spark: { cpu: number; players: number; running: boolean }[];
  players: { count: number; names: string[] } | null;
  players_source: "live" | "unknown" | "off";
  max_players: number | null;
  rcon: { reachable: boolean; latency_ms: number | null };
  idle: {
    active: boolean;
    reason: string;
    timeout_min: number;
    last_active_at: string | null;
    earliest_stop_at: string | null;
    remaining_min: number | null;
  } | null;
  next_restart: string | null;
  last_backup_at: string | null;
  error?: string;
}

interface FleetData {
  host: {
    cpu_count: number | null;
    mem_total_bytes: number | null;
    source: "docker_info" | "budget";
    disk: { free: number; total: number; used: number; percent: number | null } | null;
    budget: { total_ram_mb: number; used_ram_mb: number; total_cpu_cores: number; used_cpu_cores: number; free_ram_mb: number | null };
  };
  job: { idle_interval_min: number };
  generated_at: string;
  servers: FleetServer[];
}

export default function Fleet() {
  const { t } = useTranslation("fleet");
  const { me, can } = useAuth();
  const admin = me?.role === "admin";
  const { servers: registry, loading: registryLoading, error: registryError } = useServers();
  const nav = useNavigate();
  const [data, setData] = useState<FleetData | null>(null);
  const [error, setError] = useState("");
  // Without the guard a response that set off BEFORE the click overwrites the freshly loaded
  // state — the server would look stopped for a second although it is starting.
  const fresh = useRef(newFreshnessGuard()).current;

  const load = useCallback(async () => {
    const mark = fresh.begin();
    try {
      const d = await apiJson<FleetData>("/servers/fleet");
      if (fresh.valid(mark)) {
        setData(d);
        setError("");
      }
    } catch (e) {
      if (fresh.valid(mark)) setError((e as Error).message);
    }
  }, [fresh]);

  const afterAction = useCallback(() => {
    fresh.invalidate(); // fetches in flight have read the state BEFORE the click
    load();
  }, [fresh, load]);

  const { busy, busyKey, runAction, dialog } = useServerActions(afterAction);

  useEffect(() => {
    load();
    let timer: ReturnType<typeof setInterval> | null = null;
    // Pause in a background tab. Every round costs a Docker query and an RCON round trip per
    // server against ONE uvicorn worker — a forgotten tab must not do that forever.
    const schedule = () => {
      if (timer) clearInterval(timer);
      timer = null;
      if (document.visibilityState === "visible") {
        load();
        timer = setInterval(load, POLL_MS);
      }
    };
    schedule();
    document.addEventListener("visibilitychange", schedule);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", schedule);
    };
  }, [load]);

  if (!data && !error) return <LoadingPlaceholder />;

  const servers = data?.servers ?? [];
  const columns = buildColumns(data, t);
  // Switched off: new servers waiting for their container, and servers taken out of service.
  // The fleet endpoint lists enabled servers only, so without this list a freshly created server
  // would vanish from sight right after "Create".
  const switchedOff = registry.filter((s) => !s.enabled);
  const empty = !registryLoading && !registryError && registry.length === 0;

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={LayoutGrid}
        sub={t("subtitle")}
        right={
          admin && !empty ? (
            <Link to="/servers/new">
              <Button>
                <Plus className="h-4 w-4" /> {t("addServer")}
              </Button>
            </Link>
          ) : undefined
        }
      >
        {t("title")}
      </SectionTitle>

      {error && <Alert kind="error">{error}</Alert>}

      {empty && <NoServer />}

      {data && !empty && <LoadStrip data={data} />}

      {!empty && (
        <DataTable<FleetServer>
          columns={columns}
          rows={servers}
          rowKey={(r) => r.key}
          rowAttrs={(r) => ({ "data-game": r.game_type })}
          onRowClick={(r) => nav(`/s/${r.key}`)}
          empty={<div className="py-8 text-center text-fg-dim">{t("empty")}</div>}
          actions={
            can("can_restart")
              ? (r) => {
                  const running = r.container.running === true;
                  const stateUnknown = r.container.running == null;
                  const name = r.display_name || gameLabel(r);
                  return (
                    <>
                      {!running && (
                        <GhostButton
                          disabled={!!busy || stateUnknown}
                          disabledReason={
                            stateUnknown
                              ? t("action.startBlocked")
                              : undefined
                          }
                          onClick={() => runAction(r.key, name, "start")}
                          title={t("action.start")}
                        >
                          <Play className="h-3.5 w-3.5" />
                          <span className="sm:hidden">{t("action.startShort")}</span>
                        </GhostButton>
                      )}
                      {running && (
                        <>
                          <GhostButton
                            disabled={!!busy}
                            onClick={() => runAction(r.key, name, "restart")}
                            title={t("action.restart")}
                          >
                            <RotateCw className={cx("h-3.5 w-3.5", busyKey === r.key && "animate-spin")} />
                            <span className="sm:hidden">{t("action.restartShort")}</span>
                          </GhostButton>
                          <GhostButton
                            disabled={!!busy}
                            onClick={() => runAction(r.key, name, "stop")}
                            title={t("action.stop")}
                          >
                            <Power className="h-3.5 w-3.5" />
                            <span className="sm:hidden">{t("action.stopShort")}</span>
                          </GhostButton>
                        </>
                      )}
                    </>
                  );
                }
              : undefined
          }
        />
      )}

      {admin && switchedOff.length > 0 && (
        <Card className="space-y-2">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("off.title")}</div>
          <div className="text-xs text-fg-dim">{t("off.hint")}</div>
          <div className="flex flex-wrap gap-2">
            {switchedOff.map((s) => (
              <Link
                key={s.key}
                to={`/s/${s.key}`}
                data-game={s.game_type}
                className="inline-flex items-center gap-2 rounded-xl border border-line px-3 py-1.5 text-sm text-fg-muted transition hover:bg-surface-sunken hover:text-fg"
              >
                <PowerOff className="h-3.5 w-3.5" />
                {s.display_name || gameLabel(s)}
                <span className="font-mono text-[11px] text-fg-dim">{s.key}</span>
              </Link>
            ))}
          </div>
        </Card>
      )}

      {data && (
        <p className="text-xs text-fg-dim">
          {t("footer.refresh", { seconds: POLL_MS / 1000 })}{" "}
          {data.host.source === "budget" && (
            <Trans t={t} i18nKey="footer.budgetOnly" components={{ b: <strong /> }} />
          )}
        </p>
      )}

      {dialog}
    </div>
  );
}

// ---------------------------------------------------------------- utilisation

function LoadStrip({ data }: { data: FleetData }) {
  const { t } = useTranslation("fleet");
  const runningServers = data.servers.filter((s) => s.container.running === true);
  const cpuSum = runningServers.reduce((a, s) => a + (s.stats.cpu_percent ?? 0), 0);
  const ramSum = runningServers.reduce((a, s) => a + (s.stats.mem_bytes ?? 0), 0);
  const cores = data.host.cpu_count;
  const ramTotal = data.host.mem_total_bytes;
  const budgetRam = data.host.budget.total_ram_mb * 1024 * 1024;

  // Players: count only what is known — and say so when something is missing.
  const known = data.servers.filter((s) => s.players != null);
  const playerCount = known.reduce((a, s) => a + (s.players?.count ?? 0), 0);
  const unknownCount = data.servers.filter((s) => s.players_source === "unknown").length;

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric
        icon={Cpu}
        label={t("load.cpu")}
        value={`${Math.round(cpuSum)} %`}
        against={cores ? t("load.cpuOf", { percent: cores * 100, cores }) : t("load.cpuNoHost")}
        share={cores ? cpuSum / (cores * 100) : null}
      />
      <Metric
        icon={MemoryStick}
        label={t("load.ram")}
        value={fmtBytes(ramSum)}
        against={
          ramTotal
            ? t("load.ramOfHost", { total: fmtBytes(ramTotal) })
            : budgetRam
              ? t("load.ramOfBudget", { total: fmtBytes(budgetRam) })
              : t("load.ramNoLimit")
        }
        share={ramTotal ? ramSum / ramTotal : budgetRam ? ramSum / budgetRam : null}
      />
      <Metric
        icon={HardDrive}
        label={t("load.disk")}
        value={data.host.disk ? `${data.host.disk.percent ?? 0} %` : "—"}
        against={data.host.disk ? t("load.diskFree", { free: fmtBytes(data.host.disk.free) }) : t("load.diskUnreadable")}
        share={data.host.disk?.percent != null ? data.host.disk.percent / 100 : null}
      />
      <Metric
        icon={Users}
        label={t("load.players")}
        value={String(playerCount)}
        against={
          unknownCount
            ? t("load.runningSomeUnknown", { running: runningServers.length, total: data.servers.length, unknown: unknownCount })
            : t("load.running", { running: runningServers.length, total: data.servers.length })
        }
        share={null}
      />
    </div>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
  against,
  share,
}: {
  icon: typeof Cpu;
  label: string;
  value: string;
  against: string;
  share: number | null;
}) {
  const p = share == null ? null : Math.max(0, Math.min(1, share));
  const tone = p == null ? "bg-primary" : p > 0.9 ? "bg-danger" : p > 0.75 ? "bg-warning" : "bg-primary";
  return (
    <Card className="!p-4">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-fg-muted">{label}</div>
        <Icon className="h-4 w-4 text-fg-dim" />
      </div>
      <div className="mt-1.5 font-mono text-2xl font-semibold tabular-nums text-fg">{value}</div>
      {/* Every bar says AGAINST WHAT it measures — otherwise a percentage is just a claim. */}
      <div className="mt-1 text-xs text-fg-dim">{against}</div>
      <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-sunken">
        {p != null && <div className={cx("h-full", tone)} style={{ width: `${(p * 100).toFixed(1)}%` }} />}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- columns

function buildColumns(data: FleetData | null, t: TFunction<"fleet">): Column<FleetServer>[] {
  const interval = data?.job.idle_interval_min ?? 60;
  return [
    {
      header: t("column.server"),
      cell: (r) => (
        <div className="flex items-center gap-2.5">
          <span
            className="grid h-8 w-8 shrink-0 place-items-center border border-primary/35 bg-primary/[0.10] text-primary"
            style={{ borderRadius: "var(--g-radius)" }}
          >
            <GameEmblem game={r.game_type} className="h-5 w-5" strokeWidth={1.9} />
          </span>
          <div className="min-w-0">
            <GameLabel className="truncate text-[0.8rem] text-fg">{r.display_name || gameLabel(r)}</GameLabel>
            <div className="truncate text-[11px] text-fg-dim">
              {[gameSuffix(r), r.game_version].filter(Boolean).join(" · ") || r.key}
            </div>
            <ConnectLine connect={r.connect} />
          </div>
        </div>
      ),
    },
    {
      header: t("column.status"),
      cell: (r) => <StateCell r={r} />,
    },
    {
      header: t("common:word.players"),
      className: "text-right",
      cell: (r) => <PlayersCell r={r} />,
    },
    {
      header: t("column.load"),
      className: "text-right",
      cell: (r) => <LoadCell r={r} />,
    },
    {
      header: t("column.hour"),
      mobileHidden: true,
      cell: (r) =>
        r.spark.length > 1 ? (
          <div className="w-24 text-primary" title={t("column.hourHint")}>
            <Sparkline values={r.spark.map((p) => p.cpu)} className="h-7" />
          </div>
        ) : (
          <span className="text-xs text-fg-dim">—</span>
        ),
    },
    {
      header: t("column.autoStop"),
      cell: (r) => <AutoStopCell r={r} interval={interval} />,
    },
    {
      header: t("column.dates"),
      mobileHidden: true,
      cell: (r) => (
        <div className="space-y-0.5 text-[11px] text-fg-dim">
          <div>
            {r.next_restart ? t("dates.restart", { when: fmtRelative(r.next_restart) }) : t("dates.restartNone")}
          </div>
          <div>
            {r.last_backup_at ? t("dates.backup", { when: fmtRelative(r.last_backup_at) }) : t("dates.backupNone")}
          </div>
        </div>
      ),
    },
  ];
}

function StateCell({ r }: { r: FleetServer }) {
  const { t } = useTranslation("fleet");
  if (r.container.running == null) {
    return (
      <span className="text-sm text-warning-text" title={r.error || t("state.unknownHint")}>
        {t("state.unknown")}
      </span>
    );
  }
  const running = r.container.running;
  return (
    <div className="flex items-center gap-2">
      <span
        className={cx(
          "inline-flex h-2 w-2 shrink-0 rounded-full",
          r.container.restarting ? "bg-warning" : running ? "bg-success animate-live" : "bg-fg-dim",
        )}
      />
      <div className="min-w-0">
        <div className={cx("text-sm", running ? "text-success-text" : "text-fg-muted")}>
          {r.container.restarting ? t("state.restarting") : running ? t("common:state.running") : t("common:state.stopped")}
        </div>
        {running && r.uptime_seconds != null && (
          <div className="text-[11px] text-fg-dim">{t("state.since", { duration: fmtDuration(r.uptime_seconds) })}</div>
        )}
      </div>
    </div>
  );
}

function PlayersCell({ r }: { r: FleetServer }) {
  const { t } = useTranslation("fleet");
  // Three states, three renderings. A "0" for a server we know nothing about would launder
  // exactly the uncertainty the backend keeps apart here.
  if (r.players_source === "off") return <span className="text-fg-dim">—</span>;
  if (r.players_source === "unknown" || !r.players) {
    return (
      <span
        className="cursor-help font-mono text-warning-text"
        title={t("players.unknownHint")}
      >
        ?
      </span>
    );
  }
  return (
    <span className="font-mono tabular-nums text-fg">
      {r.players.count}
      {r.max_players ? <span className="text-fg-dim">/{r.max_players}</span> : null}
    </span>
  );
}

function LoadCell({ r }: { r: FleetServer }) {
  if (r.container.running !== true) return <span className="text-fg-dim">—</span>;
  const cpu = r.stats.cpu_percent ?? 0;
  // The container's memory against its own limit; no limit, no share (`lib/memory.ts`).
  const mem = memoryShare(r.stats);
  return (
    <div className="space-y-1">
      <Bar label="CPU" text={`${Math.round(cpu)} %`} share={cpu / 100} />
      <Bar
        label="RAM"
        text={mem.cap ? `${fmtBytes(mem.used)} / ${fmtBytes(mem.cap)}` : fmtBytes(mem.used)}
        share={mem.share}
      />
    </div>
  );
}

function Bar({ label, text, share }: { label: string; text: string; share: number | null }) {
  const p = share == null ? null : Math.max(0, Math.min(1, share));
  const tone = p == null ? "bg-fg-dim" : p > 0.9 ? "bg-danger" : p > 0.75 ? "bg-warning" : "bg-primary";
  return (
    <div className="flex items-center justify-end gap-2">
      <span className="text-[10px] uppercase tracking-wide text-fg-dim">{label}</span>
      <span className="w-14 overflow-hidden rounded-full bg-surface-sunken" style={{ height: 3 }}>
        {p != null && <span className={cx("block h-full", tone)} style={{ width: `${(p * 100).toFixed(1)}%` }} />}
      </span>
      <span className="w-28 text-right font-mono text-[11px] tabular-nums text-fg-muted">{text}</span>
    </div>
  );
}

/** The column this page is about: when does this server shut down by itself for lack of players? */
function AutoStopCell({ r, interval }: { r: FleetServer; interval: number }) {
  const { t } = useTranslation("fleet");
  const i = r.idle;
  if (!i) return <span className="text-xs text-fg-dim">—</span>;

  // The states come 1:1 from the backend (`services/idle_autostop.py`) — the function the hourly job uses
  // for its decision. This only translates, it does not calculate.
  const texts: Record<string, { text: string; tone: string; hint: string }> = {
    off: { text: t("idle.off"), tone: "text-fg-dim", hint: t("idle.offHint") },
    no_timeout: { text: t("idle.off"), tone: "text-fg-dim", hint: t("idle.noTimeoutHint") },
    not_running: { text: t("idle.notRunning"), tone: "text-fg-dim", hint: t("idle.notRunningHint") },
    never_seen: { text: t("idle.neverSeen"), tone: "text-fg-muted", hint: t("idle.neverSeenHint") },
    grace_period: {
      text: t("idle.grace", { remaining: fmtCountdown(i.remaining_min) }),
      tone: "text-fg-muted",
      hint: t("idle.graceHint"),
    },
    counting: {
      text: t("idle.counting", { remaining: fmtCountdown(i.remaining_min) }),
      tone: "text-fg-muted",
      hint: t("idle.countingHint", { days: Math.round(i.timeout_min / 1440), interval }),
    },
    due: { text: t("idle.due"), tone: "text-warning-text", hint: t("idle.dueHint") },
  };
  const z = texts[i.reason] ?? { text: i.reason, tone: "text-fg-dim", hint: "" };

  // With players online the clock stands still — even though the deadline keeps running on paper.
  const playersPresent = (r.players?.count ?? 0) > 0;
  if (playersPresent && (i.reason === "counting" || i.reason === "due")) {
    return (
      <span className="cursor-help text-xs text-fg-muted" title={t("idle.playersOnlineHint")}>
        {t("idle.playersOnline")}
      </span>
    );
  }
  return (
    <span className={cx("cursor-help text-xs", z.tone)} title={z.hint}>
      {z.text}
    </span>
  );
}

function LoadingPlaceholder() {
  return (
    <div className="space-y-5">
      <Skeleton className="h-10 w-64" />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}
