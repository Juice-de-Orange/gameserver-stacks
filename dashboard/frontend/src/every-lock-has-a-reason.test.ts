/// <reference types="vite/client" />
/**
 * No locked feature lands on the placeholder sentence.
 *
 * `lib/features.ts` ends with a last fallback (`features:reason.unrecorded`):
 *
 *     Not available for <game> — no reason has been recorded, please report this.
 *
 * The sentence is written like that on purpose: it asks for a bug report. Precisely because of
 * that it must never appear in normal operation — otherwise the UI asks for a report about
 * something nobody forgot, and the user reports a non-bug.
 *
 * This guard walks through **every** feature × game combination, including those of a game
 * nobody has entered yet. It is the reason the placeholder may exist at all.
 */
import { describe, expect, it } from "vitest";
import i18n from "./i18n";
import { featureAvailability, type Feature } from "./lib/features";
import { GAME_LABEL, type GameType, type ServerInfo } from "./lib/game";

const FEATURES: Feature[] = [
  "overview", "console", "resources", "version", "backups", "config", "logs",
  "restarts", "actions", "moderation", "mods", "join",
];

const GAMES = Object.keys(GAME_LABEL) as GameType[];

/** A server that can do NOTHING — this reaches every lock there is. */
function bare(game_type: GameType): ServerInfo {
  return { key: "x", game_type, capabilities: [], game_ports: {} } as unknown as ServerInfo;
}

describe("every lock has a reason", () => {
  for (const game of GAMES) {
    for (const feature of FEATURES) {
      it(`${game} · ${feature}`, () => {
        const av = featureAvailability(bare(game), feature);
        if (av.available) return;
        expect(av.reason, `${game}/${feature} is locked but does not say why`).toBeTruthy();
        expect(
          av.reason,
          `${game}/${feature} lands on the placeholder — it asks for a bug report where there is no bug`,
        ).not.toBe(i18n.t("features:reason.unrecorded", { game: GAME_LABEL[game] }));
        // A reason whose catalog entry is missing would show up as the raw key.
        expect(av.reason, `${game}/${feature} shows a catalog key instead of a text`).not.toMatch(/^reason\./);
      });
    }
  }

  it("reaches locks at all — otherwise the test would be green because it finds nothing", () => {
    const locked = GAMES.flatMap((g) =>
      FEATURES.filter((f) => !featureAvailability(bare(g), f).available),
    );
    expect(locked.length).toBeGreaterThan(10);
  });

  it("the placeholder itself is a real sentence that names the game", () => {
    expect(i18n.t("features:reason.unrecorded", { game: "Factorio" })).toContain("Factorio");
    expect(i18n.t("features:reason.unrecorded", { game: "Factorio" })).toContain("no reason has been recorded");
  });
});
