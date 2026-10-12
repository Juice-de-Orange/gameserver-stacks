// Settings → Games (admin): which games this installation runs.
//
// Only active games get presets, version lists, workshop jobs and downloads
// (`backend/app/services/game_settings.py`). A game that still has an enabled server cannot be
// switched off: the backend answers 409 `games.has_servers` with the server keys, and the text
// is shown on that game's row instead of as a toast that disappears.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Gamepad2 } from "lucide-react";
import { api, errText } from "../api";
import { useToast } from "../components/feedback";
import { GameEmblem } from "../components/game/GameEmblem";
import { Alert, Card, SectionTitle, Skeleton, Toggle } from "../components/ui";
import { useGameSettings } from "../hooks/useGameSettings";
import { GAMES, GAME_LABEL, GameSettingsResponse, GameType } from "../lib/game";

export default function SettingsGames() {
  const { t } = useTranslation("settingsGames");
  const toast = useToast();
  const { games, setGames, error } = useGameSettings();
  const [busy, setBusy] = useState<GameType | null>(null);
  const [rowError, setRowError] = useState<Partial<Record<GameType, string>>>({});

  async function toggle(game: GameType, active: boolean) {
    setBusy(game);
    setRowError((old) => ({ ...old, [game]: undefined }));
    try {
      const res = await api("/settings/games", { method: "PUT", body: JSON.stringify({ games: { [game]: active } }) });
      if (!res.ok) {
        const text = await errText(res, t("saveFailed"));
        setRowError((old) => ({ ...old, [game]: text }));
        return;
      }
      setGames(((await res.json()) as GameSettingsResponse).games);
      toast.success(active ? t("activated", { game: GAME_LABEL[game] }) : t("deactivated", { game: GAME_LABEL[game] }));
    } catch (e) {
      setRowError((old) => ({ ...old, [game]: (e as Error).message }));
    } finally {
      setBusy(null);
    }
  }

  const active = new Set((games ?? []).filter((g) => g.active).map((g) => g.game_type));

  return (
    <div className="space-y-5">
      <SectionTitle icon={Gamepad2} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {error && <Alert kind="error">{error}</Alert>}
      {!games && !error && <Skeleton className="h-48" />}
      {games && (
        <Card className="space-y-1">
          <p className="pb-2 text-sm text-fg-muted">{t("intro")}</p>
          {active.size === 0 && <Alert kind="warn">{t("none")}</Alert>}
          <ul className="divide-y divide-line">
            {GAMES.map((game) => (
              <li key={game} data-game={game} className="space-y-2 py-3">
                <div className="flex items-center gap-3">
                  <span
                    className="grid h-9 w-9 shrink-0 place-items-center border border-primary/30 bg-primary/[0.12] text-primary"
                    style={{ borderRadius: "var(--g-radius)" }}
                  >
                    <GameEmblem game={game} className="h-5 w-5" strokeWidth={2} />
                  </span>
                  <span className="flex-1 font-semibold text-fg">{GAME_LABEL[game]}</span>
                  <Toggle
                    checked={active.has(game)}
                    label={active.has(game) ? t("state.active") : t("state.inactive")}
                    title={t("toggle", { game: GAME_LABEL[game] })}
                    onChange={(v) => {
                      if (busy === null) void toggle(game, v);
                    }}
                  />
                </div>
                {rowError[game] && <Alert kind="error">{rowError[game]}</Alert>}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
