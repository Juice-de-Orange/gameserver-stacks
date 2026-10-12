// Next steps for a server that is switched off — above all a new one without a container.
//
// Whether the container exists is asked of the backend (`GET /api/servers/<key>/setup`, which
// asks Docker), never guessed here. "Create container" runs `services/provisioning.py` as a
// background operation (image download with progress, data directories, configuration,
// container); the page polls its progress (`GET /api/servers/<key>/provision`) and survives a
// reload. The same step is still available on the host (`app.cli create-container`).
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Box, CheckCircle2, Copy, ExternalLink, KeyRound, Pencil, Play, PowerOff, RefreshCw } from "lucide-react";
import { api, apiJson, detailText, errText } from "../api";
import { useAuth } from "../auth";
import { usePolledData } from "../hooks/usePolledData";
import { gameLabel, ServerInfo } from "../lib/game";
import { safeUrl } from "../lib/url";
import { useToast } from "./feedback";
import { OperationPanel, OperationState } from "./Operation";
import { useServerActions } from "./ServerActions";
import { useServers } from "./ServerContext";
import { Alert, Badge, Button, Card, EmptyState, GhostButton, Input, SectionTitle, Skeleton } from "./ui";

/** Where the console password comes from (`services/secrets.status`) — never the value. */
export interface SecretState {
  source: "env" | "stored" | "none";
  env_name: string;
  env_set: boolean;
  stored: boolean;
  unreadable: boolean;
}

export interface SetupState {
  key: string;
  enabled: boolean;
  management_mode: "compose" | "dashboard";
  container_name: string;
  container: { exists: boolean | null; running: boolean | null; status: string; error?: string };
  create_command: string;
  secret: SecretState;
  /** Further passwords of this game by slot (Project Zomboid: `admin`, the in-game admin). */
  secrets?: Record<string, SecretState>;
  /** Minecraft whose EULA acceptance is not recorded yet (a row created before it was asked). */
  eula_required?: boolean;
}

const EULA_URL = "https://www.minecraft.net/en-us/eula";

/**
 * The "Create container" operation of one server: attaches to a running one on entry, polls every
 * 2 s while it runs, and calls `onDone` once when it has finished — also for one that `track` is
 * handed already finished.
 */
export function useProvision(serverKey: string, onDone: (op: OperationState) => void) {
  const [op, setOp] = useState<OperationState | null>(null);
  const idRef = useRef<number | null>(null);
  const runningRef = useRef(false);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  const poll = useCallback(async () => {
    try {
      const path = idRef.current ? `/servers/${serverKey}/provision?id=${idRef.current}` : `/servers/${serverKey}/provision`;
      const data = await apiJson<Partial<OperationState>>(path);
      if (typeof data.id !== "number") return;
      const current = data as OperationState;
      idRef.current = current.id;
      const wasRunning = runningRef.current;
      runningRef.current = current.running;
      setOp(current);
      if (wasRunning && !current.running) doneRef.current(current);
    } catch {
      // A missed poll is not an error — the operation keeps running in the backend.
    }
  }, [serverKey]);

  useEffect(() => {
    idRef.current = null;
    setOp(null);
    void poll();
  }, [poll]);

  useEffect(() => {
    if (!op?.running) return;
    const timer = window.setInterval(() => void poll(), 2000);
    return () => window.clearInterval(timer);
  }, [op?.running, poll]);

  const track = useCallback(
    (started: OperationState) => {
      idRef.current = started.id;
      runningRef.current = started.running;
      setOp(started);
      if (!started.running) doneRef.current(started);
      // Ask at once instead of one interval later: the start answer always says "preparing",
      // and with a local image the whole operation takes about a second.
      else void poll();
    },
    [poll],
  );
  return { op, track };
}

/** A failed operation carries the coded error in `result` (`{code, message, params}`): translate it. */
export function operationForDisplay(op: OperationState | null): OperationState | null {
  if (!op || op.status !== "failed" || typeof op.result?.code !== "string") return op;
  return { ...op, detail: detailText(op.result, op.detail) };
}

// The operator runs the command in another window; the page notices the container by itself.
const POLL_MS = 10000;

export function ServerSetup({ server }: { server: ServerInfo }) {
  const { t } = useTranslation("serverSetup");
  const { me } = useAuth();
  const name = server.display_name || gameLabel(server);

  if (me?.role !== "admin") {
    return (
      <EmptyState icon={PowerOff}>
        <div className="font-semibold text-fg">{t("off.title", { name })}</div>
        <div>{t("off.member")}</div>
      </EmptyState>
    );
  }
  return <AdminSetup key={server.key} server={server} name={name} />;
}

function AdminSetup({ server, name }: { server: ServerInfo; name: string }) {
  const { t } = useTranslation(["serverSetup", "common", "addServer"]);
  const toast = useToast();
  const { reload } = useServers();
  const { data, error, setData } = usePolledData<SetupState>(`/servers/${server.key}/setup`, POLL_MS);
  const { busy, runAction, dialog } = useServerActions(() => void reload());
  const [enabling, setEnabling] = useState(false);
  const [checking, setChecking] = useState(false);
  const [creating, setCreating] = useState(false);
  const [eulaAccepted, setEulaAccepted] = useState(false);
  // Why the last "Enable and start" did not start the server (it was switched back off).
  const [startRefused, setStartRefused] = useState("");
  // When it is done the "Create" step, and the panel showing the operation with it, gives way to
  // "Enable and start" — so the success is said once more where it does not disappear.
  const { op, track } = useProvision(server.key, (done) => {
    if (done.status === "ok") toast.success(t("create.done", { container: server.container_name }));
    void checkAgain();
  });

  async function createContainer() {
    setCreating(true);
    try {
      const res = await api(`/servers/${server.key}/container`, {
        method: "POST",
        body: JSON.stringify({ eula_accepted: eulaAccepted }),
      });
      if (!res.ok) {
        toast.error(await errText(res, t("create.failed")));
        return;
      }
      track((await res.json()) as OperationState);
    } finally {
      setCreating(false);
    }
  }

  async function checkAgain() {
    setChecking(true);
    try {
      const res = await api(`/servers/${server.key}/setup`);
      if (res.ok) setData((await res.json()) as SetupState);
      else toast.error(await errText(res, t("error.load")));
    } finally {
      setChecking(false);
    }
  }

  async function enableAndStart() {
    setEnabling(true);
    setStartRefused("");
    try {
      const res = await api(`/servers/${server.key}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: true }),
      });
      if (!res.ok) {
        toast.error(await errText(res, t("error.enable")));
        return;
      }
      // The same start chain as everywhere else (budget dialog, boot gate). Its `onDone`
      // reloads the registry, and this page then turns into the regular server page.
      const outcome = await runAction(server.key, name, "start");
      if (outcome.status === "failed") {
        // One step for the admin: a refused start (a port held by a program on the host, the
        // mod boot gate, Docker) switches the server back off instead of leaving it enabled but
        // stopped. This page stays, with the reason and "Edit server" next to it.
        setStartRefused(outcome.reason);
        const back = await api(`/servers/${server.key}`, {
          method: "PUT",
          body: JSON.stringify({ enabled: false }),
        });
        if (!back.ok) toast.error(await errText(back, t("error.disable")));
        void reload();
      }
    } finally {
      setEnabling(false);
    }
  }

  function copy(text: string) {
    navigator.clipboard?.writeText(text);
    toast.success(t("common:state.copied"));
  }

  const c = data?.container;
  const hasConsole = server.console_kind !== "none";
  const dashboardManaged = data?.management_mode === "dashboard";
  const secretMissing = hasConsole && data?.secret.source === "none";

  return (
    <div className="space-y-5">
      <SectionTitle icon={PowerOff} sub={t("subtitle", { key: server.key })} right={<Badge tone="warn">{t("badge.off")}</Badge>}>
        {name}
      </SectionTitle>
      <div>
        <Link to={`/s/${server.key}/edit`}>
          <GhostButton>
            <Pencil className="h-4 w-4" /> {t("edit")}
          </GhostButton>
        </Link>
      </div>

      {error && <Alert kind="error">{error}</Alert>}
      {!data && !error && <Skeleton className="h-40" />}

      {c && c.exists === null && <Alert kind="error">{t("docker.unknown", { reason: c.error ?? "" })}</Alert>}

      {data && hasConsole && dashboardManaged && (
        <SecretCard
          serverKey={server.key}
          secret={data.secret}
          onChange={(secret) => setData({ ...data, secret })}
        />
      )}

      {data && c?.exists === false && (
        <Card className="space-y-4">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("next.title")}</div>
          <ol className="list-inside list-decimal space-y-4 text-sm text-fg">
            {secretMissing && <li>{t("secret.first")}</li>}
            {data.create_command ? (
              <li>
                <span className="font-semibold">{t("create.title")}</span>
                <div className="mt-1 text-fg-muted">{t("create.why")}</div>
                {data.eula_required && (
                  <div className="mt-2 space-y-1">
                    <label className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        className="mt-1"
                        checked={eulaAccepted}
                        onChange={(e) => setEulaAccepted(e.target.checked)}
                      />
                      <span>{t("addServer:accept.eula")}</span>
                    </label>
                    <a href={safeUrl(EULA_URL)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary-text underline">
                      {EULA_URL} <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  </div>
                )}
                <div className="mt-3">
                  <Button
                    disabled={creating || !!op?.running || (!!data.eula_required && !eulaAccepted)}
                    onClick={() => void createContainer()}
                  >
                    <Box className="h-4 w-4" /> {t("create.button")}
                  </Button>
                </div>
                {op && (
                  <div className="mt-3">
                    <OperationPanel op={operationForDisplay(op)} title={t("create.operation")} />
                  </div>
                )}
                <details className="mt-3 text-fg-muted">
                  <summary className="cursor-pointer text-xs">{t("create.host")}</summary>
                  <div className="mt-2 flex items-stretch gap-2">
                    <pre
                      data-testid="create-command"
                      className="min-w-0 flex-1 overflow-x-auto rounded-xl border border-line bg-surface-sunken px-3 py-2 font-mono text-xs"
                    >
                      {data.create_command}
                    </pre>
                    <GhostButton onClick={() => copy(data.create_command)} title={t("common:action.copy")}>
                      <Copy className="h-4 w-4" /> <span className="hidden sm:inline">{t("common:action.copy")}</span>
                    </GhostButton>
                  </div>
                  <div className="mt-1 text-xs text-fg-dim">{t("create.where")}</div>
                </details>
              </li>
            ) : (
              <li>{t("create.compose")}</li>
            )}
            <li>{t("then")}</li>
          </ol>
          <GhostButton disabled={checking} onClick={checkAgain}>
            <RefreshCw className="h-4 w-4" /> {t("checkAgain")}
          </GhostButton>
        </Card>
      )}

      {data && c?.exists === true && (
        <Card className="space-y-3">
          {startRefused && <Alert kind="error">{t("start.refused", { reason: startRefused })}</Alert>}
          <div className="flex items-center gap-2 text-sm text-fg">
            <CheckCircle2 className="h-4 w-4 text-success-text" />
            {t("exists", { container: data.container_name, status: c.status || "?" })}
          </div>
          {secretMissing && <Alert kind="warn">{t("secret.missing")}</Alert>}
          <Button disabled={enabling || !!busy} onClick={enableAndStart}>
            <Play className="h-4 w-4" /> {t("enableAndStart")}
          </Button>
        </Card>
      )}

      {dialog}
    </div>
  );
}

/**
 * A password of the server — by default the console password: where it comes from, and a field
 * to store a new one (encrypted, `PUT /api/servers/<key>/secret` with its `slot`). The value is
 * never shown — the API never returns it.
 */
export function SecretCard({
  serverKey,
  secret,
  onChange,
  slot = "rcon",
}: {
  serverKey: string;
  secret: SecretState;
  onChange: (secret: SecretState) => void;
  slot?: "rcon" | "admin";
}) {
  const { t } = useTranslation(["serverSetup", "common"]);
  const toast = useToast();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  async function save(next: string) {
    setSaving(true);
    try {
      const res = await api(`/servers/${serverKey}/secret`, { method: "PUT", body: JSON.stringify({ value: next, slot }) });
      if (!res.ok) {
        toast.error(await errText(res, t("secret.error")));
        return;
      }
      onChange((await res.json()) as SecretState);
      setValue("");
      toast.success(next ? t("secret.saved") : t("secret.cleared"));
    } finally {
      setSaving(false);
    }
  }

  const admin = slot === "admin";
  const status =
    secret.source === "env"
      ? t("secret.fromEnv", { name: secret.env_name })
      : secret.source === "stored"
        ? t("secret.fromStore")
        : admin
          ? t("secret.admin.notSet")
          : t("secret.notSet");

  return (
    <Card className="space-y-3">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-fg-muted">
        <KeyRound className="h-4 w-4" /> {admin ? t("secret.admin.title") : t("secret.title")}
      </div>
      <div className="text-sm text-fg" data-testid={admin ? "secret-status-admin" : "secret-status"}>
        {status}
      </div>
      {secret.unreadable && <Alert kind="warn">{t("secret.unreadable")}</Alert>}
      {secret.source === "env" && secret.stored && (
        <div className="text-xs text-fg-dim">{t("secret.envWins", { name: secret.env_name })}</div>
      )}
      <form
        className="flex flex-wrap items-stretch gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (value) void save(value);
        }}
      >
        <Input
          type="password"
          autoComplete="new-password"
          aria-label={admin ? t("secret.admin.label") : t("secret.label")}
          placeholder={secret.stored ? t("secret.replace") : t("secret.label")}
          className="min-w-0 flex-1"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <Button type="submit" disabled={saving || !value}>
          {t("common:action.save")}
        </Button>
        {secret.stored && (
          <GhostButton type="button" disabled={saving} onClick={() => void save("")}>
            {t("secret.clear")}
          </GhostButton>
        )}
      </form>
      <div className="text-xs text-fg-dim">{admin ? t("secret.admin.hint") : t("secret.hint")}</div>
    </Card>
  );
}
