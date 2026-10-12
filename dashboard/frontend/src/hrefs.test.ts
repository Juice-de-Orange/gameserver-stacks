/// <reference types="vite/client" />
/**
 * No foreign URL goes into an `href` unchecked.
 *
 * The sharpest case is `Tickets.tsx`: `workshop_url` comes from the "request a mod" form, which
 * any signed-in user can fill in, and `routers/tickets.py` stores the value verbatim. React sets
 * `javascript:…` as `href` without complaint; it is clicked by the admin who is to decide on the
 * request — and the access token lives in `localStorage`. That is the path from "may write
 * tickets" to "is admin".
 *
 * The other cases are the same class with calmer sources (Steam news API, `.env`, mod list). The
 * difference is not visible at the call site, which is why this is a guard and not a single fix.
 *
 * What is written **in the source** stays allowed: a template literal with a fixed `https://`
 * start and a reference to a local constant cannot carry a foreign scheme.
 *
 * Known limit: the guard checks the shape at the call site, not where the value comes from.
 * Applying `safeUrl` to a variable that was itself assembled passes — the guard sees THAT a
 * check happens, not WHAT is checked.
 */
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob("./{pages,components}/**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** `href={…}` with the expression inside. */
const HREF = /href=\{([^}]*)\}/g;

/** An expression that cannot carry a foreign scheme. */
function harmless(expression: string): boolean {
  const a = expression.trim();
  if (a.startsWith("safeUrl(")) return true;                 // checked
  if (/^`(https?:)?\/\//.test(a)) return true;                  // fixed scheme in the source
  if (/^`\//.test(a)) return true;                              // own path
  if (/^["'](https?:)?\/\//.test(a) || /^["']\//.test(a)) return true;
  if (/^[A-Z_]{2,}\[/.test(a)) return true;                     // local constant table
  return false;
}

describe("hrefs", () => {
  it("every foreign URL goes through safeUrl", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes(".test.")) continue;
      for (const m of source.matchAll(HREF)) {
        if (harmless(m[1])) continue;
        violations.push(`${file}:${source.slice(0, m.index).split("\n").length} → href={${m[1].trim()}}`);
      }
    }
    expect(
      violations,
      "These places set a URL as `href` without checking its scheme. React sets\n" +
        "`javascript:…` without complaint, and the click runs in the session of whoever clicks:\n  " +
        violations.join("\n  "),
    ).toEqual([]);
  });

  it("the guard tells the shapes apart", () => {
    // Counter-check — otherwise nothing shows that it separates them at all.
    expect(harmless("safeUrl(t.workshop_url)")).toBe(true);
    expect(harmless("`https://steamcommunity.com/x?id=${id}`")).toBe(true);
    expect(harmless("PARAM_HELP[p.name].url")).toBe(true);
    expect(harmless("t.workshop_url")).toBe(false);
    expect(harmless("selected.changelog_url")).toBe(false);
    expect(harmless("m.url")).toBe(false);
  });
});
