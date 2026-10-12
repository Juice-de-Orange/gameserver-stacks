import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Activity, BarChart3, Link2, Skull, Swords, TrendingUp } from "lucide-react";
import { api, apiJson } from "../api";
import { useToast } from "../components/feedback";
import { Alert, Badge, Button, Card, EmptyState, Input, SectionTitle, Select, cx } from "../components/ui";
import { Heatmap } from "../components/Heatmap";
import { Achievements } from "../components/Achievements";
import { fmtHm, fmtDateTime } from "../lib/format";
import { useGameSettings } from "../hooks/useGameSettings";
import { GAMES, GAME_LABEL, activeGames } from "../lib/game";

// `label` is a catalog key: translated when rendering, not at import.
const EVENT: Record<string, { label: string; icon: any; tone: string }> = {
  death: { label: "event.death", icon: Skull, tone: "text-danger-text" },
  kill: { label: "event.kill", icon: Swords, tone: "text-warning-text" },
  level_up: { label: "event.levelUp", icon: TrendingUp, tone: "text-success-text" },
};

export default function MyStats() {
  const { t } = useTranslation("myStats");
  const toast = useToast();
  const [data, setData] = useState<any | null>(null);
  const [myLinks, setMyLinks] = useState<any[]>([]);
  const [claimName, setClaimName] = useState("");
  // The game MUST be sent along — the backend defaults to "pz" otherwise and scopes the
  // statistics to the PZ servers. A visible choice rather than derived from the active server:
  // names differ per game, and whoever is looking at Minecraft is not necessarily linking
  // their Minecraft name.
  const [claimGame, setClaimGame] = useState("pz");
  const [err, setErr] = useState("");
  // Only the games this installation runs. If the list cannot be loaded, all of them: a failed
  // request must not take away the only way to link an account.
  const { games, error: gamesError } = useGameSettings();
  const claimGames = games ? activeGames(games) : gamesError ? GAMES : [];
  useEffect(() => {
    if (claimGames.length && !claimGames.includes(claimGame as (typeof claimGames)[number])) setClaimGame(claimGames[0]!);
  }, [claimGames.join(), claimGame]);

  async function load() {
    try {
      setData(await apiJson("/me/stats"));
      setMyLinks(await apiJson("/links/me"));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function claim() {
    if (!claimName.trim()) return;
    // `apiJson` rather than `api`, so that a 4xx does not pass as success — and `apiJson`
    // THROWS, hence the `catch`: without it a failure would show nothing at all.
    try {
      await apiJson("/links/claim", {
        method: "POST",
        body: JSON.stringify({ ingame_username: claimName, game: claimGame }),
      });
      setClaimName("");
      toast.success(t("link.requested"));
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const players: any[] = data?.players || [];

  return (
    <div className="space-y-5">
      <SectionTitle icon={BarChart3} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      {/* Link */}
      <Card className="space-y-2">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          <Link2 className="h-4 w-4" /> {t("link.title")}
        </div>
        <p className="text-sm text-fg-muted">
          {t("link.intro")}
        </p>
        <div className="flex gap-2">
          <Input value={claimName} onChange={(e) => setClaimName(e.target.value)} placeholder={t("link.name")} />
          <Select value={claimGame} onChange={(e) => setClaimGame(e.target.value)} aria-label={t("link.game")}>
            {/* From the game list, not copied: with a hand-kept list here, players of a newly
                supported game could not link their account at all. */}
            {claimGames.map((value) => (
              <option key={value} value={value}>
                {GAME_LABEL[value]}
              </option>
            ))}
          </Select>
          <Button onClick={claim}>{t("link.submit")}</Button>
        </div>
        {myLinks.map((l) => (
          <div key={l.id} className="text-sm">
            {l.ingame_username} —{" "}
            {l.verified ? <Badge tone="online">{t("link.verified")}</Badge> : <Badge tone="warn">{t("link.pending")}</Badge>}
          </div>
        ))}
      </Card>

      {data && !data.linked && players.length === 0 && (
        <EmptyState icon={BarChart3}>
          {t("empty")}
        </EmptyState>
      )}

      {players.map((p) => (
        <Card key={p.name} className="space-y-4">
          <h2 className="text-stencil text-lg text-fg">{p.name}</h2>
          {p.summary && (
            <div className="grid gap-2 sm:grid-cols-4">
              {[
                [t("summary.playtime"), fmtHm(p.summary.total_seconds)],
                [t("summary.sessions"), p.summary.sessions],
                [t("summary.avgSession"), fmtHm(p.summary.avg_session)],
                [t("summary.daysActive"), p.summary.days_active],
              ].map(([l, v]) => (
                <div key={l as string} className="rounded-md border border-line-soft bg-surface-card/50 p-2.5">
                  <div className="text-xs uppercase tracking-wide text-fg-dim">{l}</div>
                  <div className="font-mono text-lg text-fg">{v as any}</div>
                </div>
              ))}
            </div>
          )}
          {p.achievements?.length > 0 && <Achievements items={p.achievements} />}
          {p.heatmap?.grid && (
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
                <Activity className="h-4 w-4" /> {t("activity.title")}
              </div>
              <Heatmap grid={p.heatmap.grid} />
            </div>
          )}
          {p.events?.length > 0 && (
            <div>
              <div className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{t("events.title")}</div>
              <div className="space-y-1">
                {p.events.map((e: any, i: number) => {
                  const meta = EVENT[e.kind];
                  const Icon = meta?.icon ?? Activity;
                  return (
                    <div key={i} className="flex items-center gap-2 text-sm">
                      <Icon className={cx("h-4 w-4", meta?.tone ?? "text-fg")} />
                      <span className="text-fg">{meta ? t(meta.label) : e.kind}</span>
                      <span className="text-fg-dim">{fmtDateTime(e.at, true)}</span>
                      {e.detail && <span className="text-fg-dim">· {e.detail}</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
