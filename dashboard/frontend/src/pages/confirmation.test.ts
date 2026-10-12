/// <reference types="vite/client" />
/**
 * Whoever changes access, rights or world data asks first.
 *
 * On the moderation page seven buttons work on THE SAME freely typed name field, in a
 * `flex-wrap` container — on a phone that is several rows right next to each other, and
 * "Whitelist +" and "Whitelist −" differ by an icon and one character. Unbanning and removing
 * from the whitelist have no way back except the opposite action, so they need the same dialog
 * banning has. And the level selector must not be preselected: the first level that is not
 * "none" is "admin" for Project Zomboid.
 *
 * Checked here: no call of an intervening endpoint directly from an `onClick`. The way there
 * has to go through a function that calls `confirm(...)` first.
 *
 * The limit, named honestly: this checks the FORM (`onClick={() => call("/moderation/unban", …)}`),
 * not that the dialog is answered. Calling `confirm` and ignoring the result gets through.
 */
import { describe, expect, it } from "vitest";

const PAGES = import.meta.glob("./*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Endpoints that change access or rights and have no way back except the opposite action. */
const INTERVENING = [
  "/moderation/unban",
  "/moderation/ban",
  "/moderation/whitelist/remove",
  "/moderation/access",
];

describe("confirmation", () => {
  it("no intervening call directly from an onClick", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(PAGES)) {
      for (const endpoint of INTERVENING) {
        let i = source.indexOf(endpoint);
        while (i >= 0) {
          // Look back to the start of the enclosing expression: an `onClick={` there means the
          // call hangs on the click without a step in between.
          const before = source.slice(Math.max(0, i - 400), i);
          const line = source.slice(0, i).split("\n").length;
          if (/onClick=\{[^}]*$/.test(before) && !/\bconfirmed\(/.test(before)) {
            violations.push(`${file}:${line} → ${endpoint}`);
          }
          i = source.indexOf(endpoint, i + 1);
        }
      }
    }
    expect(
      violations,
      "These places change access or rights with a single tap, without a confirmation and without\n" +
        "a way back — while the opposite action next to them has a dialog:\n  " + violations.join("\n  "),
    ).toEqual([]);
  });

  it("the level selector has no preselection", () => {
    const source = PAGES["./Moderation.tsx"];
    expect(source, "Moderation.tsx not found").toBeTruthy();
    // `find(l => l !== "none")` hits "admin" for PZ (`_ACCESS_LEVELS` starts with it). A click on
    // "Set level" without touching the selector would then hand out in-game admin rights.
    expect(/access_levels\.find\(/.test(source)).toBe(false);
    expect(/setLevel\(""\)/.test(source)).toBe(true);
  });
});
