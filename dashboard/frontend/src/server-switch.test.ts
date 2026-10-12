/// <reference types="vite/client" />
/**
 * State must not survive a server switch.
 *
 * React Router does **not** remount when going from `/s/pz-1/config` to `/s/mc/config`: same
 * route, same element type, same place in the tree. A component that holds state therefore
 * carries it over to the new server — the config editor its changed fields, the mods page its
 * file selection (`sel`). A "save" before the reload has finished then writes the values of ONE
 * server into the OTHER, for mods including the mandatory backup and the boot proof.
 *
 * `FeatureGate` solves this for all server-scoped routes at once. This test guards the
 * outliers: components that get `server` as a prop and hold state themselves need a `key` too.
 *
 * **Known limit:** only the literal form `server={server}` is recognised, and only a
 * declaration in the same module. A component that takes `serverKey={key}` or calls
 * `useActiveServer()` itself is invisible — and that is most pages. That is why the
 * `FeatureGate` remount is the actual solution and this test only the rearguard.
 */
import { describe, expect, it } from "vitest";

// Vite's `import.meta.glob` instead of `node:fs` — the test needs no Node types and the
// frontend no extra dependency. `?raw` yields the source text as a string.
const FILES = import.meta.glob("./**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Does the component `name` in this file hold state of its own? */
function holdsState(source: string, name: string): boolean {
  const start = source.search(new RegExp(`function\\s+${name}\\s*\\(`));
  if (start < 0) return false;
  // Read up to the next module-level declaration — crude, but sufficient.
  const rest = source.slice(start + 1);
  const end = rest.search(/\n(export )?(function|const|class) /);
  const body = end < 0 ? rest : rest.slice(0, end);
  return /\buse(State|Reducer|Ref)\s*[<(]/.test(body);
}

describe("server switch", () => {
  it("stateful components with a server prop get a key", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(FILES)) {
      if (file.endsWith(".test.tsx")) continue;
      for (const m of source.matchAll(/<([A-Z][A-Za-z0-9_]*)\b([^>]*?)server=\{server\}/g)) {
        const [match, name, before] = m;
        const line = source.slice(0, m.index).split("\n").length;
        const hasKey = /\bkey=\{/.test(before) || /\bkey=\{/.test(match);
        if (!hasKey && holdsState(source, name)) {
          violations.push(`${file}:${line} → <${name} server={server}>`);
        }
      }
    }
    expect(
      violations,
      "These components hold state and get the server as a prop, but no `key`.\n" +
        "React Router does not remount them on a server switch — the state of one server\n" +
        "ends up with the other:\n  " + violations.join("\n  "),
    ).toEqual([]);
  });

  it("FeatureGate forces a remount per server", () => {
    const app = FILES["./App.tsx"];
    const gate = app.slice(app.indexOf("function FeatureGate"));
    expect(
      /key=\{server\?\.key/.test(gate.slice(0, gate.indexOf("\n}"))),
      "FeatureGate does not remount per server — then EVERY server-scoped page carries its " +
        "state across the server switch.",
    ).toBe(true);
  });
});
