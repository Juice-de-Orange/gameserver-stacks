// Registry types, capability gating and the active-games switch for the supported games.
// Mirrors `server_public` of the backend (GET /api/servers).
//
// Deliberately NOT here: what a game looks like. Emblem, pattern, edge shape and label typography
// live in `index.css` (`[data-game="…"]`, the `--g-*` tokens) and in `components/game/`. A file
// that held both registry types and design would be touched in two places for every new game.
import i18n from "../i18n";

export type GameType =
  | "pz"
  | "minecraft"
  | "vintagestory"
  | "arma_reforger"
  | "valheim"
  | "avorion"
  | "factorio";

export interface ServerInfo {
  id: number;
  key: string;
  game_type: GameType;
  display_name: string;
  sort_order: number;
  enabled: boolean;
  management_mode: "compose" | "dashboard";
  container_name: string;
  data_dir: string;
  image_repo: string;
  image_tag: string;
  network: string;
  console_kind: string;
  rcon_host: string;
  rcon_port: number;
  rcon_password_env: string;
  game_ports: Record<string, string>;
  /** Host address the game ports are published on: 0.0.0.0 = every interface. */
  bind_address?: string;
  game_version: string;
  server_type: string;
  ram_mb: number;
  cpu_cores: number;
  heap_max_mb: number | null;
  heap_min_mb: number | null;
  idle_autostop_enabled: boolean;
  idle_timeout_minutes: number | null;
  bridge_in: boolean;
  bridge_out: boolean;
  /**
   * Careful, the name misleads: for Project Zomboid this field switches the **Steam visibility**
   * (`PUBLIC` in the container environment, i.e. the entry in the server browser) — NOT the
   * who-is-where overview of the cross-server bridge. That one filters solely by
   * `presence_enabled` in the cross-server settings.
   *
   * There is deliberately NO switch for it in the UI: a change only takes effect after the
   * container is recreated, and whoever wants to hide presence would pick the wrong switch.
   */
  presence_visible: boolean;
  announce_target: boolean;
  /** Free-form options; secret values and the names of secret variables arrive masked. */
  extra?: Record<string, unknown>;
  capabilities: string[];
  /** Where the version comes from: "image_tag" (a switch pulls an image) | "env" | "runtime". */
  version_source?: "image_tag" | "env" | "runtime" | "";
  /** What makes a configuration change take effect: "live" | "restart" | "recreate". */
  config_reload?: "live" | "restart" | "recreate" | "";
  created_at?: string;
  updated_at?: string;
}

export const GAME_LABEL: Record<GameType, string> = {
  pz: "Project Zomboid",
  minecraft: "Minecraft",
  vintagestory: "Vintage Story",
  arma_reforger: "Arma Reforger",
  valheim: "Valheim",
  avorion: "Avorion",
  factorio: "Factorio",
};

/** Every supported game, in the backend's order (`GameType`). Derived from GAME_LABEL, so a new
 *  game cannot be missing from one list and present in the other. */
export const GAMES = Object.keys(GAME_LABEL) as GameType[];

/** One row of `GET /api/settings/games`: whether this installation runs the game. Only active
 *  games get presets, version lists and downloads (`backend/app/services/game_settings.py`). */
export interface GameSetting {
  game_type: GameType;
  active: boolean;
}

export interface GameSettingsResponse {
  games: GameSetting[];
}

/** The active games, in GAMES order. A game type this build does not know (a newer backend) is
 *  left out: there is no label or emblem for it here. */
export function activeGames(settings: readonly GameSetting[] | null | undefined): GameType[] {
  if (!Array.isArray(settings)) return [];
  const on = new Set(settings.filter((g) => g?.active).map((g) => g.game_type));
  return GAMES.filter((g) => on.has(g));
}

/** What `gameLabel`/`gameSuffix` really need. Deliberately NOT `ServerInfo`: the fleet page gets
 *  a leaner row from `/api/servers/fleet`, and the naming rule applies there just the same. A
 *  helper that demanded the full registry row would force a second version. */
export interface Nameable {
  game_type: GameType;
  display_name?: string;
}

/** The game name of a server — with a fallback, so that an unknown game leaves no gap. */
export function gameLabel(server: Nameable | null | undefined): string {
  if (!server) return i18n.t("game:fallback");
  return GAME_LABEL[server.game_type] ?? i18n.t("game:fallback");
}

/** The game name — but ONLY if it does not say the same as the server name.
 *
 * If the server is named like its game, a game line above the name is a repetition — this
 * function then returns an empty string and the space belongs to something that carries
 * information. It stays visible for "Project Zomboid (Test)", because there the difference is
 * the information.
 *
 * One function, not three comparisons: the hero, the server chip and the fleet table need the
 * same answer, and three copies would be three chances to answer it differently.
 */
export function gameSuffix(server: Nameable | null | undefined): string {
  const game = gameLabel(server);
  const name = (server?.display_name || "").trim();
  return name.toLowerCase() === game.toLowerCase() ? "" : game;
}

/** Does the server have a given capability (e.g. "console", "mods", "versions")? */
export function hasCap(server: ServerInfo | null | undefined, cap: string): boolean {
  return !!server && Array.isArray(server.capabilities) && server.capabilities.includes(cap);
}

/** Does a version switch pull an image for this server? (PZ, Factorio, Avorion)
 *
 * Comes from the driver, not from a list of game names in the frontend. Older responses without
 * the field fall back to the original meaning.
 */
export function versionChangePullsImage(server: ServerInfo | null | undefined): boolean {
  if (!server) return false;
  if (server.version_source) return server.version_source === "image_tag";
  return server.game_type === "pz";
}
