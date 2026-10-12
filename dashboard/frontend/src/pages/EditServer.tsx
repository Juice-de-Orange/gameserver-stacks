// "Edit server" (admin): change a registry row after creation, switch it off, remove it.
//
//  1. **One form schema, one validation function.** `EditForm`/`formFromServer` describe what the
//     page edits, `editProblems` is the only check in the browser (the backend checks again in
//     `routers/servers.validate_update`). Further fields and checks go into these two.
//  2. **What needs a new container says so before saving.** Image, ports and their bind address,
//     resources and (Project Zomboid) the Steam listing live in the container's create payload
//     (`services/container_spec.build_create_payload`). Saving such a change recreates the
//     container: a switched-on server through the version path (`POST …/recreate`: mandatory
//     backup, boot check, fallback), a switched-off one by replacing its stopped container
//     (`POST …/container {replace: true}`).
//  3. **Switching off stops the container** (`PUT {enabled: false}`), and only a switched-off
//     server can be removed — with the choice to keep or delete its data and the key typed as
//     confirmation (`POST …/remove`).
import { FormEvent, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Pencil, PowerOff, Save, Trash2 } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { tb } from "../i18n";
import { useConfirm, useToast } from "../components/feedback";
import { OperationPanel, OperationState, useOperation } from "../components/Operation";
import { RecreateNotApplied } from "../components/RecreateNotApplied";
import { useServers } from "../components/ServerContext";
import { operationForDisplay, SecretCard, SetupState, useProvision } from "../components/ServerSetup";
import { Alert, Badge, Button, Card, Field, GhostButton, Input, SectionTitle, Skeleton } from "../components/ui";
import { gameLabel, ServerInfo } from "../lib/game";
import { isBindAddress } from "../lib/net";
import type { Preset, PresetsResponse } from "./AddServer";

export interface EditForm {
  displayName: string;
  publicHost: string;
  imageRepo: string;
  imageTag: string;
  ports: Record<string, string>; // name → number (protocol stays as it is)
  bindAddress: string;
  ramMb: string;
  cpuCores: string;
  heapMaxMb: string;
  heapMinMb: string;
  bridgeIn: boolean;
  bridgeOut: boolean;
  announceTarget: boolean;
  presenceVisible: boolean;
}

const portNumber = (spec: string) => spec.split("/")[0] ?? "";
const portProto = (spec: string) => spec.split("/")[1] ?? "tcp";

export function formFromServer(s: ServerInfo): EditForm {
  const extra = s.extra ?? {};
  return {
    displayName: s.display_name,
    publicHost: typeof extra.public_host === "string" ? extra.public_host : "",
    imageRepo: s.image_repo,
    imageTag: s.image_tag,
    ports: Object.fromEntries(Object.entries(s.game_ports).map(([k, v]) => [k, portNumber(v)])),
    bindAddress: s.bind_address ?? "0.0.0.0",
    ramMb: String(s.ram_mb),
    cpuCores: String(s.cpu_cores),
    heapMaxMb: s.heap_max_mb == null ? "" : String(s.heap_max_mb),
    heapMinMb: s.heap_min_mb == null ? "" : String(s.heap_min_mb),
    bridgeIn: s.bridge_in,
    bridgeOut: s.bridge_out,
    announceTarget: s.announce_target,
    presenceVisible: s.presence_visible,
  };
}

const isPort = (v: string) => /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 65535;
const isWhole = (v: string) => /^\d+$/.test(v);
const isCores = (v: string) => /^\d+(\.\d+)?$/.test(v);

/** Everything that blocks saving, as catalog keys of `editServer` — empty means "ready". */
export function editProblems(s: ServerInfo, f: EditForm): string[] {
  const problems: string[] = [];
  if (s.management_mode === "dashboard") {
    if (!f.imageRepo.trim() || !f.imageTag.trim()) problems.push("problem.image");
    if (Object.values(f.ports).some((p) => !isPort(p))) problems.push("problem.port");
    if (!isBindAddress(f.bindAddress)) problems.push("problem.bindAddress");
    if (!isWhole(f.ramMb) || !isCores(f.cpuCores) || (f.heapMaxMb && !isWhole(f.heapMaxMb)) || (f.heapMinMb && !isWhole(f.heapMinMb)))
      problems.push("problem.resources");
  }
  if (f.publicHost && /\s|\//.test(f.publicHost.trim())) problems.push("problem.publicHost");
  return problems;
}

/** Fields of the `PUT` body that live in the container's create payload. */
const CONTAINER_FIELDS = ["image_repo", "image_tag", "game_ports", "bind_address", "ram_mb", "cpu_cores", "heap_max_mb", "heap_min_mb"];

/**
 * The `PUT /api/servers/<key>` body: only what changed, with the backend's field names.
 * `env` of an editable port follows its number (`extra.env`, e.g. Minecraft's `SERVER_PORT`).
 */
export function changesOf(s: ServerInfo, f: EditForm, preset: Preset | null) {
  const body: Record<string, unknown> = {};
  const extra = { ...(s.extra ?? {}) };
  let extraChanged = false;
  if (f.displayName.trim() !== s.display_name) body.display_name = f.displayName.trim();
  const host = f.publicHost.trim();
  if (host !== (typeof extra.public_host === "string" ? extra.public_host : "")) {
    if (host) extra.public_host = host;
    else delete extra.public_host;
    extraChanged = true;
  }
  if (s.management_mode === "dashboard") {
    if (f.imageRepo.trim() !== s.image_repo) body.image_repo = f.imageRepo.trim();
    if (f.imageTag.trim() !== s.image_tag) body.image_tag = f.imageTag.trim();
    const ports: Record<string, string> = {};
    let portsChanged = false;
    const env = { ...((extra.env as Record<string, string> | undefined) ?? {}) };
    for (const [name, spec] of Object.entries(s.game_ports)) {
      const number = f.ports[name] ?? portNumber(spec);
      ports[name] = `${number}/${portProto(spec)}`;
      if (number !== portNumber(spec)) {
        portsChanged = true;
        const envName = preset?.ports.find((p) => p.name === name)?.env;
        if (envName) {
          env[envName] = number;
          extra.env = env;
          extraChanged = true;
        }
      }
    }
    if (portsChanged) body.game_ports = ports;
    const bind = f.bindAddress.trim() || "0.0.0.0";
    if (bind !== (s.bind_address ?? "0.0.0.0")) body.bind_address = bind;
    if (Number(f.ramMb) !== s.ram_mb) body.ram_mb = Number(f.ramMb);
    if (Number(f.cpuCores) !== s.cpu_cores) body.cpu_cores = Number(f.cpuCores);
    const heapMax = f.heapMaxMb ? Number(f.heapMaxMb) : null;
    const heapMin = f.heapMinMb ? Number(f.heapMinMb) : null;
    if (heapMax !== s.heap_max_mb) body.heap_max_mb = heapMax;
    if (heapMin !== s.heap_min_mb) body.heap_min_mb = heapMin;
    if (f.presenceVisible !== s.presence_visible) body.presence_visible = f.presenceVisible;
  }
  if (f.bridgeIn !== s.bridge_in) body.bridge_in = f.bridgeIn;
  if (f.bridgeOut !== s.bridge_out) body.bridge_out = f.bridgeOut;
  if (f.announceTarget !== s.announce_target) body.announce_target = f.announceTarget;
  if (extraChanged) body.extra = extra;
  return body;
}

/** Does saving this body need a new container? (`presence_visible` is container env for PZ only.) */
export function needsRecreate(s: ServerInfo, body: Record<string, unknown>): boolean {
  if (s.management_mode !== "dashboard") return false;
  if (CONTAINER_FIELDS.some((k) => k in body)) return true;
  if ("presence_visible" in body && s.game_type === "pz") return true;
  // `extra.env` is container env, `extra.public_host` is not.
  const before = JSON.stringify(s.extra?.env ?? {});
  const after = body.extra ? JSON.stringify((body.extra as Record<string, unknown>).env ?? {}) : before;
  return before !== after;
}

export default function EditServer() {
  const { t } = useTranslation("editServer");
  const { serverKey = "" } = useParams();
  const { servers, loading } = useServers();
  const server = servers.find((s) => s.key === serverKey);

  if (!server) {
    return loading ? <Skeleton className="h-48" /> : <Alert kind="error">{t("notFound", { key: serverKey })}</Alert>;
  }
  // Remount per server and after every registry reload of it, so the form starts from the row.
  return <EditServerForm key={`${server.key}-${server.updated_at ?? ""}`} server={server} />;
}

function EditServerForm({ server }: { server: ServerInfo }) {
  const { t } = useTranslation(["editServer", "common"]);
  const toast = useToast();
  const confirm = useConfirm();
  const nav = useNavigate();
  const { reload } = useServers();
  const [f, setF] = useState<EditForm>(() => formFromServer(server));
  const [preset, setPreset] = useState<Preset | null>(null);
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [busy, setBusy] = useState(false);
  const [tried, setTried] = useState(false);
  const [error, setError] = useState("");
  const recreateOp = useOperation(server.enabled ? server.key : "", "version.recreate");
  const provision = useProvision(server.key, () => void reload());
  const name = server.display_name || gameLabel(server);
  const dashboard = server.management_mode === "dashboard";

  useEffect(() => {
    apiJson<PresetsResponse>("/servers/presets")
      .then((d) => setPreset(d.presets.find((p) => p.game_type === server.game_type) ?? null))
      .catch(() => setPreset(null));
    apiJson<SetupState>(`/servers/${server.key}/setup`)
      .then(setSetup)
      .catch(() => setSetup(null));
  }, [server.key, server.game_type]);

  const set = (patch: Partial<EditForm>) => setF((old) => ({ ...old, ...patch }));
  const problems = useMemo(() => editProblems(server, f), [server, f]);
  const body = useMemo(() => changesOf(server, f, preset), [server, f, preset]);
  const changed = Object.keys(body).length > 0;
  const containerExists = setup?.container.exists === true;
  const recreate = changed && containerExists && needsRecreate(server, body);
  const editable = (name: string) => {
    const p = preset?.ports.find((x) => x.name === name);
    return dashboard && !!p?.editable;
  };

  async function save(e: FormEvent) {
    e.preventDefault();
    setTried(true);
    if (problems.length || !changed) return;
    if (recreate) {
      const ok = await confirm({
        title: t("recreate.confirmTitle", { name }),
        body: server.enabled ? t("recreate.confirmOn") : t("recreate.confirmOff"),
        confirmLabel: t("recreate.confirm"),
      });
      if (!ok) return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await api(`/servers/${server.key}`, { method: "PUT", body: JSON.stringify(body) });
      if (!res.ok) {
        setError(await errText(res, t("saveFailed")));
        return;
      }
      if (recreate) {
        const path = server.enabled ? `/servers/${server.key}/recreate` : `/servers/${server.key}/container`;
        const opts = server.enabled ? { method: "POST" } : { method: "POST", body: JSON.stringify({ replace: true }) };
        const r = await api(path, opts);
        if (!r.ok) {
          setError(await errText(r, t("recreate.failed")));
        } else {
          const op = (await r.json()) as OperationState;
          if (server.enabled) recreateOp.track(op);
          else provision.track(op);
        }
      }
      toast.success(t("saved"));
      // Not awaited before the operation panel shows: the reload remounts this form.
      void reload();
    } finally {
      setBusy(false);
    }
  }

  async function switchOff() {
    const ok = await confirm({
      title: t("off.confirmTitle", { name }),
      body: t("off.confirmBody"),
      confirmLabel: t("off.confirm"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const res = await api(`/servers/${server.key}`, { method: "PUT", body: JSON.stringify({ enabled: false }) });
      if (!res.ok) {
        toast.error(await errText(res, t("off.failed")));
        return;
      }
      toast.success(t("off.done", { name }));
      await reload();
    } finally {
      setBusy(false);
    }
  }

  const recreateTitle = t("recreate.operation");
  const op = server.enabled ? recreateOp.op : operationForDisplay(provision.op);

  return (
    <div className="space-y-5" data-game={server.game_type}>
      <form onSubmit={save} className="space-y-5" noValidate>
        <SectionTitle
          icon={Pencil}
          sub={t("subtitle", { key: server.key, game: gameLabel(server) })}
          right={server.enabled ? <Badge tone="online">{t("badge.on")}</Badge> : <Badge tone="warn">{t("badge.off")}</Badge>}
        >
          {t("title", { name })}
        </SectionTitle>

        {!dashboard && <Alert kind="info">{t("compose")}</Alert>}

        <Card className="space-y-4">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.general")}</div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("field.displayName")}>
              <Input value={f.displayName} placeholder={gameLabel(server)} onChange={(e) => set({ displayName: e.target.value })} />
            </Field>
            <Field label={t("field.publicHost")} hint={t("hint.publicHost")}>
              <Input value={f.publicHost} placeholder="play.example.com" onChange={(e) => set({ publicHost: e.target.value })} />
            </Field>
          </div>
        </Card>

        {dashboard && (
          <Card className="space-y-4">
            <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.image")}</div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("field.imageRepo")}>
                <Input value={f.imageRepo} onChange={(e) => set({ imageRepo: e.target.value })} />
              </Field>
              <Field label={t("field.imageTag")} hint={preset ? tb(preset.image_help) : undefined}>
                <Input value={f.imageTag} onChange={(e) => set({ imageTag: e.target.value })} />
              </Field>
            </div>
          </Card>
        )}

        {dashboard && Object.keys(server.game_ports).length > 0 && (
          <Card className="space-y-4">
            <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.ports")}</div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("field.bindAddress")} hint={t("hint.bindAddress")}>
                <Input
                  aria-label={t("field.bindAddress")}
                  value={f.bindAddress}
                  placeholder="0.0.0.0"
                  onChange={(e) => set({ bindAddress: e.target.value.trim() })}
                />
              </Field>
              {Object.entries(server.game_ports).map(([portName, spec]) => {
                const help = preset?.ports.find((x) => x.name === portName)?.help;
                return (
                  <Field
                    key={portName}
                    label={t("field.port", { name: portName, proto: portProto(spec).toUpperCase() })}
                    hint={help ? tb(help) : undefined}
                  >
                    <Input
                      inputMode="numeric"
                      aria-label={t("field.port", { name: portName, proto: portProto(spec).toUpperCase() })}
                      value={f.ports[portName] ?? ""}
                      disabled={!editable(portName)}
                      onChange={(e) => set({ ports: { ...f.ports, [portName]: e.target.value.trim() } })}
                    />
                  </Field>
                );
              })}
            </div>
            {(server.game_type === "valheim" || server.game_type === "pz") && (
              <div className="text-xs text-fg-dim">{t(`hint.steamList.${server.game_type}`)}</div>
            )}
          </Card>
        )}

        {dashboard && (
          <Card className="space-y-4">
            <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.resources")}</div>
            <div className="grid gap-4 sm:grid-cols-4">
              <Field label={t("field.ramMb")} hint={t("hint.zero")}>
                <Input inputMode="numeric" value={f.ramMb} onChange={(e) => set({ ramMb: e.target.value.trim() })} />
              </Field>
              <Field label={t("field.cpuCores")} hint={t("hint.zero")}>
                <Input inputMode="decimal" value={f.cpuCores} onChange={(e) => set({ cpuCores: e.target.value.trim() })} />
              </Field>
              {server.heap_max_mb != null && (
                <Field label={t("field.heapMaxMb")}>
                  <Input inputMode="numeric" value={f.heapMaxMb} onChange={(e) => set({ heapMaxMb: e.target.value.trim() })} />
                </Field>
              )}
              {server.heap_min_mb != null && (
                <Field label={t("field.heapMinMb")}>
                  <Input inputMode="numeric" value={f.heapMinMb} onChange={(e) => set({ heapMinMb: e.target.value.trim() })} />
                </Field>
              )}
            </div>
          </Card>
        )}

        <Card className="space-y-3">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.cross")}</div>
          <div className="grid gap-2 text-sm text-fg sm:grid-cols-2">
            {server.capabilities.includes("announce") && (
              <>
                <label className="flex items-start gap-2">
                  <input type="checkbox" className="mt-1" checked={f.bridgeIn} onChange={(e) => set({ bridgeIn: e.target.checked })} />
                  <span>{t("flag.bridgeIn")}</span>
                </label>
                <label className="flex items-start gap-2">
                  <input type="checkbox" className="mt-1" checked={f.bridgeOut} onChange={(e) => set({ bridgeOut: e.target.checked })} />
                  <span>{t("flag.bridgeOut")}</span>
                </label>
                <label className="flex items-start gap-2">
                  <input type="checkbox" className="mt-1" checked={f.announceTarget} onChange={(e) => set({ announceTarget: e.target.checked })} />
                  <span>{t("flag.announceTarget")}</span>
                </label>
              </>
            )}
            {server.game_type === "pz" && dashboard && (
              <label className="flex items-start gap-2">
                <input type="checkbox" className="mt-1" checked={f.presenceVisible} onChange={(e) => set({ presenceVisible: e.target.checked })} />
                <span>{t("flag.steamListing")}</span>
              </label>
            )}
            {!server.capabilities.includes("announce") && server.game_type !== "pz" && (
              <div className="text-fg-dim">{t("flag.none")}</div>
            )}
          </div>
        </Card>

        {tried && problems.length > 0 && (
          <Alert kind="warn">
            <ul className="list-inside list-disc">
              {problems.map((x) => (
                <li key={x}>{t(x)}</li>
              ))}
            </ul>
          </Alert>
        )}
        {recreate && <Alert kind="warn">{server.enabled ? t("recreate.noteOn") : t("recreate.noteOff")}</Alert>}
        {error && <Alert kind="error">{error}</Alert>}
        {/* After a rolled-back recreate the values above are saved but not running. Mounted anew
            when a recreate finishes, so it asks the history again. */}
        {server.enabled && !op?.running && <RecreateNotApplied serverKey={server.key} />}
        {op && <OperationPanel op={op} title={recreateTitle} />}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={busy || !changed}>
            <Save className="h-4 w-4" /> {recreate ? t("saveAndRecreate") : t("common:action.save")}
          </Button>
          <Link to={`/s/${server.key}`}>
            <GhostButton type="button">{t("common:action.back")}</GhostButton>
          </Link>
          {!changed && <span className="text-xs text-fg-dim">{t("unchanged")}</span>}
        </div>
      </form>

      {dashboard && server.console_kind !== "none" && setup && (
        <SecretCard serverKey={server.key} secret={setup.secret} onChange={(secret) => setSetup({ ...setup, secret })} />
      )}
      {dashboard && setup?.secrets?.admin && (
        <SecretCard
          serverKey={server.key}
          slot="admin"
          secret={setup.secrets.admin}
          onChange={(admin) => setSetup({ ...setup, secrets: { ...setup.secrets, admin } })}
        />
      )}

      <Card className="space-y-3">
        <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-fg-muted">
          <PowerOff className="h-4 w-4" /> {t("off.title")}
        </div>
        {server.enabled ? (
          <>
            <div className="text-sm text-fg-muted">{t("off.body")}</div>
            <div>
              <GhostButton type="button" disabled={busy} onClick={() => void switchOff()}>
                <PowerOff className="h-4 w-4" /> {t("off.button")}
              </GhostButton>
            </div>
          </>
        ) : (
          <div className="text-sm text-fg-muted">
            {t("off.isOff")}{" "}
            <Link to={`/s/${server.key}`} className="font-semibold text-primary-text underline-offset-2 hover:underline">
              {t("off.setupLink")}
            </Link>
          </div>
        )}
      </Card>

      <RemoveCard key={server.key} server={server} onRemoved={async () => {
        await reload();
        nav("/");
      }} />
    </div>
  );
}

function RemoveCard({ server, onRemoved }: { server: ServerInfo; onRemoved: () => Promise<void> }) {
  const { t } = useTranslation("editServer");
  const toast = useToast();
  const [deleteData, setDeleteData] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function remove() {
    setBusy(true);
    setError("");
    try {
      const res = await api(`/servers/${server.key}/remove`, {
        method: "POST",
        body: JSON.stringify({ confirm_key: typed.trim(), delete_data: deleteData }),
      });
      if (!res.ok) {
        setError(await errText(res, t("remove.failed")));
        return;
      }
      toast.success(deleteData ? t("remove.doneDeleted", { key: server.key }) : t("remove.doneKept", { key: server.key }));
      await onRemoved();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="space-y-3 border-danger-border">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-danger-text">
        <Trash2 className="h-4 w-4" /> {t("remove.title")}
      </div>
      {server.enabled ? (
        <div className="text-sm text-fg-muted">{t("remove.switchOffFirst")}</div>
      ) : (
        <>
          <div className="text-sm text-fg-muted">{t("remove.body")}</div>
          <div className="space-y-1 text-sm text-fg">
            <label className="flex items-start gap-2">
              <input type="radio" className="mt-1" name="remove-data" checked={!deleteData} onChange={() => setDeleteData(false)} />
              <span>{t("remove.keep", { path: server.data_dir || "—" })}</span>
            </label>
            {server.management_mode === "dashboard" && (
              <label className="flex items-start gap-2">
                <input type="radio" className="mt-1" name="remove-data" checked={deleteData} onChange={() => setDeleteData(true)} />
                <span>{t("remove.delete", { path: server.data_dir || "—" })}</span>
              </label>
            )}
          </div>
          <Field label={t("remove.typeKey", { key: server.key })}>
            <Input
              value={typed}
              autoComplete="off"
              aria-label={t("remove.typeKey", { key: server.key })}
              onChange={(e) => setTyped(e.target.value)}
            />
          </Field>
          {error && <Alert kind="error">{error}</Alert>}
          <div>
            <Button type="button" tone="danger" disabled={busy || typed.trim() !== server.key} onClick={() => void remove()}>
              <Trash2 className="h-4 w-4" /> {deleteData ? t("remove.buttonDelete") : t("remove.buttonKeep")}
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}
