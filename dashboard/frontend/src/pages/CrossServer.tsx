// Cross-server page: presence, leaderboard, announcements, feature switches + live chat feed.
// Read access for everyone with can_view_status; switches/announcements need can_manage_cross_server.
import { FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Megaphone, Radio, Trophy, Users, Network } from "lucide-react";
import { api, apiJson, detailText } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/feedback";
import { Alert, Badge, Button, Card, Input, SectionTitle, Spinner, Toggle } from "../components/ui";
import { newFreshnessGuard } from "../lib/freshness";
import { newReconnect } from "../lib/reconnect";
import { wsUrl } from "../lib/ws";

interface CrossSettings {
  chat_bridge_enabled: boolean;
  presence_enabled: boolean;
  leaderboard_enabled: boolean;
  announcements_enabled: boolean;
  chat_format: string;
  event_format: string;
  come_over_format: string;
  rate_limit_per_min: number;
  come_over_cooldown_sec: number;
}
interface Presence {
  server_key: string | null;
  display_name: string;
  game: string | null;
  count: number;
  players: string[];
}
interface LbRow {
  player: string;
  playtime_hours: number;
  kills: number;
  deaths: number;
  games: Record<string, number>;
}
interface ChatLine {
  id: number;
  server_key: string;
  player: string;
  message: string;
  at: string;
}

export default function CrossServer() {
  const { t } = useTranslation("crossServer");
  const { can } = useAuth();
  const toast = useToast();
  const manage = can("can_manage_cross_server");
  const [settings, setSettings] = useState<CrossSettings | null>(null);
  const [presence, setPresence] = useState<Presence[]>([]);
  const [lb, setLb] = useState<LbRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const [busy, setBusy] = useState(false);
  const [chat, setChat] = useState<ChatLine[]>([]);
  const chatBox = useRef<HTMLDivElement>(null);

  // The poll must not overwrite a switch that was just saved. Rule and reasoning are in
  // `lib/freshness.ts`; the window is wide here because `setSettings` only runs once
  // `/cross/leaderboard?limit=15` has answered as well.
  const fresh = useRef(newFreshnessGuard()).current;

  const loadAll = async () => {
    const mark = fresh.begin();
    try {
      const [s, p, l] = await Promise.all([
        apiJson<CrossSettings>("/cross/settings"),
        apiJson<Presence[]>("/cross/presence"),
        apiJson<LbRow[]>("/cross/leaderboard?limit=15"),
      ]);
      // Nobody writes presence or the leaderboard from here — only the switches need the guard.
      if (fresh.valid(mark)) setSettings(s);
      setPresence(p);
      setLb(l);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live chat feed (also works with the bridge switched off: history/presence only then).
  useEffect(() => {
    let unmounted = false;
    // Rule and reasoning: `lib/reconnect.ts`. The backoff must not be reset on the first message —
    // a connection that sends an `init` message and then drops would reconnect once per second.
    const reconnect = newReconnect();
    let timer: ReturnType<typeof setTimeout>;
    let sock: WebSocket | null = null;
    const connect = async () => {
      reconnect.attemptStarted();
      const url = await wsUrl("/ws/cross-chat");
      if (unmounted) return; // unmounted while the ticket was being fetched
      sock = new WebSocket(url);
      sock.onopen = () => reconnect.opened();
      sock.onmessage = (e) => {
        try {
          const m = JSON.parse(e.data);
          if (m.type === "init") setChat(m.chat || []);
          else if (m.type === "chat") setChat((c) => [...c, ...(m.chat || [])].slice(-100));
        } catch {
          /* ignore */
        }
      };
      sock.onclose = () => {
        if (!unmounted) timer = setTimeout(connect, reconnect.waitMs());
      };
    };
    connect();
    return () => {
      unmounted = true;
      clearTimeout(timer);
      sock?.close();
    };
  }, []);

  useEffect(() => {
    chatBox.current?.scrollTo(0, chatBox.current.scrollHeight);
  }, [chat]);

  async function patch(field: keyof CrossSettings, value: boolean) {
    if (!settings) return;
    fresh.invalidate();   // polls in flight have read the state BEFORE this change
    setSettings({ ...settings, [field]: value });
    try {
      const updated = await apiJson<CrossSettings>("/cross/settings", {
        method: "PUT",
        body: JSON.stringify({ [field]: value }),
      });
      fresh.invalidate();   // and so have the ones started during the PUT
      setSettings(updated);
      toast.success(t("saved"));
    } catch (e) {
      toast.error((e as Error).message);
      loadAll();
    }
  }

  async function sendAnnounce(e: FormEvent) {
    e.preventDefault();
    if (!announce.trim()) return;
    setBusy(true);
    try {
      const res = await api("/cross/announce", { method: "POST", body: JSON.stringify({ message: announce }) });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast.error(detailText(d.detail, t("announce.failed")));
        return;   // the typed announcement stays — it did not arrive
      }
      if ((d.sent?.length ?? 0) > 0) {
        toast.success(t("announce.sent", { count: d.sent.length }));
        setAnnounce("");
        return;
      }
      // `sent: []` with HTTP 200 has SEVERAL causes, and `reason` says which one. Claiming "no
      // running server reached" in every case would mislead — most of all while the switch that
      // is the real cause sits right above on this page and shows "off".
      const REASON: Record<string, string> = {
        announcements_disabled: t("announce.reason.disabled"),
        empty: t("announce.reason.empty"),
      };
      toast.error(REASON[d.reason] ?? t("announce.reason.noTarget"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // The error banner MUST be part of this early `return`: `settings` is only set in the `try`
  // of `loadAll`, so when one of the three fetches fails (expired session, 502) the page would
  // otherwise show an endless spinner, indistinguishable from "still loading".
  if (!settings) {
    return (
      <div className="space-y-5">
        <SectionTitle icon={Network} sub={t("subtitle")}>
          {t("title")}
        </SectionTitle>
        {err ? (
          <Alert kind="error">{err}</Alert>
        ) : (
          <div className="grid place-items-center py-16">
            <Spinner className="h-6 w-6" />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <SectionTitle icon={Network} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {err && <Alert kind="error">{err}</Alert>}

      {/* Feature switches (management only) */}
      {manage && (
        <Card>
          <div className="mb-3 text-xs uppercase tracking-wide text-fg-muted">{t("features.title")}</div>
          <div className="flex flex-wrap gap-2">
            <Toggle checked={settings.presence_enabled} onChange={(v) => patch("presence_enabled", v)} icon={Users} label={t("features.presence")} />
            <Toggle checked={settings.leaderboard_enabled} onChange={(v) => patch("leaderboard_enabled", v)} icon={Trophy} label={t("features.leaderboard")} />
            <Toggle checked={settings.announcements_enabled} onChange={(v) => patch("announcements_enabled", v)} icon={Megaphone} label={t("features.announcements")} />
            <Toggle checked={settings.chat_bridge_enabled} onChange={(v) => patch("chat_bridge_enabled", v)} icon={Radio} label={t("features.chatBridge")} />
          </div>
          {!settings.chat_bridge_enabled && (
            <div className="mt-2 text-xs text-fg-dim">
              {t("features.chatBridgeOff")}
            </div>
          )}
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Presence */}
        <Card>
          <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
            <Users className="h-4 w-4" /> {t("presence.title")}
          </div>
          {presence.length === 0 ? (
            <div className="py-4 text-center text-sm text-fg-dim">{t("presence.empty")}</div>
          ) : (
            <div className="space-y-2">
              {presence.map((p) => (
                <div key={p.server_key} className="flex items-start justify-between gap-2 border-b border-surface-card/60 pb-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-fg">{p.display_name}</div>
                    <div className="flex flex-wrap gap-1 pt-1">
                      {p.players.map((n) => (
                        <Badge key={n} tone="online">{n}</Badge>
                      ))}
                    </div>
                  </div>
                  <Badge tone="info">{p.count}</Badge>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Leaderboard */}
        <Card>
          <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
            <Trophy className="h-4 w-4" /> {t("leaderboard.title")}
          </div>
          {lb.length === 0 ? (
            <div className="py-4 text-center text-sm text-fg-dim">{t("leaderboard.empty")}</div>
          ) : (
            <>
              {/* Mobile: compact list (instead of a narrow, scrolling table) */}
              <div className="space-y-1.5 sm:hidden">
                {lb.map((r, i) => (
                  <div
                    key={r.player}
                    className="flex items-center justify-between gap-2 rounded-lg border border-line bg-surface-sunken/40 px-3 py-2 text-sm"
                  >
                    <span className="min-w-0 truncate">
                      <span className="text-fg-dim">{i + 1}.</span> <span className="text-fg">{r.player}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-3 font-mono text-xs text-fg-muted">
                      <span title={t("leaderboard.hours")}>{t("leaderboard.short.hours", { n: r.playtime_hours })}</span>
                      <span title={t("leaderboard.kills")} className="text-success-text">{t("leaderboard.short.kills", { n: r.kills })}</span>
                      <span title={t("leaderboard.deaths")} className="text-danger-text">{t("leaderboard.short.deaths", { n: r.deaths })}</span>
                    </span>
                  </div>
                ))}
              </div>
              {/* Desktop: table */}
              <div className="hidden overflow-x-auto sm:block">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-fg-dim">
                      <th className="pb-1">{t("leaderboard.column.player")}</th>
                      <th className="pb-1 text-right" title={t("leaderboard.hours")}>{t("leaderboard.column.hours")}</th>
                      <th className="pb-1 text-right" title={t("leaderboard.kills")}>{t("leaderboard.column.kills")}</th>
                      <th className="pb-1 text-right" title={t("leaderboard.deaths")}>{t("leaderboard.column.deaths")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lb.map((r, i) => (
                      <tr key={r.player} className="border-t border-line/60">
                        <td className="py-1 text-fg">
                          <span className="mr-1 text-fg-dim">{i + 1}.</span>
                          {r.player}
                        </td>
                        <td className="py-1 text-right text-fg-muted">{r.playtime_hours}</td>
                        <td className="py-1 text-right text-fg-muted">{r.kills}</td>
                        <td className="py-1 text-right text-fg-muted">{r.deaths}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Card>
      </div>

      {/* Announce */}
      {manage && (
        <Card>
          <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
            <Megaphone className="h-4 w-4" /> {t("announce.title")}
          </div>
          <form onSubmit={sendAnnounce} className="flex gap-2">
            <Input value={announce} onChange={(e) => setAnnounce(e.target.value)} placeholder={t("announce.placeholder")} />
            <Button type="submit" disabled={busy || !announce.trim()}>{t("announce.send")}</Button>
          </form>
        </Card>
      )}

      {/* Chat feed */}
      <Card className="!p-0">
        <div className="flex items-center justify-between border-b border-surface-card/60 px-3 py-2">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
            <Radio className="h-4 w-4" /> {t("chat.title")}
          </div>
          <Badge tone={settings.chat_bridge_enabled ? "online" : "offline"}>
            {settings.chat_bridge_enabled ? t("chat.active") : t("chat.off")}
          </Badge>
        </div>
        <div ref={chatBox} className="h-64 overflow-auto p-3 font-mono text-xs text-fg-muted">
          {chat.length === 0 ? (
            <span className="text-fg-dim">{t("chat.empty")}</span>
          ) : (
            chat.map((c) => (
              <div key={c.id} className="border-b border-surface-card/40 py-0.5">
                <span className="text-success">[{c.server_key}]</span> <span className="text-fg">{c.player}</span>: {c.message}
              </div>
            ))
          )}
        </div>
      </Card>
    </div>
  );
}
