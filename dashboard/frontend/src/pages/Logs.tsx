import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, ScrollText } from "lucide-react";
import { api, errText } from "../api";
import { useActiveServer } from "../components/ServerContext";
import { Alert, Badge, Card, GhostButton, SectionTitle } from "../components/ui";
import { newReconnect } from "../lib/reconnect";
import { wsUrl } from "../lib/ws";

export default function Logs() {
  const { t } = useTranslation("logs");
  const server = useActiveServer();
  const key = server?.key;
  const [text, setText] = useState("");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!key) return;
    setText("");
    let unmounted = false;
    // Rule and reasoning are in `lib/reconnect.ts`. In short: the backoff must not be reset in
    // `onmessage` — `docker logs?follow=1&tail=200` on a STOPPED container delivers exactly one
    // message and closes at once, which would mean one connection per second while the tab is open.
    const reconnect = newReconnect();
    let timer: ReturnType<typeof setTimeout>;
    let sock: WebSocket;
    async function connect() {
      reconnect.attemptStarted();
      const url = await wsUrl(`/ws/logs?server=${key}`);
      if (unmounted) return; // unmounted while the ticket was being fetched
      sock = new WebSocket(url);
      sock.onopen = () => {
        reconnect.opened();
        setConnected(true);
      };
      sock.onclose = () => {
        setConnected(false);
        if (!unmounted) timer = setTimeout(connect, reconnect.waitMs());
      };
      sock.onmessage = (e) => {
        setText((t) => (t + e.data).slice(-100000));
      };
    }
    connect();
    return () => {
      unmounted = true;
      clearTimeout(timer);
      sock?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [text]);

  async function download() {
    if (!key) return;
    const res = await api(`/servers/${key}/logs/download`);
    if (!res.ok) {
      // Without this check a 502/403 would save the JSON error body as the log file.
      setError(await errText(res, res.statusText));
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${key}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      <SectionTitle
        icon={ScrollText}
        sub={`${t("subtitle")}${server ? ` · ${server.display_name}` : ""}`}
        right={
          <>
            <Badge tone={connected ? "online" : "blood"}>{connected ? t("state.live") : t("state.disconnected")}</Badge>
            <GhostButton onClick={download}>
              <Download className="h-4 w-4" /> {t("download")}
            </GhostButton>
          </>
        }
      >
        {t("title")}
      </SectionTitle>
      {error && <Alert>{error}</Alert>}
      <Card className="!p-0">
        <div
          ref={boxRef}
          className="h-[30rem] overflow-auto whitespace-pre-wrap p-3 font-mono text-xs text-fg-muted"
        >
          {text || t("waiting")}
        </div>
      </Card>
    </div>
  );
}
