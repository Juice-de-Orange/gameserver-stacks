// Server registry context: loads GET /api/servers and provides the active server (from the
// route /s/:serverKey/…). Both providers sit ABOVE the routes so that the server list and the
// active selection survive navigation.
import {
  createContext,
  ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { matchPath, useLocation } from "react-router-dom";
import { apiJson } from "../api";
import { useAuth } from "../auth";
import { ServerInfo } from "../lib/game";

interface ServersValue {
  servers: ServerInfo[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  byKey: (key: string) => ServerInfo | undefined;
}

const ServersCtx = createContext<ServersValue>(null as unknown as ServersValue);
export const useServers = (): ServersValue => useContext(ServersCtx);

export function ServersProvider({ children }: { children: ReactNode }) {
  const { me, can } = useAuth();
  const [servers, setServers] = useState<ServerInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function reload(): Promise<void> {
    if (!me || !can("can_view_status")) {
      setServers([]);
      setLoading(false);
      return;
    }
    try {
      setServers(await apiJson<ServerInfo[]>("/servers"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me]);

  const value = useMemo<ServersValue>(
    () => ({
      servers,
      loading,
      error,
      reload,
      byKey: (key: string) => servers.find((s) => s.key === key),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [servers, loading, error],
  );

  return <ServersCtx.Provider value={value}>{children}</ServersCtx.Provider>;
}

/** The default server among the ENABLED ones: Project Zomboid with the lowest sort_order,
 * otherwise the first enabled server; `null` while there is none.
 *
 * Disabled rows never become the default. A new server is created disabled and has no
 * container yet; every `/api/servers/<key>/…` route answers 404 for it, so scoping a poller to it
 * would only produce errors. Its own page (`/s/<key>`) shows the next steps instead. */
function defaultServer(servers: ServerInfo[]): ServerInfo | null {
  const enabled = servers.filter((s) => s.enabled);
  const pz = enabled.filter((s) => s.game_type === "pz").sort((a, b) => a.sort_order - b.sort_order);
  return pz[0] ?? enabled[0] ?? null;
}

/** Key of the default server (see `defaultServer`). App-wide consumers without a server of
 * their own scope to it; `null` means "no server yet" — show an empty state, request nothing. */
export function useDefaultServerKey(): string | null {
  const { servers } = useServers();
  return defaultServer(servers)?.key ?? null;
}

// --- Active server: route OR last used (persisted) ---
const ActiveCtx = createContext<ServerInfo | null>(null);
export const useActiveServer = (): ServerInfo | null => useContext(ActiveCtx);

const LAST_SERVER_KEY = "gss_last_server";

/** The active server is STABLE: a `/s/:key` route wins and is remembered; on global pages
 * (overview, stats, …) the last used server stays instead of jumping to the default, so the
 * server never changes unnoticed.
 *
 * Only an ENABLED server can be active. A route to a disabled one gives `null` (not another
 * server: the page names this key), a remembered disabled one falls back to the default. */
export function ActiveServerProvider({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const { servers } = useServers();
  const m = matchPath("/s/:serverKey/*", loc.pathname);
  const routeKey = m?.params?.serverKey;

  const [remembered, setRemembered] = useState<string | null>(() => {
    try {
      return localStorage.getItem(LAST_SERVER_KEY);
    } catch {
      return null;
    }
  });

  // On a server route remember this server (state + localStorage).
  useEffect(() => {
    if (routeKey && routeKey !== remembered) {
      setRemembered(routeKey);
      try {
        localStorage.setItem(LAST_SERVER_KEY, routeKey);
      } catch {
        /* ignore */
      }
    }
  }, [routeKey, remembered]);

  const defaultPz = useMemo<ServerInfo | null>(() => defaultServer(servers), [servers]);

  const active = useMemo<ServerInfo | null>(() => {
    // 1) explicit route → 2) remembered server (if it still exists) → 3) default server.
    const wantKey = routeKey ?? remembered ?? undefined;
    if (wantKey) {
      const found = servers.find((s) => s.key === wantKey);
      if (found?.enabled) return found;
      if (found && routeKey) return null;
    }
    return defaultPz;
  }, [routeKey, remembered, servers, defaultPz]);

  return <ActiveCtx.Provider value={active}>{children}</ActiveCtx.Provider>;
}

/** Key of the active (remembered/route) server — for links and redirects. Falls back to the
 * default server; `null` while there is no enabled server. */
export function useActiveServerKey(): string | null {
  const active = useActiveServer();
  const def = useDefaultServerKey();
  return active?.key ?? def;
}
