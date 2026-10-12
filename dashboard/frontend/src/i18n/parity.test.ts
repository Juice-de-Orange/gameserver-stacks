/// <reference types="vite/client" />
// Both languages must offer the same keys with the same placeholders. A key that exists in one
// catalog only shows up as English text in a German page (or as a raw key), and a placeholder
// that is missing in one language silently drops a value.
import { describe, expect, it } from "vitest";
import i18n, { buildResources, LANGUAGES, tb } from "./index";
import { detailText } from "../api";

const files = import.meta.glob("./locales/*/*.json", { eager: true, import: "default" }) as Record<
  string,
  Record<string, string>
>;

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([\w.]+)\s*\}\}/g)].map((m) => m[1]!).sort();
}

describe("translation catalogs", () => {
  const perFile: Record<string, Record<string, Record<string, string>>> = {};
  for (const [path, catalog] of Object.entries(files)) {
    const [, language, file] = /\/locales\/([^/]+)\/([^/]+)\.json$/.exec(path)!;
    (perFile[file!] ??= {})[language!] = catalog;
  }

  it("has at least the shared catalog", () => {
    expect(Object.keys(perFile)).toContain("common");
  });

  for (const [file, languages] of Object.entries(perFile)) {
    it(`${file}: exists in every language with the same keys`, () => {
      expect(Object.keys(languages).sort()).toEqual([...LANGUAGES].sort());
      const [en, de] = [languages.en!, languages.de!];
      expect(Object.keys(de).sort()).toEqual(Object.keys(en).sort());
    });

    it(`${file}: placeholders match and no text is empty`, () => {
      const [en, de] = [languages.en!, languages.de!];
      for (const key of Object.keys(en)) {
        expect(en[key], `${file}:${key} (en)`).not.toBe("");
        expect(de[key] ?? "", `${file}:${key} (de)`).not.toBe("");
        expect(placeholders(de[key] ?? ""), `${file}:${key}`).toEqual(placeholders(en[key]!));
      }
    });
  }

  it("merges the api-* and backend-* files into one namespace each without clashing keys", () => {
    for (const prefix of ["api-", "backend-"]) {
      const seen = new Map<string, string>();
      for (const [file, languages] of Object.entries(perFile)) {
        if (!file.startsWith(prefix)) continue;
        for (const key of Object.keys(languages.en ?? {})) {
          expect(seen.get(key), `${key} is defined in ${seen.get(key)} and ${file}`).toBeUndefined();
          seen.set(key, file);
        }
      }
    }
    expect(Object.keys(buildResources(files).en ?? {})).toContain("api");
  });
});

describe("plain backend texts", () => {
  it("are translated by their English text, colons included, and pass through when unknown", async () => {
    i18n.addResource("de", "backend", "Restart: now or later", "Neustart: jetzt oder später");
    await i18n.changeLanguage("de");
    try {
      expect(tb("Restart: now or later")).toBe("Neustart: jetzt oder später");
      expect(tb("Port 27015 is taken")).toBe("Port 27015 is taken");
      expect(tb("Mod {{name}} $t(common:action.save)")).toBe("Mod {{name}} $t(common:action.save)");
      expect(tb(null)).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("use the English text as key in the English catalog", () => {
    for (const [path, catalog] of Object.entries(files)) {
      if (!/\/locales\/en\/backend-[^/]+\.json$/.test(path)) continue;
      for (const [key, text] of Object.entries(catalog)) expect(text, path).toBe(key);
    }
  });
});

describe("backend error codes", () => {
  it("are translated by code, with the parameters filled in", async () => {
    await i18n.changeLanguage("de");
    try {
      expect(detailText({ code: "auth.token_invalid", message: "Invalid or expired token", params: {} })).toBe(
        "Ungültiges oder abgelaufenes Token",
      );
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("fall back to the English message for a code the catalog does not know", () => {
    expect(detailText({ code: "not.in.catalog", message: "Server 'pz-1' not found", params: { key: "pz-1" } })).toBe(
      "Server 'pz-1' not found",
    );
  });
});
