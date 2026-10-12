/// <reference types="vite/client" />
// One colour scale, not two.
//
// `ink`/`bone`/`blood`/`toxic`/`rust` are the legacy names of the scale that is now called
// `surface`/`line`/`fg`/`success`/`warning`/`danger` — the same CSS variables under two names
// (`border-ink-700` next to `border-line`). That is not cosmetic: whoever changes a colour has to
// think of two names, and nobody does so reliably. This guard keeps the legacy class names out.
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob("./**/*.{ts,tsx,css}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const TAILWIND = import.meta.glob("../tailwind.config.js", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const LEGACY = ["ink", "bone", "blood", "toxic", "rust"];
const UTILS =
  "bg|text|border|from|to|via|ring|fill|stroke|divide|placeholder|outline|decoration|caret|accent|shadow";
// The suffix part is not a convenience: without it `text-bone-dim` — the most common legacy
// class of all — would slip through. A guard that misses the most common form is worse than
// none: it reassures.
const RX = new RegExp(`(?<![\\w-])(${UTILS})-(${LEGACY.join("|")})(-[a-z0-9]+)?(?![\\w-])`, "g");

describe("colour scale", () => {
  it("has no legacy class names left", () => {
    const hits: string[] = [];
    for (const [path, text] of Object.entries(SOURCES)) {
      if (/\.test\.tsx?$/.test(path)) continue;
      for (const m of text.matchAll(RX)) hits.push(`${path}: ${m[0]}`);
    }
    expect(hits).toEqual([]);
  });

  it("no longer defines the legacy names in the Tailwind configuration either", () => {
    // The second half, and the more important one: as long as the old names STAY defined, an
    // overlooked class keeps working and nobody notices. Once they are gone, an overlooked class
    // is a class without effect — visible instead of silent.
    const cfg = Object.values(TAILWIND)[0];
    expect(cfg).toBeTruthy();
    const colourBlock = cfg.slice(cfg.indexOf("colors: {"), cfg.indexOf("fontFamily: {"));
    for (const name of LEGACY) {
      expect(colourBlock).not.toMatch(new RegExp(`^\\s*${name}:`, "m"));
    }
  });
});
