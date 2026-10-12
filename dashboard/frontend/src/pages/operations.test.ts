/// <reference types="vite/client" />
/**
 * Whoever starts a background operation has to track it as well.
 *
 * Several endpoints run as `operations.start`: applying mods, mod files, backup restore, version
 * change and version rollback. They answer **at once** with an operation id; the actual work
 * (create-then-swap, `verify_boot` with up to 900 s) runs in the task afterwards. Treating such
 * a call as synchronous reports success within milliseconds, and a failed boot proof **never**
 * reaches the user.
 *
 * Why a contract test against the source and not a render test: these pages depend on
 * `ActiveServerProvider`, `FeedbackProvider`, `useOperation`, the router and `api()`. Rendering
 * them would mean rebuilding half the app — and the test would then check the mocks, not the
 * rule. The rule itself is structural: **no `toast.success` after starting an operation without
 * a `track(...)` before it.**
 *
 * The limit, named honestly: the call is detected, not its effect. A `track(...)` that passes
 * the wrong id stays invisible.
 */
import { describe, expect, it } from "vitest";

const PAGES = import.meta.glob("./*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Endpoints that start a background operation (backend: `operations.start`). */
const OPERATION_ENDPOINTS = [
  "/version/change",
  "/version/rollback",
  "/mods/apply",
  "/mods/files/apply",
  "/restore",
  // The manual backup. This path is fuzzier than the others — it also matches the GET that
  // fetches the list. That is harmless because no success is reported there; a sharper pattern
  // would have to read the method and would break on the first reformatting.
  "/backups`",
];

/** The function body around `index` — roughly, up to the next declaration. */
function bodyAround(source: string, index: number): string {
  const prev = source.lastIndexOf("\n  async function", index);
  const start = prev >= 0 ? prev : Math.max(0, index - 1500);
  const rest = source.slice(start + 1);
  const end = rest.search(/\n {2}(async )?function /);
  return end < 0 ? rest : rest.slice(0, end);
}

describe("background operations", () => {
  it("every operation start is tracked before success is reported", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(PAGES)) {
      if (file.endsWith(".test.tsx")) continue;
      for (const endpoint of OPERATION_ENDPOINTS) {
        let i = source.indexOf(endpoint);
        while (i >= 0) {
          const body = bodyAround(source, i);
          const reportsSuccess = /toast\.success\(/.test(body);
          // `track…(` with a suffix: a page with TWO kinds of operations has to rename the
          // helpers (`track: trackBackup`). A rigid `track(` would flag exactly the page
          // that does it right.
          const tracked = /\btrack[A-Za-z]*\(/.test(body);
          if (reportsSuccess && !tracked) {
            const line = source.slice(0, i).split("\n").length;
            violations.push(`${file}:${line} → ${endpoint}`);
          }
          i = source.indexOf(endpoint, i + 1);
        }
      }
    }
    expect(
      violations,
      "These places start a background operation and report success without tracking it.\n" +
        "The endpoint answers at once — the message claims a completion that does not exist,\n" +
        "and a failure never reaches the user:\n  " + violations.join("\n  "),
    ).toEqual([]);
  });

  it("the guard would find the old mistake", () => {
    // Cross-check — without it there is no evidence that the guard checks anything at all.
    const old = `
  async function rollback() {
    const r = await api(\`/servers/\${key}/version/rollback\`, { method: "POST" });
    if (!r.ok) throw new Error("broken");
    toast.success("Rolled back.");
  }
`;
    const i = old.indexOf("/version/rollback");
    const body = bodyAround(old, i);
    expect(/toast\.success\(/.test(body)).toBe(true);
    expect(/\btrack[A-Za-z]*\(/.test(body)).toBe(false);
  });
});
