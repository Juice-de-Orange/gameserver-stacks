import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Copy, DoorOpen, Download, ExternalLink, Info, Mic, MessageCircle, Package, Play } from "lucide-react";
import { api, errText } from "../api";
import { usePolledData } from "../hooks/usePolledData";
import { useActiveServerKey, useServers } from "../components/ServerContext";
import { NoServer } from "../components/NoServer";
import { useToast } from "../components/feedback";
import { Alert, Button, Card, EmptyState, GhostButton, SectionTitle, cx } from "../components/ui";
import i18n, { tb } from "../i18n";
import { safeUrl } from "../lib/url";

interface JoinInfo {
  server: {
    key: string;
    name: string;
    host: string;
    port: number;
    protocol: string;
    /** One-click join — exists ONLY where the game really has a protocol for it (PZ). */
    uri: string;
    password_required: boolean;
    hint: string;
  };
  status?: { running: boolean; players: number | null; max_players: number | null };
  next_restart?: string | null;
  mods: { workshop_id: string; title: string; url: string }[];
  mod_collection_url?: string | null;
  community_url?: string | null;
  auto_download?: boolean;
  /** What the player needs on THEIR machine. Absent for games where there is nothing to do. */
  client_setup?: {
    edition: string;
    version: string;
    /** `null` = unknown (plugin folder not readable) — then NOTHING is claimed about voice chat. */
    voice: boolean | null;
    modpack: boolean;
  } | null;
}

/** The whole phrase ("in 2 h 5 min"), translated at call time. */
function fmtCountdown(target: string): string {
  const ms = new Date(target).getTime() - Date.now();
  if (ms <= 0) return i18n.t("join:restart.now");
  const totalMin = Math.floor(ms / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? i18n.t("join:restart.inHoursMinutes", { h, m }) : i18n.t("join:restart.inMinutes", { m });
}

export default function Join() {
  const { t } = useTranslation("join");
  const toast = useToast();
  const key = useActiveServerKey();
  const { data: info, error } = usePolledData<JoinInfo>(key ? `/servers/${key}/join-info` : null, 30000);
  const { loading: serversLoading } = useServers();
  const [, setTick] = useState(0);
  const [packBusy, setPackBusy] = useState(false);

  // Re-render the countdown regularly ("in X h Y min").
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 30000);
    return () => window.clearInterval(t);
  }, []);

  function copy(text: string, done: string) {
    navigator.clipboard?.writeText(text);
    toast.success(done);
  }

  /** Fetch the join pack. A plain `<a href>` would not do — the download needs the token. */
  async function downloadPack() {
    setPackBusy(true);
    try {
      const res = await api(`/servers/${key}/join-pack`);
      if (!res.ok) {
        // Without this branch the browser would save the JSON error body of a 404 as the "modpack".
        toast.error(await errText(res, res.statusText));
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `stacks-${cs?.version || "modpack"}.mrpack`;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      toast.error(t("pack.failed", { error: e instanceof Error ? e.message : t("pack.unknownError") }));
    } finally {
      setPackBusy(false);
    }
  }

  // No enabled server: there is nothing to join, and a request would only answer "not found".
  if (!key && !serversLoading) return <NoServer />;

  const s = info?.server;
  // The PORT belongs in the check, not only the host: `drivers/base.game_port()` returns 0 when
  // the `game` entry is missing, and `game_ports` is optional (default `"{}"`). Without the check
  // the card would show `<host>:0` with "Copy address" and "Join now" instead of the honest
  // warning — and this is the page for friends.
  const addr = s?.host && s.port ? `${s.host}:${s.port}` : "";
  const mods = info?.mods || [];
  const running = info?.status?.running;
  const players = info?.status?.players;
  const maxPlayers = info?.status?.max_players;
  const cs = info?.client_setup;

  return (
    <div className="space-y-5">
      <SectionTitle icon={DoorOpen} sub={t("subtitle")}>
        {s?.name ? t("titleNamed", { name: s.name }) : t("title")}
      </SectionTitle>
      {error && <Alert>{error}</Alert>}

      {/* Live status + join */}
      <Card className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className={cx("h-3 w-3 rounded-full", running ? "bg-success animate-live" : "bg-fg-dim")} />
            <div>
              <div className={cx("text-stencil text-lg", running ? "text-success-text" : "text-fg-muted")}>
                {info ? (running ? t("status.online") : t("status.offline")) : "…"}
              </div>
              {running && players != null && (
                <div className="text-sm text-fg-muted">
                  {t("status.players", {
                    count: players,
                    shown: maxPlayers ? `${players} / ${maxPlayers}` : `${players}`,
                  })}
                </div>
              )}
            </div>
          </div>
          {info?.next_restart && (
            <div className="text-right text-xs text-fg-dim">
              <div className="uppercase tracking-wide">{t("restart.next")}</div>
              <div className="text-sm text-warning-text">{fmtCountdown(info.next_restart)}</div>
            </div>
          )}
        </div>

        {!info ? (
          <p className="text-sm text-fg-dim">{t("common:state.loading")}</p>
        ) : !addr ? (
          // Fail closed: rather honestly nothing than an address nobody can reach.
          <Alert kind="warn">
            <Trans t={t} i18nKey="address.missing" components={{ c: <code /> }} />
          </Alert>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {/* The button appears ONLY when there really is a one-click join. A button that
                  does nothing would be worse than none. */}
              {s!.uri && (
                <Button onClick={() => (window.location.href = s!.uri)}>
                  <Play className="h-4 w-4" /> {t("joinNow")}
                </Button>
              )}
              <code className="rounded-md border border-line bg-surface-raised px-3 py-2 font-mono text-success-text">
                {addr}
              </code>
              <GhostButton onClick={() => copy(addr, t("address.copied"))}>
                <Copy className="h-4 w-4" /> {t("address.copy")}
              </GhostButton>
            </div>
            <p className="text-xs text-fg-dim">
              {tb(s!.hint)}
              {s!.password_required && ` ${t("passwordRequired")}`}
            </p>
          </>
        )}
      </Card>

      {info?.auto_download && (
        <Alert kind="info">
          <span className="flex items-start gap-2">
            <Info className="mt-0.5 h-4 w-4 shrink-0" />
            {t("autoDownload")}
          </span>
        </Alert>
      )}

      {/* What the player has to install. Only shown where there really is something to do —
          for Project Zomboid Steam fetches everything by itself and the card is gone. */}
      {cs && (
        <Card className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Info className="mr-1 inline h-4 w-4" /> {t("client.title")}
          </div>

          <ul className="space-y-2 text-sm text-fg">
            <li className="flex items-start gap-2">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
              <span>
                <Trans
                  t={t}
                  i18nKey="client.edition"
                  values={{ edition: cs.edition }}
                  components={{ b: <strong /> }}
                />
              </span>
            </li>
            {cs.version && (
              <li className="flex items-start gap-2">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                <span className="flex flex-wrap items-center gap-2">
                  <Trans
                    t={t}
                    i18nKey="client.version"
                    values={{ version: cs.version }}
                    components={{
                      c: (
                        <code className="rounded border border-line bg-surface-raised px-1.5 py-0.5 font-mono text-success-text" />
                      ),
                    }}
                  />
                  <GhostButton onClick={() => copy(cs.version, t("client.versionCopied"))}>
                    <Copy className="h-3.5 w-3.5" /> {t("client.versionCopy")}
                  </GhostButton>
                </span>
              </li>
            )}
          </ul>

          {/* `voice === null` means UNKNOWN: then nothing is shown here, rather than "no voice chat". */}
          {cs.voice === true && (
            <div className="space-y-2 rounded-md border border-line bg-surface-raised p-3">
              <div className="flex items-center gap-2 text-sm text-fg">
                <Mic className="h-4 w-4 shrink-0 text-primary" />
                <strong>{t("voice.title")}</strong>
              </div>
              <p className="text-sm text-fg-muted">
                {t("voice.intro")}
              </p>
              {cs.modpack ? (
                <>
                  <p className="text-sm text-fg-muted">
                    <Trans
                      t={t}
                      i18nKey="voice.howto"
                      components={{
                        app: (
                          <a
                            href="https://modrinth.com/app"
                            target="_blank"
                            rel="noreferrer"
                            className="text-primary hover:underline"
                          />
                        ),
                        em: <em />,
                      }}
                    />
                  </p>
                  <Button onClick={downloadPack} disabled={packBusy}>
                    <Download className="h-4 w-4" />
                    {packBusy ? t("common:state.loading") : t("pack.download")}
                  </Button>
                </>
              ) : (
                <Alert kind="warn">
                  {t("pack.missing")}
                </Alert>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Mods */}
      <Card className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Package className="mr-1 inline h-4 w-4" /> {t("mods.title", { n: mods.length })}
          </div>
          <div className="flex flex-wrap gap-2">
            {safeUrl(info?.mod_collection_url) && (
              <a
                href={safeUrl(info?.mod_collection_url)}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                <ExternalLink className="h-4 w-4" /> {t("mods.subscribe")}
              </a>
            )}
            {safeUrl(info?.community_url) && (
              <a
                href={safeUrl(info?.community_url)}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                <MessageCircle className="h-4 w-4" /> {t("mods.community")}
              </a>
            )}
          </div>
        </div>
        {mods.length === 0 ? (
          <EmptyState icon={Package}>{t("mods.empty")}</EmptyState>
        ) : (
          <ul className="space-y-1 text-sm">
            {mods.map((m, i) => (
              <li key={m.workshop_id || `${m.title}-${i}`} className="flex items-center gap-2">
                <span className="text-fg">{m.title}</span>
                {safeUrl(m.url) && (
                  <a
                    href={safeUrl(m.url)}
                    target="_blank"
                    rel="noreferrer"
                    className="text-fg-dim hover:text-primary"
                    aria-label={t("mods.openAria", { title: m.title })}
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
