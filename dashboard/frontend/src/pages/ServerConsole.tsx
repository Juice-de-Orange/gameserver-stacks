// Live console of the active server: RCON through /ws/console?server=<key>.
// Quick commands per game; free input. Access is gated by the route (can_console_*).
import { FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Lock, Terminal } from "lucide-react";
import { detailText } from "../api";
import { tb } from "../i18n";
import { useActiveServer } from "../components/ServerContext";
import { newReconnect } from "../lib/reconnect";
import { wsUrl } from "../lib/ws";
import { Badge, Button, Card, GhostButton, Input, SectionTitle } from "../components/ui";

// Quick commands per game (extend freely; one outside the safe whitelist needs the full mode).
const QUICK: Record<string, string[]> = {
  pz: ["players", "save"],
  minecraft: ["list", "save-all"],
  // VS: the "Vintage RCON" mod injects like the server console → WITHOUT a leading slash.
  vintagestory: ["list clients", "stats"],
  // Reforger: `#`-prefixed, read-only verbs (destructive #kick/#ban/#restart need the full console).
  arma_reforger: ["#players", "#roles"],
  // Factorio: with a slash, as in the game. `/server-save` is left out on purpose — it halts
  // the server while writing, and the backup button exists for that.
  factorio: ["/players online", "/admins", "/version"],
  // Avorion: the same selection as in the driver's safe mode.
  avorion: ["/players", "/status"],
  // Valheim is deliberately NOT listed: the server has no console, and the tab is locked.
};

/** A message from the socket: a plain backend text or a coded `{code, message, params}`. */
function messageText(message: unknown): string {
  return typeof message === "string" ? tb(message) : detailText(message);
}

// Vintage Story's console comes from an optional third-party mod. When the backend reports it
// missing, the page says so with the way to set it up instead of a bare "RCON unreachable".
const VS_MOD_MISSING = "console.vintagestory_rcon_mod_missing";

function hasCode(message: unknown, code: string): boolean {
  return typeof message === "object" && message !== null && (message as { code?: unknown }).code === code;
}

export default function ServerConsole() {
  const { t } = useTranslation("serverConsole");
  const server = useActiveServer();
  const key = server?.key;
  const [lines, setLines] = useState<string[]>([]);
  const [cmd, setCmd] = useState("");
  const [connected, setConnected] = useState(false);
  const [modMissing, setModMissing] = useState<unknown>(null);
  const ws = useRef<WebSocket | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!key) return;
    let unmounted = false;
    // Rule and reasoning: `lib/reconnect.ts`. In short: reset the backoff neither in `onopen`
    // (the handshake is accepted before the auth check) nor on the first message — only once
    // the connection has held.
    const reconnect = newReconnect();
    let timer: ReturnType<typeof setTimeout>;
    setLines([]);
    setModMissing(null);
    const push = (s: string) => setLines((l) => [...l, s]);
    async function connect() {
      reconnect.attemptStarted();
      const url = await wsUrl(`/ws/console?server=${key}`);
      if (unmounted) return; // unmounted while the ticket was being fetched
      const sock = new WebSocket(url);
      ws.current = sock;
      sock.onopen = () => {
        reconnect.opened();
        setConnected(true);
      };
      sock.onclose = () => {
        setConnected(false);
        if (!unmounted) timer = setTimeout(connect, reconnect.waitMs());
      };
      sock.onmessage = (e) => {
        try {
          const m = JSON.parse(e.data);
          if (m.type === "info") push(`» ${messageText(m.message)}`);
          else if (m.type === "error" && hasCode(m.message, VS_MOD_MISSING)) setModMissing(m.message);
          else if (m.type === "error") push(`✖ ${messageText(m.message)}`);
          else if (m.type === "output") push(`$ ${m.command}\n${m.output}`);
        } catch {
          push(e.data as string);
        }
      };
    }
    connect();
    return () => {
      unmounted = true;
      clearTimeout(timer);
      ws.current?.close();
    };
  }, [key]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [lines]);

  function send(c: string) {
    if (!c.trim() || ws.current?.readyState !== WebSocket.OPEN) return;
    ws.current.send(c);
    setCmd("");
  }

  if (!server) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;
  const quick = QUICK[server.game_type] ?? [];

  return (
    <div className="space-y-4">
      <SectionTitle
        icon={Terminal}
        sub={`${t("subtitle")} · ${server.display_name}`}
        right={<Badge tone={connected ? "online" : "blood"}>{connected ? t("state.connected") : t("state.disconnected")}</Badge>}
      >
        {t("title")}
      </SectionTitle>

      {modMissing != null && (
        <Card className="flex items-start gap-3">
          <Lock className="mt-0.5 h-5 w-5 shrink-0 text-fg-dim" />
          <div className="space-y-2 text-sm">
            <div className="font-semibold text-fg">{t("vsMod.title")}</div>
            <p className="text-fg-muted">{detailText(modMissing)}</p>
            <p className="text-fg-muted">{t("vsMod.howTo")}</p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <a className="underline underline-offset-2" href="https://github.com/Juice-de-Orange/gameserver-stacks/blob/HEAD/games/vintage-story/README.md#optional-remote-console-with-the-vintage-rcon-mod" target="_blank" rel="noreferrer noopener">
                {t("vsMod.guide")}
              </a>
              <a className="underline underline-offset-2" href="https://mods.vintagestory.at/vintagercon" target="_blank" rel="noreferrer noopener">
                {t("vsMod.modPage")}
              </a>
            </div>
          </div>
        </Card>
      )}

      {quick.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {quick.map((q) => (
            <GhostButton key={q} onClick={() => send(q)}>
              {q}
            </GhostButton>
          ))}
        </div>
      )}

      <Card className="!p-0">
        <div
          ref={boxRef}
          className="h-[55vh] overflow-auto whitespace-pre-wrap p-3 font-mono text-xs text-fg-muted sm:h-[26rem]"
        >
          {lines.length === 0 ? (
            <span className="text-fg-dim">{t("waiting")}</span>
          ) : (
            lines.map((l, i) => (
              <div key={i} className="border-b border-surface-card/60 py-0.5">
                {l}
              </div>
            ))
          )}
        </div>
      </Card>

      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          send(cmd);
        }}
        className="flex gap-2"
      >
        <Input
          value={cmd}
          onChange={(e) => setCmd(e.target.value)}
          placeholder={t("input")}
          className="font-mono"
        />
        <Button type="submit">{t("send")}</Button>
      </form>
    </div>
  );
}
