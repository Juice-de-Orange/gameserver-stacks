import { useCallback, useEffect, useState } from "react";
import { apiJson } from "../api";
import { GameSetting, GameSettingsResponse } from "../lib/game";

/**
 * Loads `GET /api/settings/games` once: which games this installation runs. `games` stays `null`
 * until the first answer, so a caller can tell "not loaded yet" from "no game active".
 * `setGames` takes the answer of a `PUT`, which returns the full list as well.
 */
export function useGameSettings(enabled = true) {
  const [games, setGames] = useState<GameSetting[] | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const d = await apiJson<GameSettingsResponse>("/settings/games");
      setGames(d.games);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (enabled) void reload();
  }, [enabled, reload]);

  return { games, setGames, error, reload };
}
