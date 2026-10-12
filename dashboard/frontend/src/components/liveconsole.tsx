import {
  createContext,
  MutableRefObject,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { Terminal } from "lucide-react";
import { detailText } from "../api";
import { useAuth } from "../auth";
import i18n, { tb } from "../i18n";
import { newReconnect } from "../lib/reconnect";
import { wsUrl } from "../lib/ws";
import { Toggle } from "./ui";
import { useActiveServer } from "./ServerContext";

// Source of a console line → determines its colour in the scrollback.
export type LineSource = "log" | "cmd-echo" | "cmd-out" | "cmd-info" | "cmd-err" | "action";
export interface DockLine {
  id: number;
  ts: number;
  source: LineSource;
  text: string;
}

const MAX_LINES = 1000; // ring buffer cap
const DOCK_KEY = "gss_dock_open";

interface LiveConsoleValue {
  dockOpen: boolean;
  setDockOpen: (v: boolean) => void;
  lines: DockLine[];
  logConnected: boolean;
  cmdConnected: boolean;
  canLog: boolean; // may see the live server log
  canCmd: boolean; // may send RCON commands
  serverKey: string; // the server the log AND the commands go to
  serverLabel: string; // its display name for the header
  send: (cmd: string) => void;
  emit: (text: string) => void; // action echo (no-op while the dock is closed)
  clear: () => void;
  history: MutableRefObject<string[]>;
}

const Ctx = createContext<LiveConsoleValue>(null as unknown as LiveConsoleValue);
export const useLiveConsole = () => useContext(Ctx);


// Both WebSocket routers close with meaningful codes (routers/logs.py, routers/console.py).
// Without this mapping the user only sees a silent reconnect loop: the handshake is accepted
// BEFORE the check, so the badge briefly shows "connected" and then no line ever arrives.
// The codes map to catalog keys (namespace `liveconsole`), translated when a line is written.
const CLOSE_CODES = [1006, 4400, 4401, 4404, 4501];

function closeReason(code: number): string {
  return CLOSE_CODES.includes(code) ? i18n.t(`liveconsole:close.${code}`) : "";
}

/** A message from the console socket: a coded object, a fixed backend text or free text. */
function socketText(message: unknown): string {
  return typeof message === "string" ? tb(message) : detailText(message);
}

/**
 * Holds the state of the dockable live console. MUST sit above <Routes> so that the WebSocket
 * connections survive navigation. Connects lazily: only while the dock is open AND the user
 * has the respective permission.
 */
export function LiveConsoleProvider({ children }: { children: ReactNode }) {
  const { me, can } = useAuth();
  const canLog = !!me && can("can_view_logs");
  const canCmd = !!me && (can("can_console_safe") || can("can_console_full"));
  // The dock follows the ACTIVE server — like the StatusStrip and the status page. A command
  // typed on one server's page must not go to another one, which is why the header names the
  // server.
  //
  // DELIBERATELY no fallback to `useDefaultServerKey()`: it would kick in during the window in
  // which `me` is already there but the server registry is still loading (it is only triggered
  // by `me`). Both sockets would then hang on the default server for 50–300 ms — including up to
  // 200 of its log lines under a header that names a different server a moment later. Without a
  // server there simply is no connection.
  const active = useActiveServer();
  const serverKey = active?.key ?? "";
  const serverLabel = active?.display_name || active?.key || "—";

  const [dockOpen, setDockOpenState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DOCK_KEY) === "true";
    } catch {
      return false;
    }
  });
  const setDockOpen = useCallback((v: boolean) => {
    setDockOpenState(v);
    try {
      localStorage.setItem(DOCK_KEY, String(v));
    } catch {
      /* ignore */
    }
  }, []);

  const [lines, setLines] = useState<DockLine[]>([]);
  const [logConnected, setLogConnected] = useState(false);
  const [cmdConnected, setCmdConnected] = useState(false);
  const seq = useRef(0);
  const history = useRef<string[]>([]);
  const cmdSock = useRef<WebSocket | null>(null);
  // The close reason last reported per socket, as `<serverKey>:<code>`. A ref, not local to
  // the effect: otherwise the suppression would survive a "clear history" and the only
  // explanation would be wiped for good.
  const logReported = useRef("");
  const cmdReported = useRef("");
  const dockOpenRef = useRef(dockOpen);
  useEffect(() => {
    dockOpenRef.current = dockOpen;
  }, [dockOpen]);

  const append = useCallback((source: LineSource, text: string) => {
    setLines((l) => {
      const next = [...l, { id: ++seq.current, ts: Date.now(), source, text }];
      return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
    });
  }, []);

  // --- Live server log (/ws/logs) -----------------------------------------
  const wantLog = dockOpen && canLog && !!serverKey;
  useEffect(() => {
    if (!wantLog) {
      setLogConnected(false);
      return;
    }
    let unmounted = false;
    // The backoff rule lives in `lib/reconnect.ts`.
    const reconnect = newReconnect();
    let timer: ReturnType<typeof setTimeout>;
    let sock: WebSocket;
    let buf = ""; // buffers an incomplete last line
    async function connect() {
      buf = ""; // do not glue leftovers of the dead connection to the first chunk of the new one
      reconnect.attemptStarted();
      // `?server=` is REQUIRED (routers/logs.py) — without it the router closes with 4400.
      const url = await wsUrl(`/ws/logs?server=${encodeURIComponent(serverKey)}`);
      if (unmounted) return; // unmounted while the ticket was being fetched
      sock = new WebSocket(url);
      sock.onopen = () => {
        reconnect.opened();
        setLogConnected(true);
      };
      sock.onclose = (e) => {
        setLogConnected(false); // BEFORE the unmounted guard: otherwise the badge stays green on
        if (unmounted) return;  // a server switch although nothing is connected yet.
        const reason = closeReason(e.code);
        // The key belongs in the mark: otherwise a 4404 of server A swallows the same code of
        // server B, and the dock would show the separator line and nothing else.
        const mark = `${serverKey}:${e.code}`;
        if (mark !== logReported.current) {
          logReported.current = mark;
          if (reason) append("cmd-err", i18n.t("liveconsole:log.closed", { server: serverKey, reason }));
          // A clean close is not an error but needs explaining: `docker logs --follow` ends
          // at once if the container is not running. Without this line there would be a red
          // badge and nothing else — the most common state for on-demand servers.
          else if (e.code === 1000) {
            append("cmd-info", i18n.t("liveconsole:log.ended", { server: serverKey }));
          }
        }
        timer = setTimeout(connect, reconnect.waitMs());
      };
      sock.onmessage = (e) => {
        logReported.current = ""; // carrying again → a later failure may be reported again
        buf += typeof e.data === "string" ? e.data : "";
        const parts = buf.split("\n");
        buf = parts.pop() ?? "";
        for (const line of parts) {
          const t = line.replace(/\s+$/, "");
          if (t) append("log", t);
        }
      };
    }
    connect();
    return () => {
      unmounted = true;
      clearTimeout(timer);
      sock?.close();
    };
  }, [wantLog, append, serverKey]);

  // --- RCON console (/ws/console) -----------------------------------------
  const wantCmd = dockOpen && canCmd && !!serverKey;
  useEffect(() => {
    if (!wantCmd) {
      setCmdConnected(false);
      cmdSock.current = null;
      return;
    }
    let unmounted = false;
    const reconnect = newReconnect();
    let timer: ReturnType<typeof setTimeout>;
    async function connect() {
      reconnect.attemptStarted();
      const url = await wsUrl(`/ws/console?server=${encodeURIComponent(serverKey)}`);
      if (unmounted) return; // unmounted while the ticket was being fetched
      const sock = new WebSocket(url);
      cmdSock.current = sock;
      sock.onopen = () => {
        reconnect.opened();
        setCmdConnected(true);
      };
      sock.onclose = (e) => {
        setCmdConnected(false);
        if (unmounted) return;
        const reason = closeReason(e.code);
        const mark = `${serverKey}:${e.code}`;
        if (reason && mark !== cmdReported.current) {
          cmdReported.current = mark;
          append("cmd-err", i18n.t("liveconsole:console.closed", { server: serverKey, reason }));
        }
        timer = setTimeout(connect, reconnect.waitMs());
      };
      sock.onmessage = (e) => {
        cmdReported.current = "";
        try {
          const m = JSON.parse(e.data);
          if (m.type === "info") append("cmd-info", `» ${socketText(m.message)}`);
          else if (m.type === "error") append("cmd-err", `✖ ${socketText(m.message)}`);
          else if (m.type === "output") {
            append("cmd-echo", `$ ${m.command}`);
            if (m.output) append("cmd-out", m.output);
          }
        } catch {
          append("cmd-out", String(e.data));
        }
      };
    }
    connect();
    return () => {
      unmounted = true;
      clearTimeout(timer);
      cmdSock.current?.close();
      cmdSock.current = null;
    };
  }, [wantCmd, append, serverKey]);

  const send = useCallback(
    (cmd: string) => {
      const c = cmd.trim();
      if (!c) return;
      const sock = cmdSock.current;
      if (!sock || sock.readyState !== WebSocket.OPEN) {
        append("cmd-err", i18n.t("liveconsole:notConnected"));
        return;
      }
      sock.send(c);
      history.current = [c, ...history.current].slice(0, 50);
    },
    [append],
  );

  const emit = useCallback(
    (text: string) => {
      if (!dockOpenRef.current) return; // no echo while the dock is closed
      append("action", text);
    },
    [append],
  );

  const clear = useCallback(() => {
    setLines([]);
    logReported.current = "";
    cmdReported.current = "";
  }, []);

  // The history belongs to the provider and survives a server switch — but the header changes
  // at once. Without this separator the lines of server A would sit under the name of server B,
  // coloured the same and with nothing to tell them apart. Deliberately not cleared: the RCON
  // output one has just read should not vanish because one changes the page.
  // `shownKey` only advances while the dock is open. A switch that happened while the dock was
  // closed is thereby caught up on opening — and several switches in a row yield exactly one
  // separator instead of one per click.
  const shownKey = useRef("");
  useEffect(() => {
    if (!dockOpen || !serverKey) return;
    if (shownKey.current && shownKey.current !== serverKey) {
      append("cmd-info", i18n.t("liveconsole:serverSwitched", { label: serverLabel, server: serverKey }));
    }
    shownKey.current = serverKey;
  }, [dockOpen, serverKey, serverLabel, append]);

  // Nothing of the session may outlive it: the provider sits above the routes and is not
  // unmounted on sign-out. `history` is explicitly part of that — it is the buffer behind the
  // arrow keys of the input and holds sent RCON commands in plain text, including ones that
  // carry passwords (`adduser <name> <password>`). Without this the next person signing in on
  // the same browser would read the previous one's commands with a single key press.
  useEffect(() => {
    if (!me) {
      setLines([]);
      history.current = [];
      logReported.current = "";
      cmdReported.current = "";
      shownKey.current = "";
    }
  }, [me]);

  return (
    <Ctx.Provider
      value={{
        dockOpen,
        setDockOpen,
        lines,
        logConnected,
        cmdConnected,
        canLog,
        canCmd,
        serverKey,
        serverLabel,
        send,
        emit,
        clear,
        history,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

/** Top bar switch that docks the live console. Only visible with a matching permission. */
export function DockToggle() {
  const { t } = useTranslation("liveconsole");
  const { dockOpen, setDockOpen, canLog, canCmd } = useLiveConsole();
  if (!canLog && !canCmd) return null;
  return (
    <Toggle checked={dockOpen} onChange={setDockOpen} icon={Terminal} label={t("toggle.label")} title={t("toggle.title")} />
  );
}
