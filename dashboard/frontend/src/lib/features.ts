// The one place that decides whether an operations tab works for the selected server.
// The navigation (greyed out + tooltip), the server home tiles and the FeatureGate in App.tsx all
// read from here. Principle: role/permission hides, game capability greys out.
import i18n from "../i18n";
import { GAME_LABEL, GameType, ServerInfo, hasCap } from "./game";

export type Feature =
  | "overview"
  | "console"
  | "resources"
  | "version"
  | "backups"
  | "config"
  | "logs"
  | "restarts"
  | "actions"
  | "moderation"
  | "mods"
  | "join";

export interface Availability {
  available: boolean;
  reason?: string;
}

// For which games an operations feature REALLY works per server. Whatever is missing here is
// missing for a factual reason, and that reason is in `REASONS` — never a "coming soon".
type RolloutFeature = "backups" | "config" | "logs" | "restarts" | "actions" | "moderation" | "mods";

// Every game the dashboard knows. The table below subtracts what is missing individually — not
// the other way round. A new game is therefore fully operable by default, and where it is not, a
// reason is recorded in `REASONS`. With a list of the supported games instead, a new game would
// be locked out of seven tabs with the "no reason has been recorded" sentence.
const ALL: GameType[] = [
  "pz",
  "minecraft",
  "vintagestory",
  "arma_reforger",
  "valheim",
  "avorion",
  "factorio",
];

/** All but the named ones — so that an exception stays readable as an exception. */
const except = (...excluded: GameType[]): GameType[] => ALL.filter((g) => !excluded.includes(g));

const BUILT: Record<RolloutFeature, GameType[]> = {
  backups: ALL,
  config: ALL,
  logs: ALL,
  restarts: ALL,
  // Game master actions: Reforger and Valheim stay out; the reasons are in REASONS and are
  // limits of the games, not open tasks.
  actions: except("arma_reforger", "valheim"),
  moderation: ALL,
  mods: ALL,
};

// Why a feature is missing for a particular game — the honest sentence, not "coming soon".
// The values are catalog keys (namespace `features`), translated when the reason is asked for:
// a translated text stored here would freeze the language at import.
const REASONS: Partial<Record<Feature, Partial<Record<GameType, string>>>> = {
  actions: {
    arma_reforger: "reason.actions.arma_reforger",
    valheim: "reason.actions.valheim",
  },
  console: {
    valheim: "reason.console.valheim",
  },
};

function reasonFor(feature: Feature, game: GameType, standardKey?: string): string {
  const key = REASONS[feature]?.[game] ?? standardKey;
  if (key) return i18n.t(`features:${key}`);
  // No placeholder promise: whoever lands here has added a feature without recording a reason.
  // The sentence says so (`every-lock-has-a-reason.test.ts` keeps it out of normal use).
  return i18n.t("features:reason.unrecorded", { game: GAME_LABEL[game] });
}

export function featureAvailability(
  server: ServerInfo | null | undefined,
  feature: Feature,
): Availability {
  if (!server) return { available: false, reason: i18n.t("features:reason.noServer") };
  const game = server.game_type;

  switch (feature) {
    case "overview":
    case "resources":
      return { available: true };
    case "console":
      // The general sentence is the fallback; a specific reason wins. For Valheim it is a
      // property of the game, not a missing setup step, and the text should say so.
      return hasCap(server, "console")
        ? { available: true }
        : { available: false, reason: reasonFor("console", game, "reason.console.default") };
    case "version":
      // The default sentence matters most when the driver reports no capabilities at all
      // (because it failed to load): without it every game would land on the placeholder.
      return hasCap(server, "versions")
        ? { available: true }
        : { available: false, reason: reasonFor("version", game, "reason.version.default") };
    case "join":
      // The address is in the registry and the way in comes from the driver. What CAN be
      // missing is the published port — then there simply is no way in from outside.
      return server.game_ports?.game
        ? { available: true }
        : { available: false, reason: i18n.t("features:reason.join.noPort") };
    default: {
      const built = BUILT[feature];
      if (built.includes(game)) return { available: true };
      return { available: false, reason: reasonFor(feature, game) };
    }
  }
}
