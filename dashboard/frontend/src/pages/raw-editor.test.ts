/// <reference types="vite/client" />
/**
 * Whoever writes back a WHOLE file must send along the state their text is based on.
 *
 * The configuration page has four editors over two files: "Settings" and "server.ini (raw)"
 * both write the `.ini`, the quick settings and the SandboxVars field both write the `.lua`.
 * The two GUI forms send single keys (`values`) and can therefore be merged — the two raw
 * editors send the complete file content (`content`).
 *
 * A raw buffer that still holds the state from page load would **silently** undo a save made
 * in the GUI tab — and likewise the change of a second browser tab, a second admin, `mods/apply`
 * or a version change. So `GET …/config/{raw,sandbox}` returns a `revision` (hash of the file
 * content) and the `PUT` requires it: if it no longer matches, the server answers 409 instead
 * of overwriting. This test pins down that the frontend sends it — the protection hangs on one
 * field in a payload, and that is exactly what gets lost without a sound in the next rework.
 *
 * The search goes by the payload (`content`), not by the path: the path is a composed template
 * in the code, a grep for it would find nothing after the first rework.
 *
 * Limit, named honestly: what is checked is that `revision` is in the payload — not that it is the
 * stamp of THIS file. A stamp swapped between `.ini` and `.lua` would stay invisible (it would
 * trigger a 409, though, so no silent loss).
 */
import { describe, expect, it } from "vitest";

const PAGES = import.meta.glob("./*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** A payload with `content` is the complete file content — everything else is called `values`. */
const WHOLE_FILE = /JSON\.stringify\(\{\s*content\b[^}]*\}/g;

function missingRevisions(source: string): number[] {
  const hits: number[] = [];
  for (const m of source.matchAll(WHOLE_FILE)) {
    if (!/\brevision\b/.test(m[0])) hits.push(source.slice(0, m.index).split("\n").length);
  }
  return hits;
}

describe("raw editors", () => {
  it("send the file stamp along when saving", () => {
    const violations: string[] = [];
    for (const [file, source] of Object.entries(PAGES)) {
      for (const line of missingRevisions(source)) violations.push(`${file}:${line}`);
    }
    expect(
      violations,
      "These places write a whole file back without sending the state their buffer is based\n" +
        "on. Everything that arrived in the file since loading is silently overwritten — the\n" +
        "user's own change from the other tab included:\n  " + violations.join("\n  "),
    ).toEqual([]);
  });

  it("the guard would find the old bug", () => {
    // Cross-check — without it there would be no proof that it checks anything at all.
    const before = 'await apiJson(path, { method: "PUT", body: JSON.stringify({ content }) });';
    const after = 'await apiJson(path, { method: "PUT", body: JSON.stringify({ content, revision }) });';
    expect(missingRevisions(before)).toEqual([1]);
    expect(missingRevisions(after)).toEqual([]);
  });
});
