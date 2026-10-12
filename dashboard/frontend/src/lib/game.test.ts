// `hasCap` is the gate in front of every operations tab (features.ts reads through it only),
// `gameLabel`/`gameSuffix` write the name above every server. All of them are fed with data from
// the registry, i.e. with anything a backend field can be — including missing.
//
// Looks are NOT tested here: emblem, pattern and edge shape live in `index.css` and
// `components/game/`.
import { describe, expect, it } from "vitest";
import {
  GAME_LABEL,
  GameType,
  ServerInfo,
  gameLabel,
  hasCap,
  versionChangePullsImage,
  gameSuffix,
} from "./game";

// Derived from GAME_LABEL, not copied: a hand-written list here would mean that a new game
// misses exactly the check this file exists for.
const GAMES = Object.keys(GAME_LABEL) as GameType[];

function srv(over: Partial<ServerInfo> = {}): ServerInfo {
  return { key: "s", game_type: "pz", capabilities: [], ...over } as ServerInfo;
}

describe("hasCap", () => {
  it("recognises capabilities that are present", () => {
    expect(hasCap(srv({ capabilities: ["console", "mods"] }), "console")).toBe(true);
    expect(hasCap(srv({ capabilities: ["console", "mods"] }), "versions")).toBe(false);
  });

  it("says no instead of throwing when the server or the field is missing", () => {
    // The active server is `null` while loading, and an old registry row may not carry
    // `capabilities` at all. Neither may crash — and above all neither may yield an accidental
    // "yes" that would open a tab the game cannot serve.
    expect(hasCap(null, "console")).toBe(false);
    expect(hasCap(undefined, "console")).toBe(false);
    expect(hasCap(srv({ capabilities: undefined as unknown as string[] }), "console")).toBe(false);
    expect(hasCap(srv({ capabilities: "console" as unknown as string[] }), "console")).toBe(false);
  });
});

describe("labels", () => {
  it("knows every game — a gap would be an empty tab name", () => {
    for (const g of GAMES) {
      expect(GAME_LABEL[g]).toBeTruthy();
      expect(gameLabel(srv({ game_type: g }))).toBe(GAME_LABEL[g]);
    }
  });

  it("falls back instead of returning undefined", () => {
    // A future game must not break the UI before someone has registered it.
    expect(gameLabel(srv({ game_type: "sons_of_the_forest" as GameType }))).toBeTruthy();
    expect(gameLabel(null)).toBeTruthy();
    expect(gameLabel(undefined)).toBeTruthy();
  });
});

describe("gameSuffix", () => {
  it("stays silent when the server is named like its game", () => {
    // If the server is called "Factorio", a game line above it is a repetition and has to go —
    // not set smaller, gone.
    expect(gameSuffix(srv({ game_type: "factorio", display_name: "Factorio" }))).toBe("");
    expect(gameSuffix(srv({ game_type: "valheim", display_name: "  valheim " }))).toBe("");
  });

  it("names the game when the server is called something else", () => {
    expect(gameSuffix(srv({ game_type: "pz", display_name: "Project Zomboid (Test)" }))).toBe(
      "Project Zomboid",
    );
  });

  it("names the game without a display name too", () => {
    // A registry row without `display_name` exists (the model default is ""), and then the
    // game name is the only thing shown.
    expect(gameSuffix(srv({ game_type: "avorion", display_name: "" }))).toBe("Avorion");
  });
});

describe("versionChangePullsImage", () => {
  it("follows the driver, not the game name", () => {
    // A check on `game_type` would have to list every image-tagged game by name, at every
    // call site.
    expect(versionChangePullsImage(srv({ game_type: "factorio", version_source: "image_tag" }))).toBe(true);
    expect(versionChangePullsImage(srv({ game_type: "minecraft", version_source: "env" }))).toBe(false);
    expect(versionChangePullsImage(srv({ game_type: "valheim", version_source: "runtime" }))).toBe(false);
  });

  it("falls back to the old meaning without the field", () => {
    // A response without the field (or a registry row without a driver) must not break the
    // version page.
    expect(versionChangePullsImage(srv({ game_type: "pz" }))).toBe(true);
    expect(versionChangePullsImage(srv({ game_type: "minecraft" }))).toBe(false);
    expect(versionChangePullsImage(null)).toBe(false);
  });
});
