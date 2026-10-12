// Empty state for every place that needs a server when the registry has no enabled one yet.
// A fresh installation lands here first, so it has to say how to go on — not show a "not found".
//
// Two steps for an admin: first "which games do you want to run?" (nothing is active on a fresh
// installation unless `GSS_GAMES` named games, `backend/app/services/game_settings.py`), then
// "add your first server". Without an active game the add-server page would offer nothing.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Gamepad2, Plus, Server } from "lucide-react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import { useGameSettings } from "../hooks/useGameSettings";
import { GAMES, GAME_LABEL, GameSettingsResponse, GameType, activeGames } from "../lib/game";
import { GameEmblem } from "./game/GameEmblem";
import { Alert, Button, EmptyState, Spinner, cx } from "./ui";

export function NoServer() {
  const { t } = useTranslation("noServer");
  const { me } = useAuth();
  const admin = me?.role === "admin";
  const { games, setGames, error } = useGameSettings(admin);

  if (admin && !games && !error)
    return (
      <EmptyState icon={Server}>
        <Spinner className="h-5 w-5" />
      </EmptyState>
    );
  // A failed load does not lock the admin out: the add-server page explains itself.
  if (admin && games && activeGames(games).length === 0) return <ChooseGames onSaved={setGames} />;

  return (
    <EmptyState icon={Server}>
      <div className="space-y-3">
        <div className="font-semibold text-fg">{t("title")}</div>
        <div>{admin ? t("admin") : t("member")}</div>
        {admin && (
          <div className="flex flex-wrap items-center justify-center gap-3">
            <Link to="/servers/new" className="inline-block">
              <Button>
                <Plus className="h-4 w-4" /> {t("addFirst")}
              </Button>
            </Link>
            <Link to="/settings/games" className="text-sm text-primary-text underline-offset-2 hover:underline">
              {t("games.change")}
            </Link>
          </div>
        )}
      </div>
    </EmptyState>
  );
}

/** First-run step: pick the games, save them in one request. */
function ChooseGames({ onSaved }: { onSaved: (games: GameSettingsResponse["games"]) => void }) {
  const { t } = useTranslation("noServer");
  const [picked, setPicked] = useState<GameType[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const flip = (game: GameType) =>
    setPicked((old) => (old.includes(game) ? old.filter((g) => g !== game) : [...old, game]));

  async function save() {
    setBusy(true);
    setError("");
    try {
      const body = { games: Object.fromEntries(picked.map((g) => [g, true])) };
      const res = await api("/settings/games", { method: "PUT", body: JSON.stringify(body) });
      if (!res.ok) {
        setError(await errText(res, t("games.saveFailed")));
        return;
      }
      onSaved(((await res.json()) as GameSettingsResponse).games);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <EmptyState icon={Gamepad2}>
      <div className="space-y-3">
        <div className="font-semibold text-fg">{t("games.title")}</div>
        <div>{t("games.body")}</div>
        <div className="grid gap-2 text-left sm:grid-cols-2">
          {GAMES.map((game) => (
            <button
              key={game}
              type="button"
              data-game={game}
              aria-pressed={picked.includes(game)}
              onClick={() => flip(game)}
              className={cx(
                "flex items-center gap-3 rounded-xl border px-3 py-2.5 transition",
                picked.includes(game) ? "border-primary/50 bg-primary-soft" : "border-line hover:bg-surface-sunken",
              )}
            >
              <span
                className="grid h-8 w-8 shrink-0 place-items-center border border-primary/30 bg-primary/[0.12] text-primary"
                style={{ borderRadius: "var(--g-radius)" }}
              >
                <GameEmblem game={game} className="h-4 w-4" strokeWidth={2} />
              </span>
              <span className="font-semibold text-fg">{GAME_LABEL[game]}</span>
            </button>
          ))}
        </div>
        {error && <Alert kind="error">{error}</Alert>}
        <Button onClick={save} disabled={busy || picked.length === 0}>
          {t("games.continue")}
        </Button>
        <div className="text-xs text-fg-dim">{t("games.later")}</div>
      </div>
    </EmptyState>
  );
}
