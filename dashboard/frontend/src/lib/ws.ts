import { api } from "../api";

/**
 * Build the WebSocket URL for an API WS path: protocol matching http/https, plus a single-use
 * ticket as query parameter (the WS endpoints read `?ticket=`).
 * Shared by the console, the logs and the live dock.
 *
 * A browser cannot set an `Authorization` header on a WebSocket, so a credential has to go into
 * the URL. The access token must not: URLs end up in proxy and access logs. The ticket from
 * `POST /api/auth/ws-ticket` is valid for seconds and for one connection only, so it is fetched
 * anew for every connection attempt, right before connecting.
 *
 * Never rejects. If no ticket can be had (signed out, network down) the URL carries none and the
 * server closes the socket with 4401 — callers handle that in `onclose` like any other failed
 * attempt, so there is exactly one reconnect path.
 *
 * The separator is chosen to fit: if the path already has a query (e.g. `/ws/console?server=mc`)
 * the ticket is appended with `&` instead of `?`.
 */
export async function wsUrl(path: string): Promise<string> {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const base = `${proto}://${location.host}${path}`;
  let ticket = "";
  try {
    const res = await api("/auth/ws-ticket", { method: "POST" });
    if (res.ok) ticket = String((await res.json()).ticket ?? "");
  } catch {
    /* connect without a ticket; the server answers 4401 */
  }
  if (!ticket) return base;
  const sep = path.includes("?") ? "&" : "?";
  return `${base}${sep}ticket=${encodeURIComponent(ticket)}`;
}
