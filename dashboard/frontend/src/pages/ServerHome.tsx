// Overview of one server: status/players/controls through the /api/servers/:key/* endpoints.
// The active server comes from the route context.
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";
import { Pencil, Play, Power, RotateCw, Sliders, Tag, Terminal } from "lucide-react";
import { useAuth } from "../auth";
import { useActiveServer, useServers } from "../components/ServerContext";
import { NoServer } from "../components/NoServer";
import { ServerSetup } from "../components/ServerSetup";
import { ConnectCard } from "../components/ConnectAddress";
import { FirewallServerStatus } from "../components/Firewall";
import { ReachabilityPanel } from "../components/ReachabilityPanel";
import { GameHero } from "../components/game/GameHero";
import { ServerAction, useServerActions } from "../components/ServerActions";
import { Alert, Badge, Button, Card, GhostButton } from "../components/ui";
import { IncidentList } from "../components/Incidents";
import { useOverview } from "../components/overview";
import { hasCap } from "../lib/game";

export default function ServerHome() {
  // A switched-off server is never the ACTIVE one (`ServerContext`), but its own page still has
  // to open: that is where a new server shows its next steps.
  const { serverKey } = useParams();
  const { servers, loading } = useServers();
  const row = serverKey ? servers.find((s) => s.key === serverKey) : undefined;
  if (row && !row.enabled) return <ServerSetup server={row} />;
  if (!loading && !servers.some((s) => s.enabled)) return <NoServer />;
  return <ServerOverview />;
}

function ServerOverview() {
  const { t } = useTranslation("serverHome");
  const server = useActiveServer();
  const { can, me } = useAuth();

  /**
   * NO poller of its own.
   *
   * `OverviewProvider` wraps the whole app in `App.tsx` and polls exactly this URL every 12 s,
   * following the same active server. One call costs (`routers/server.py`) a Docker inspect,
   * Docker stats, a FULL RCON round trip to the running game server, an `.ini` read, a backup
   * listing with a `stat` per archive, `shutil.disk_usage` and several DB queries — against a
   * backend with exactly ONE uvicorn worker. A second poller here would double that.
   */
  const { data, error: err, refresh } = useOverview();

  // Start/stop/restart including the budget-conflict dialog come from `useServerActions` — the
  // same chain the fleet page uses. A second copy here would drift apart from it.
  const { busy, runAction, dialog } = useServerActions(refresh);

  if (!server) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;

  // Until the first overview answer the state is unknown: no "Stopped" and no Start button for a
  // server that may well be running.
  const known = !!data?.container;
  const running = !!data?.container?.running;
  const cpu = data?.stats?.cpu_percent ?? 0;
  const action = (a: ServerAction) =>
    runAction(server.key, server.display_name || server.key, a);

  return (
    <div className="space-y-5">
      {err && <Alert kind="error">{err}</Alert>}

      <GameHero
        server={server}
        stats={{
          running: known ? running : null,
          players: data?.players ?? null,
          maxPlayers: data?.max_players ?? null,
          uptimeSeconds: data?.uptime_seconds ?? null,
          cpuPercent: cpu,
        }}
      />

      <ConnectCard connect={data?.connect} />
      <FirewallServerStatus serverKey={server.key} />

      {can("can_restart") && known && (
        <div className="flex flex-wrap gap-2">
          {!running && (
            <Button disabled={!!busy} onClick={() => action("start")}>
              <Play className="h-4 w-4" /> {t("action.start")}
            </Button>
          )}
          {running && (
            <GhostButton disabled={!!busy} onClick={() => action("restart")}>
              <RotateCw className="h-4 w-4" /> {t("action.restart")}
            </GhostButton>
          )}
          {running && (
            <GhostButton disabled={!!busy} onClick={() => action("stop")}>
              <Power className="h-4 w-4" /> {t("action.stop")}
            </GhostButton>
          )}
        </div>
      )}

      {data?.players?.names?.length ? (
        <Card>
          <div className="mb-2 text-xs uppercase tracking-wide text-fg-muted">{t("common:state.online")}</div>
          <div className="flex flex-wrap gap-1.5">
            {data.players.names.map((n) => (
              <Badge key={n} tone="online">
                {n}
              </Badge>
            ))}
          </div>
        </Card>
      ) : null}

      <Card>
        <div className="mb-3 text-xs uppercase tracking-wide text-fg-muted">{t("manage.title")}</div>
        <div className="flex flex-wrap gap-2">
          {(can("can_console_safe") || can("can_console_full")) && (
            <Link to={`/s/${server.key}/console`}>
              <GhostButton>
                <Terminal className="h-4 w-4" /> {t("manage.console")}
              </GhostButton>
            </Link>
          )}
          {can("can_manage_resources") && (
            <Link to={`/s/${server.key}/resources`}>
              <GhostButton>
                <Sliders className="h-4 w-4" /> {t("manage.resources")}
              </GhostButton>
            </Link>
          )}
          {can("can_manage_versions") && hasCap(server, "versions") && (
            <Link to={`/s/${server.key}/version`}>
              <GhostButton>
                <Tag className="h-4 w-4" /> {t("manage.version")}
              </GhostButton>
            </Link>
          )}
          {me?.role === "admin" && (
            <Link to={`/s/${server.key}/edit`}>
              <GhostButton>
                <Pencil className="h-4 w-4" /> {t("manage.edit")}
              </GhostButton>
            </Link>
          )}
        </div>
      </Card>

      {/* Ports, firewall commands and the reachability checks: admins only, like the edit page. */}
      {me?.role === "admin" && <ReachabilityPanel key={server.key} serverKey={server.key} />}

      {/* Incidents: start loops and rejected restarts stay findable this way, not only as a
          banner for 24 hours. */}
      {server && <IncidentList serverKey={server.key} />}

      {dialog}
    </div>
  );
}
