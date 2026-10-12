/// <reference types="vite/client" />
/**
 * Nobody builds a reconnect backoff of their own.
 *
 * A hand-copied backoff tends to reset its counter on the FIRST message. For `/ws/logs` that
 * means: a stopped container answers `docker logs?follow=1&tail=200` with 200, sends the last
 * 200 lines and ends the stream at once. The message arrives before the close, `retry` is back
 * at 0, the next connection follows after exactly one second. Per second one WS handshake and
 * one call to the Docker socket proxy — against a backend with a single uvicorn worker, and
 * precisely during version changes, mod applies and restores, when the container is down for
 * minutes.
 *
 * The rule lives in `lib/reconnect.ts` (with the tests of its behaviour); this guard makes sure
 * nobody builds a private one past it.
 *
 * Known limit: the guard recognises the telltale SHAPE (`2 ** retry`, `retry = 0`), not every
 * conceivable home-made variant. A backoff written differently would escape.
 */
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob("./{pages,components}/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/**
 * Strip comments before searching — otherwise the guard fires on exactly the lines that EXPLAIN
 * the pitfall. A guard that forbids talking about a bug teaches people to leave out the reason.
 */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

/** The shapes the hand-copied versions took. */
const HOME_MADE = [
  { pattern: /2 \*\* retry/, what: "home-made backoff (2 ** retry)" },
  { pattern: /\bretry\s*=\s*0\b/, what: "home-made counter reset (retry = 0)" },
  { pattern: /Date\.now\(\)\s*-\s*openedAt/, what: "home-made held check (Date.now() - openedAt)" },
];

describe("reconnect guard", () => {
  it("no page and no component builds its own backoff", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes(".test.")) continue;
      const code = withoutComments(source);
      for (const { pattern, what } of HOME_MADE) {
        const i = code.search(pattern);
        if (i >= 0) {
          violations.push(`${file}:${code.slice(0, i).split("\n").length} → ${what}`);
        }
      }
    }
    expect(
      violations,
      "These places build their own reconnect. The rule — reset only once the connection has\n" +
        "HELD — lives in `lib/reconnect.ts`; a copy of it drifts apart sooner or later:\n  " + violations.join("\n  "),
    ).toEqual([]);
  });

  it("whoever opens a WebSocket uses the helper", () => {
    const missing: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes(".test.")) continue;
      if (!/new WebSocket\(/.test(source)) continue;
      // Exception with a reason: code that never reconnects needs no backoff either.
      if (!/setTimeout\(connect/.test(source)) continue;
      if (!/newReconnect/.test(source)) missing.push(file);
    }
    expect(missing, "WebSocket with reconnect but without `newReconnect`:\n  " + missing.join("\n  "))
      .toEqual([]);
  });
});
