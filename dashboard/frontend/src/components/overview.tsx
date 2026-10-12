import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { apiJson } from "../api";
import { useAuth } from "../auth";
import type { Connect } from "./ConnectAddress";
import { useActiveServerKey } from "./ServerContext";

export interface Overview {
  container: { running?: boolean; status?: string; health?: string | null; exists?: boolean };
  uptime_seconds: number | null;
  stats: {
    mem_mib?: number;
    mem_bytes?: number;
    mem_limit?: number;
    mem_percent?: number | null;
    mem_max_heap?: number | null; // -Xmx ceiling of the game server (bytes), information only
    mem_container_limit?: number | null; // the container's memory limit (bytes), null = none
    cpu_percent?: number;
    cpu_count?: number;
  };
  players: { count: number; names: string[] } | null;
  rcon: { reachable: boolean; latency_ms: number | null };
  max_players: number | null;
  mods_count: number | null;
  last_backup: { name: string; size: number; created: string } | null;
  next_backup: string | null;
  next_restart: string | null;
  disk: { free: number; total: number; used: number; percent: number | null } | null;
  pending_registrations: number | null;
  open_tickets: number | null;
  last_incident: { at: string; kind: string; detail: string } | null;
  mod_health: { updates: number; errors: number; warnings: number; ok: boolean } | null;
  /** Where players connect — the same source as the fleet and the join page. */
  connect?: Connect | null;
}

interface Ctx {
  data: Overview | null;
  error: string;
  refresh: () => void;
}

const OverviewCtx = createContext<Ctx>({ data: null, error: "", refresh: () => {} });
export const useOverview = () => useContext(OverviewCtx);

/** One shared poller for the status strip and the status page (saves RCON round trips). */
export function OverviewProvider({ children }: { children: ReactNode }) {
  const { me, can } = useAuth();
  const allowed = !!me && can("can_view_status");
  const key = useActiveServerKey(); // the status strip and the status page follow the ACTIVE server
  // Kept with the key it belongs to: after a switch to another server the previous server's
  // overview must not stand in for the new one until its first answer arrives.
  const [answer, setAnswer] = useState<{ key: string; data: Overview } | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!allowed || !key) return;
    try {
      setAnswer({ key, data: await apiJson<Overview>(`/servers/${key}/overview`) });
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }, [allowed, key]);

  useEffect(() => {
    // No enabled server yet: nothing to poll. Asking anyway would put a "not found" into every
    // page that shows the overview error.
    if (!allowed || !key) {
      setAnswer(null);
      setError("");
      return;
    }
    refresh();
    const t = setInterval(refresh, 12000);
    return () => clearInterval(t);
  }, [allowed, key, refresh]);

  // `null` until the active server's first answer: "not known yet", never "stopped".
  const data = answer && answer.key === key ? answer.data : null;
  return <OverviewCtx.Provider value={{ data, error, refresh }}>{children}</OverviewCtx.Provider>;
}
