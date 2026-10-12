/**
 * Foreign URLs, before they go into an `href`.
 *
 * A URL from a foreign source must never be set as `href` without checking its scheme. The
 * sharpest case is `Tickets.tsx`: `workshop_url` comes from the "request a mod" form, which ANY
 * signed-in user can fill in, and the backend stores it verbatim. React sets `javascript:…` as
 * `href` without complaint; a click runs it in the context of the signed-in session — and the
 * reader is an admin who is to decide on the request. The access token lives in `localStorage`,
 * so that is the path from "may write tickets" to "is admin".
 *
 * The other cases are the same class with calmer sources: the changelog URL from the Steam news
 * API (stored verbatim by the backend), the collection and community links from the `.env` and
 * the mod links of the join page. The difference is not visible at the call site, which is why
 * all of them go through here.
 *
 * `rel="noreferrer noopener"` protects against tabnabbing, not against a foreign scheme.
 */

/** Only `http:` and `https:` — everything else becomes `undefined`, which makes the link inert. */
export function safeUrl(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  const raw = url.trim();
  // Relative paths are harmless and come without a scheme — `new URL` would need a base for them.
  if (raw.startsWith("/") && !raw.startsWith("//")) return raw;
  try {
    const proto = new URL(raw).protocol;
    return proto === "http:" || proto === "https:" ? raw : undefined;
  } catch {
    // Not a parseable absolute link (depending on the browser "javascript:alert(1)" without a
    // host ends up here too). When in doubt leave it out — a missing link is harmless, a wrong
    // one is not.
    return undefined;
  }
}
