/// <reference types="vite/client" />
// Translations. English is the source language and the fallback; German is the second catalog.
//
// Layout: `locales/<language>/<namespace>.json`, flat keys, `{{name}}` placeholders. Two groups
// of files are merged into one namespace each, split only so that they stay readable:
//   `api-*.json`     → `api`:     texts for the codes the backend sends (`{code, message,
//                                 params}`, see `backend/app/errors.py`), keyed by code;
//   `backend-*.json` → `backend`: fixed English texts the backend sends as plain strings
//                                 (labels, help texts), keyed by that English text — see `tb`.
//
// All catalogs are bundled; nothing is fetched at runtime, so the first render is already
// translated and the Content-Security-Policy needs no exception.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export const LANGUAGES = ["en", "de"] as const;
export type Language = (typeof LANGUAGES)[number];

const STORAGE_KEY = "gss_lang";

type Catalog = Record<string, string>;

const files = import.meta.glob("./locales/*/*.json", { eager: true, import: "default" }) as Record<
  string,
  Catalog
>;

/** `{ en: { common: {...}, api: {...} }, de: {...} }` from the bundled files. */
export function buildResources(source: Record<string, Catalog> = files) {
  const resources: Record<string, Record<string, Catalog>> = {};
  for (const [path, catalog] of Object.entries(source)) {
    const match = /\/locales\/([^/]+)\/([^/]+)\.json$/.exec(path);
    const language = match?.[1];
    const file = match?.[2];
    if (!language || !file) continue;
    const namespace = file.startsWith("api-") ? "api" : file.startsWith("backend-") ? "backend" : file;
    const perLanguage = (resources[language] ??= {});
    perLanguage[namespace] = { ...perLanguage[namespace], ...catalog };
  }
  return resources;
}

function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (LANGUAGES as readonly string[]).includes(value);
}

/** Stored choice first, then the browser's preference list, then English. */
export function detectLanguage(): Language {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (isLanguage(saved)) return saved;
  } catch {
    /* localStorage may be blocked — fall through to the browser preference */
  }
  const preferred = typeof navigator !== "undefined" ? (navigator.languages ?? [navigator.language]) : [];
  for (const tag of preferred) {
    const base = String(tag).toLowerCase().split("-")[0];
    if (isLanguage(base)) return base;
  }
  return "en";
}

export function currentLanguage(): Language {
  return isLanguage(i18n.language) ? i18n.language : "en";
}

export function setLanguage(language: Language): void {
  try {
    localStorage.setItem(STORAGE_KEY, language);
  } catch {
    /* the choice still applies for this session */
  }
  void i18n.changeLanguage(language);
}

const resources = buildResources();

void i18n.use(initReactI18next).init({
  resources,
  lng: detectLanguage(),
  fallbackLng: "en",
  supportedLngs: [...LANGUAGES],
  ns: Object.keys(resources.en ?? {}),
  defaultNS: "common",
  // Flat catalogs: a dot is part of the key (`server.not_found`), not a path.
  keySeparator: false,
  nsSeparator: ":",
  // React escapes on render; escaping here as well would show `&amp;` to the user.
  interpolation: { escapeValue: false },
  // Bundled resources, so init can finish before the first render.
  initAsync: false,
  react: { useSuspense: false },
});

/**
 * Translate a fixed text the backend sent as a plain English string (a label, a help text).
 * The English text is the key; a text the catalog does not know — anything with a value
 * built into it — is shown as it came.
 */
export function tb(text: string): string;
export function tb(text: string | null | undefined): string | null | undefined;
export function tb(text: string | null | undefined): string | null | undefined {
  if (!text) return text;
  // Looked up, not translated-with-fallback: an unknown text must come back untouched, and `t`
  // would run interpolation and `$t(…)` nesting over it (mod names are user-influenced).
  if (!i18n.exists(text, { ns: "backend", nsSeparator: false })) return text;
  // `nsSeparator: false`: the key is a sentence and may contain a colon.
  return i18n.t(text, { ns: "backend", nsSeparator: false });
}

function syncDocumentLanguage(language: string): void {
  if (typeof document !== "undefined") document.documentElement.lang = language;
}
syncDocumentLanguage(i18n.language);
i18n.on("languageChanged", syncDocumentLanguage);

export default i18n;
