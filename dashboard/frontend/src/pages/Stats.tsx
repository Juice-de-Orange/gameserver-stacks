import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Activity, BarChart3, Link2, Skull, Swords, Trophy, TrendingUp, X } from "lucide-react";
import { api, apiJson } from "../api";
import { useAuth } from "../auth";
import { newFreshnessGuard } from "../lib/freshness";
import { useConfirm, useToast } from "../components/feedback";
import {
  Alert,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  GhostButton,
  Input,
  SectionTitle,
  Select,
  cx,
} from "../components/ui";
import { Heatmap } from "../components/Heatmap";
import { DeathMap, type Death } from "../components/DeathMap";
import { Achievements } from "../components/Achievements";
import { GAME_LABEL, type GameType } from "../lib/game";
import { fmtHm, fmtDateTime, fmtRelative } from "../lib/format";

// `label` is a catalog key: translated when rendering, not at import.
const EVENT = {
  death: { label: "event.death", icon: Skull, tone: "text-danger-text" },
  kill: { label: "event.kill", icon: Swords, tone: "text-warning-text" },
  level_up: { label: "event.levelUp", icon: TrendingUp, tone: "text-success-text" },
} as const;

interface SummaryRow {
  username: string;
  online?: boolean;
  total_seconds: number;
  sessions: number;
  avg_session: number;
  longest_session: number;
  days_active: number;
  last_seen: string;
}

function Board({ title, icon: Icon, entries, fmt }: any) {
  return (
    <div className="rounded-md border border-line-soft bg-surface-card/50 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-fg-dim">
        <Icon className="h-4 w-4" /> {title}
      </div>
      <ol className="space-y-1 text-sm">
        {entries?.length ? (
          entries.map((e: any, i: number) => (
            <li key={e.username} className="flex items-center justify-between gap-2">
              <span className="truncate">
                <span className="text-fg-dim">{i + 1}.</span> <span className="text-fg">{e.username}</span>
              </span>
              <span className="font-mono text-success-text">{fmt(e.value)}</span>
            </li>
          ))
        ) : (
          <li className="text-fg-dim">—</li>
        )}
      </ol>
    </div>
  );
}

export default function Stats() {
  const { t } = useTranslation("stats");
  const { me } = useAuth();
  const toast = useToast();
  const admin = me?.role === "admin";
  const confirm = useConfirm();
  const [summary, setSummary] = useState<SummaryRow[]>([]);
  const [online, setOnline] = useState<any[]>([]);
  const [myLinks, setMyLinks] = useState<any[]>([]);
  const [allLinks, setAllLinks] = useState<any[]>([]);
  const [claimName, setClaimName] = useState("");
  // A visible choice instead of the backend default "pz": without it every player of another
  // game gets a PZ-scoped link and never sees their statistics (same as in MyStats.tsx).
  const [claimGame, setClaimGame] = useState("pz");
  const [detail, setDetail] = useState<any | null>(null);
  const [detailName, setDetailName] = useState("");
  const [board, setBoard] = useState<any | null>(null);
  const [feed, setFeed] = useState<any[]>([]);
  const [deaths, setDeaths] = useState<Death[]>([]);
  const [err, setErr] = useState("");
  const [serverKey, setServerKey] = useState(""); // "" = all servers; otherwise cleanly per server
  const [servers, setServers] = useState<{ key: string; display_name: string }[]>([]);

  const q = serverKey ? `?server=${encodeURIComponent(serverKey)}` : "";

  // Changing the filter is an action of its own: everything that set off before belongs to the
  // previous scope. Without this guard a late response of the OLD filter would write its numbers
  // into the view of the new one. Rule and reasoning: `lib/freshness.ts`.
  const fresh = useRef(newFreshnessGuard()).current;

  async function load() {
    const mark = fresh.begin();
    try {
      const [s, o, b, f, d, ml] = await Promise.all([
        apiJson<any>(`/stats/summary${q}`),
        apiJson<any>(`/stats/online${q}`),
        apiJson<any>(`/stats/leaderboards${q}`),
        apiJson<any>(`/stats/feed${q}`),
        apiJson<{ deaths: Death[] }>(`/stats/deaths${q}`),
        apiJson<any>("/links/me"),
      ]);
      const al = admin ? await apiJson<any>("/links") : null;
      if (!fresh.valid(mark)) return;   // the filter changed in the meantime
      setSummary(s);
      setOnline(o);
      setBoard(b);
      setFeed(f);
      setDeaths(d.deaths);
      setMyLinks(ml);
      if (al) setAllLinks(al);
      setErr("");
    } catch (e: any) {
      if (fresh.valid(mark)) setErr(e.message);
    }
  }
  useEffect(() => {
    // Clear the previous scope's numbers first, then load. Otherwise the page shows foreign
    // numbers under the new heading after switching.
    fresh.invalidate();
    // Empty values of the RIGHT type, not `null`: `summary`, `online` and `feed` are arrays and
    // are iterated with `.map` when rendering. `strict` is off outside `src/lib` — nothing would
    // flag a `null` here, and it would tear the page down on the next render.
    setSummary([]);
    setOnline([]);
    setBoard(null);
    setFeed([]);
    setDeaths([]);
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);
  useEffect(() => {
    // Server list for the filter (metadata only). Errors are ignored → the filter stays hidden.
    apiJson<{ key: string; display_name: string }[]>("/servers")
      .then(setServers)
      .catch(() => {});
  }, []);

  const detailReq = useRef("");
  async function openDetail(name: string) {
    setDetailName(name);
    setDetail(null);
    detailReq.current = name;
    try {
      const d = await apiJson(`/stats/player/${encodeURIComponent(name)}${q}`);
      // Out-of-order guard: only accept when no other player was requested since the click
      // (two quick clicks could otherwise show A's data under B's name).
      if (detailReq.current === name) setDetail(d);
    } catch (e: any) {
      if (detailReq.current === name) toast.error(e.message);
    }
  }

  async function claim() {
    if (!claimName.trim()) return;
    try {
      // `apiJson` rather than `api`: `api` does not throw, so a 401/403/500 would clear the
      // typed name and report success although nothing was created.
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

  /**
   * The only authorisation check of the whole flow.
   *
   * `claim` accepts any typed name, `verify` sets the flag — and `GET /api/me/stats` then
   * filters by the confirmed names alone. A wrong approval therefore opens someone else's
   * playtime, activity heatmap and death history on the "my stats" page, which every signed-in
   * user reaches without any right. The backend additionally rejects a double assignment
   * with a 409.
   */
  async function verify(l: any) {
    const ok = await confirm({
      title: t("verify.title", { name: l.ingame_username, account: l.user?.display_name ?? `#${l.user_id}` }),
      body:
        t("verify.body", {
          account: l.user?.display_name ?? t("account", { id: l.user_id }),
          game: GAME_LABEL[l.game as GameType] ?? l.game,
        }) +
        (l.conflict
          ? ` ${t("verify.conflict", { account: l.conflict.user?.display_name ?? `#${l.conflict.user_id}` })}`
          : ""),
      danger: !!l.conflict,
      confirmLabel: t("common:action.confirm"),
    });
    if (!ok) return;
    try {
      await apiJson(`/links/${l.id}/verify`, { method: "POST" });
      load();
    } catch (e: any) {
      // A 404 or 409 from the backend must be shown — otherwise nothing visible happens.
      toast.error(e.message);
    }
  }

  async function remove(l: any) {
    const ok = await confirm({
      title: t("remove.title", { name: l.ingame_username }),
      body: t("remove.body", { account: l.user?.display_name ?? `#${l.user_id}` }),
      danger: true,
      confirmLabel: t("common:action.delete"),
    });
    if (!ok) return;
    try {
      await apiJson(`/links/${l.id}`, { method: "DELETE" });
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={BarChart3}
        sub={t("subtitle")}
        right={
          servers.length > 1 ? (
            <select
              value={serverKey}
              onChange={(e) => {
                setServerKey(e.target.value);
                setDetailName(""); // an open detail belongs to the old server scope
                setDetail(null);
              }}
              className="rounded-md border border-line bg-surface-card px-2 py-1.5 text-sm text-fg"
              title={t("filter.hint")}
            >
              <option value="">{t("filter.all")}</option>
              {servers.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.display_name || s.key}
                </option>
              ))}
            </select>
          ) : undefined
        }
      >
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      <Card className="!p-4">
        <div className="flex items-center gap-2 text-sm">
          <span className="h-2.5 w-2.5 rounded-full bg-success animate-live" />
          <span className="text-fg-muted">{t("online.label", { n: online.length })}</span>
          <span className="text-fg">{online.map((o) => o.username).join(", ") || t("online.nobody")}</span>
        </div>
      </Card>

      <DataTable<SummaryRow>
        rows={summary}
        rowKey={(s) => s.username}
        onRowClick={(s) => openDetail(s.username)}
        empty={
          <Card>
            <p className="text-sm text-fg-dim">
              {t("empty")}
            </p>
          </Card>
        }
        columns={[
          {
            header: t("column.player"),
            cell: (s) => (
              <span className="font-medium text-fg">
                {s.username} {s.online && <span className="text-success-text">●</span>}
              </span>
            ),
          },
          { header: t("summary.playtime"), cell: (s) => fmtHm(s.total_seconds) },
          { header: t("summary.sessions"), cell: (s) => s.sessions },
          { header: t("summary.avgSession"), cell: (s) => fmtHm(s.avg_session), className: "text-fg-muted" },
          { header: t("column.longest"), cell: (s) => fmtHm(s.longest_session), className: "text-fg-muted" },
          { header: t("summary.daysActive"), cell: (s) => s.days_active, className: "text-fg-muted" },
          { header: t("column.lastSeen"), cell: (s) => fmtDateTime(s.last_seen, true), className: "text-fg-dim" },
        ]}
      />

      {board && (board.playtime?.length > 0 || board.deaths?.length > 0) && (
        <Card className="space-y-3">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Trophy className="h-4 w-4 text-warning-text" /> {t("board.title")}
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Board title={t("summary.playtime")} icon={BarChart3} entries={board.playtime} fmt={fmtHm} />
            <Board title={t("board.longestSession")} icon={Activity} entries={board.longest_session} fmt={fmtHm} />
            <Board title={t("summary.daysActive")} icon={BarChart3} entries={board.active_days} fmt={(v: number) => `${v}`} />
            <Board title={t("board.deaths")} icon={Skull} entries={board.deaths} fmt={(v: number) => `${v}`} />
          </div>
        </Card>
      )}

      {deaths.length > 0 && (
        <Card className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Skull className="h-4 w-4 text-danger-text" /> {t("deaths.title")}
          </div>
          <DeathMap deaths={deaths} />
        </Card>
      )}

      {feed.length > 0 && (
        <Card className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Skull className="h-4 w-4 text-danger-text" /> {t("feed.title")}
          </div>
          <div className="space-y-1">
            {feed.map((e: any, i: number) => {
              const meta = (EVENT as any)[e.kind];
              const Icon = meta?.icon ?? Activity;
              return (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <Icon className={cx("h-4 w-4", meta?.tone ?? "text-fg")} />
                  <span className="text-fg">{e.username}</span>
                  <span className="text-fg-dim">{meta ? t(meta.label) : e.kind}</span>
                  <span className="ml-auto text-fg-dim">{fmtRelative(e.at, true)}</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Player detail */}
      {detailName && (
        <Card className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-stencil text-lg text-fg">{detailName}</h2>
            <button onClick={() => setDetailName("")} aria-label={t("detail.close")} className="text-fg-dim hover:text-fg">
              <X className="h-5 w-5" />
            </button>
          </div>
          {!detail ? (
            <p className="text-fg-dim">{t("common:state.loading")}</p>
          ) : (
            <>
              {detail.summary && (
                <div className="grid gap-2 sm:grid-cols-4">
                  {[
                    [t("summary.playtime"), fmtHm(detail.summary.total_seconds)],
                    [t("summary.sessions"), detail.summary.sessions],
                    [t("summary.avgSession"), fmtHm(detail.summary.avg_session)],
                    [t("summary.daysActive"), detail.summary.days_active],
                  ].map(([l, v]) => (
                    <div key={l as string} className="rounded-md border border-line-soft bg-surface-card/50 p-2.5">
                      <div className="text-xs uppercase tracking-wide text-fg-dim">{l}</div>
                      <div className="font-mono text-lg text-fg">{v as any}</div>
                    </div>
                  ))}
                </div>
              )}

              {detail.achievements?.length > 0 && <Achievements items={detail.achievements} />}

              {detail.heatmap?.grid && (
                <div>
                  <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
                    <Activity className="h-4 w-4" /> {t("activity.title")}
                  </div>
                  <Heatmap grid={detail.heatmap.grid} />
                </div>
              )}

              {detail.events?.length > 0 && (
                <div>
                  <div className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
                    {t("events.title")}
                  </div>
                  <div className="space-y-1">
                    {detail.events.map((e: any, i: number) => {
                      const meta = (EVENT as any)[e.kind];
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
            </>
          )}
        </Card>
      )}

      {/* Account linking */}
      <Card className="space-y-2">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          <Link2 className="h-4 w-4" /> {t("link.title")}
        </div>
        <div className="flex gap-2">
          <Input value={claimName} onChange={(e) => setClaimName(e.target.value)} placeholder={t("link.name")} />
          <Select value={claimGame} onChange={(e) => setClaimGame(e.target.value)} aria-label={t("link.game")}>
            {(Object.keys(GAME_LABEL) as GameType[]).map((g) => (
              <option key={g} value={g}>
                {GAME_LABEL[g]}
              </option>
            ))}
          </Select>
          <Button onClick={claim}>{t("link.submit")}</Button>
        </div>
        {myLinks.map((l) => (
          <div key={l.id} className="text-sm">
            {l.ingame_username} —{" "}
            {l.verified ? (
              <Badge tone="online">{t("link.verified")}</Badge>
            ) : (
              <Badge tone="warn">{t("link.pending")}</Badge>
            )}
          </div>
        ))}
      </Card>

      {admin && (
        <Card className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            {t("links.title")}
          </div>
          {allLinks.map((l) => (
            <div key={l.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="text-fg-muted">
                {/* Name the account, not just "#7": this approval is the only check in the flow,
                    and resolving an id would need the users page — another right. */}
                <span className="text-fg">{l.user?.display_name ?? t("account", { id: l.user_id })}</span>
                {l.user?.username && <span className="text-fg-dim"> @{l.user.username}</span>} →{" "}
                <span className="text-fg">{l.ingame_username}</span>{" "}
                <span className="text-fg-dim">({GAME_LABEL[l.game as GameType] ?? l.game})</span>{" "}
                {l.verified && <span className="text-success-text">✓</span>}
                {l.conflict && (
                  <span className="text-warning-text">
                    {" "}
                    {t("links.conflict", { account: l.conflict.user?.display_name ?? `#${l.conflict.user_id}` })}
                  </span>
                )}
              </span>
              <span className="flex gap-2">
                {!l.verified && <GhostButton onClick={() => verify(l)}>{t("common:action.confirm")}</GhostButton>}
                <GhostButton onClick={() => remove(l)}>{t("common:action.delete")}</GhostButton>
              </span>
            </div>
          ))}
          {allLinks.length === 0 && <p className="text-sm text-fg-dim">{t("links.empty")}</p>}
        </Card>
      )}
    </div>
  );
}
