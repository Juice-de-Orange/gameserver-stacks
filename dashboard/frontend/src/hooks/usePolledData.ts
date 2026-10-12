import { useEffect, useRef, useState } from "react";
import { apiJson } from "../api";
import i18n from "../i18n";

/**
 * Loads `path` once and then again every `intervalMs`. Replaces the useEffect + setInterval +
 * fetch pattern. `path = null` pauses. `setData` is handed out so that pages can update
 * optimistically.
 */
export function usePolledData<T = unknown>(path: string | null, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const pathRef = useRef(path);
  pathRef.current = path;

  useEffect(() => {
    // DISCARD the old data when the path changes. Otherwise the payload of the previous server
    // stays if the first request for the new one fails — `tick()` only sets `error` in its catch
    // and leaves `data` alone. On the join page that meant: address, port, one-click URI and mod
    // list still belonged to the OLD server, recognisable only by a red banner above them.
    setData(null);
    setError("");
    setLoading(true);
    if (!path) return;
    let alive = true;
    async function tick() {
      try {
        const d = await apiJson(pathRef.current as string);
        if (alive) {
          setData(d as T);
          setError("");
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : i18n.t("usePolledData:loadFailed"));
      } finally {
        if (alive) setLoading(false);
      }
    }
    void tick();
    const id = window.setInterval(tick, intervalMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [path, intervalMs]);

  return { data, error, loading, setData };
}
