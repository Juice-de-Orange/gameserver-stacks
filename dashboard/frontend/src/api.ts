// Thin API client: attaches the access token and renews it on a 401 via the refresh token.
//
// Both tokens live in `localStorage`. That makes the API immune to CSRF (nothing is sent
// automatically) and leaves script injection as the way to steal them — the Content-Security-
// Policy in the Caddyfile is the guard for that. See `docs/security.md`.
import i18n, { tb } from "./i18n";

const ACCESS = "gss_access";
const REFRESH = "gss_refresh";

export function getAccess(): string | null {
  return localStorage.getItem(ACCESS);
}
export function setTokens(access: string, refresh?: string): void {
  localStorage.setItem(ACCESS, access);
  if (refresh) localStorage.setItem(REFRESH, refresh);
}
export function clearTokens(): void {
  localStorage.removeItem(ACCESS);
  localStorage.removeItem(REFRESH);
}

async function tryRefresh(): Promise<boolean> {
  const refresh = localStorage.getItem(REFRESH);
  if (!refresh) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!res.ok) return false;
  const data = await res.json();
  setTokens(data.access_token);
  return true;
}

// `path` WITHOUT the /api prefix (added here). `body` may be a JSON string or URLSearchParams.
export async function api(path: string, opts: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(opts.headers || {});
  const token = getAccess();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (opts.body && typeof opts.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (res.status === 401 && retry && (await tryRefresh())) {
    return api(path, opts, false);
  }
  return res;
}

/**
 * Turn a FastAPI error payload into text — reliably, even if it is not text.
 *
 * `detail` is not always a string here. For `POST /api/servers/{key}/{start,restart}` the
 * backend returns an object: the RAM budget with `deficit_mb`/`stop_candidates`, the mod boot
 * gate with `blockers`. The same shape comes from `version_manager`, `mods/apply` and
 * `mods/foreign_apply` under the key `error`.
 *
 * Passed on unchecked, that becomes `[object Object]` (wherever a `new Error(...)` sits in
 * between) or a blank page (where it lands directly in JSX). This is the one place that
 * handles it.
 */
export function detailText(detail: unknown, fallback = ""): string {
  if (typeof detail === "string") return detail || fallback;
  if (detail === null || detail === undefined) return fallback;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => detailText(d)).filter(Boolean);
    return parts.length ? parts.join(" · ") : fallback;
  }
  if (typeof detail === "object") {
    const o = detail as Record<string, unknown>;
    // The backend's own errors: `{code, message, params}`. The code selects the translation,
    // the English `message` is the fallback for a code this build has no text for.
    if (typeof o.code === "string" && o.code) {
      const params = o.params && typeof o.params === "object" ? (o.params as Record<string, unknown>) : {};
      const message = typeof o.message === "string" && o.message ? o.message : o.code;
      return i18n.t(`api:${o.code}`, { ...params, defaultValue: message });
    }
    // Ordered by frequency in the backend: `message` (budget, boot gate), `error` (mods,
    // versions). `detail` catches a response that was wrapped twice by accident.
    // "The server is now OFF" after a failed restart (`lifecycle.ensure_restart`) travels as a
    // field of its own next to the reason; a coded detail already carries it in its message.
    const warning = typeof o.restart_warning === "string" && o.restart_warning ? o.restart_warning : "";
    for (const field of ["message", "error", "detail"]) {
      const value = o[field];
      if (typeof value === "string" && value) return warning ? `${value} | ${warning}` : value;
    }
    try {
      // Raw, but true. Better than an invented euphemism — and far better than a blank page.
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}

/**
 * A note or summary the backend sends with a result: a fixed sentence (translated by its English
 * text, `tb`), a coded one with values in it (`errors.note` — translated by its code), or a list of
 * them.
 */
export function noteText(note: unknown): string {
  if (typeof note === "string") return tb(note);
  if (Array.isArray(note)) return note.map(noteText).filter(Boolean).join(" ");
  return detailText(note);
}

/** Read the error text from a response (the body is read exactly once). */
export async function errText(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return detailText((body as { detail?: unknown })?.detail, fallback);
}

export async function apiJson<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await api(path, opts);
  if (!res.ok) {
    throw new Error(await errText(res, res.statusText));
  }
  return res.json() as Promise<T>;
}
