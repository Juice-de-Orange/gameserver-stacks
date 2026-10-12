// The head of a server page.
//
// 1. **The name is the name.** The heading is the `display_name`, and the line above it names
//    the game ONLY if the server is called differently from its game (so for "Project Zomboid
//    (Test)" — not for "Factorio").
// 2. **The art style is the surface, not a tint.** Pattern, edge, grain and keyline come from
//    `GameSurface`/`GameRule`; the lettering from `GameLabel`.
import { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { gameLabel, ServerInfo, gameSuffix } from "../../lib/game";
import { fmtDuration } from "../../lib/format";
import { cx } from "../ui";
import { GameEmblem } from "./GameEmblem";
import { GameLabel } from "./GameLabel";
import { GameRule, GameSurface } from "./GameSurface";

export interface HeroStats {
  /** `null`: not known yet (the first status answer is still on the way). */
  running: boolean | null;
  players?: { count: number; names: string[] } | null;
  maxPlayers?: number | null;
  uptimeSeconds?: number | null;
  cpuPercent?: number | null;
}

export function GameHero({ server, stats }: { server: ServerInfo; stats?: HeroStats }) {
  const { t } = useTranslation();
  const running = !!stats?.running;
  const known = stats ? stats.running !== null : true;
  const name = server.display_name || gameLabel(server);
  const gameLine = gameSuffix(server);

  return (
    <GameSurface game={server.game_type} className="overflow-hidden">
      <div className="flex flex-col gap-6 p-5 sm:flex-row sm:items-end sm:justify-between sm:p-6">
        <div className="flex min-w-0 items-center gap-4 sm:gap-5">
          <span className="grid h-14 w-14 shrink-0 place-items-center border border-primary/35 bg-primary/[0.10] text-primary sm:h-[4.5rem] sm:w-[4.5rem]"
                style={{ borderRadius: "var(--g-radius)" }}>
            <GameEmblem game={server.game_type} className="h-8 w-8 sm:h-10 sm:w-10" strokeWidth={1.6} />
          </span>
          <div className="min-w-0">
            {gameLine && (
              <GameLabel className="text-[10px] text-primary-text sm:text-[11px]">{gameLine}</GameLabel>
            )}
            <GameLabel
              as="h1"
              className="truncate text-[1.75rem] leading-none text-fg sm:text-[2.4rem]"
            >
              {name}
            </GameLabel>
            <GameRule game={server.game_type} className="mt-3 w-24 sm:w-32" />
            <div className="mt-3 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-sm">
              <span
                className={cx(
                  "inline-flex h-2 w-2 shrink-0",
                  running ? "bg-success animate-live" : "bg-fg-dim",
                )}
                style={{ borderRadius: running ? "9999px" : "0" }}
              />
              <span className={running ? "font-medium text-success-text" : "text-fg-muted"}>
                {!known ? t("state.loading") : running ? t("state.running") : t("state.stopped")}
              </span>
              {server.game_version && (
                <span className="font-mono text-xs text-fg-dim">· {server.game_version}</span>
              )}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-6 sm:gap-8">
          <HeroStat
            label={t("word.players")}
            value={
              stats?.players
                ? `${stats.players.count}${stats.maxPlayers ? "/" + stats.maxPlayers : ""}`
                : "–"
            }
          />
          <HeroStat label={t("word.uptime")} value={stats?.uptimeSeconds ? fmtDuration(stats.uptimeSeconds) : "–"} />
          <HeroStat
            label="CPU"
            value={running && stats?.cpuPercent != null ? `${Math.round(stats.cpuPercent)} %` : "–"}
          />
        </div>
      </div>
    </GameSurface>
  );
}

function HeroStat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <GameLabel className="text-[10px] text-fg-muted">{label}</GameLabel>
      <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-fg">{value}</div>
    </div>
  );
}
