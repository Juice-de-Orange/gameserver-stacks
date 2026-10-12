// "Add server" (admin): choose the game, check the preset's values, create the registry row.
//
//  1. **The values come from the backend** (`GET /api/servers/presets`, `services/server_presets.py`),
//     each traceable to the game's driver or its block under `games/`. Nothing here invents a
//     port or an image tag; the page only lets the admin change them.
//  2. **Created switched off.** `POST /api/servers` with `enabled=false`: the row exists, the
//     container does not. The server's own page shows the next steps (`ServerSetup`).
//  3. **A secret is a value or a name, never shown again.** Like `ServerCreate`: `rcon_password`
//     is typed here and stored encrypted (`services/secrets.py`, the default), or
//     `rcon_password_env` names a variable from the `.env`. The API never returns the value.
import { FormEvent, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { ExternalLink, Plus, Server } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { tb } from "../i18n";
import { GameEmblem } from "../components/game/GameEmblem";
import { useServers } from "../components/ServerContext";
import { Alert, Badge, Button, Card, cx, Field, GhostButton, Input, SectionTitle, Skeleton } from "../components/ui";
import { GAME_LABEL, GameType } from "../lib/game";
import { isBindAddress } from "../lib/net";
import { safeUrl } from "../lib/url";

interface PresetPort {
  name: string;
  port: number;
  proto: string;
  editable: boolean;
  env: string;
  help: string;
}

interface PresetValue {
  name: string;
  value: string;
  target: "env" | "extra";
  secret: boolean;
  required: boolean;
  help: string;
}

interface Acknowledgement {
  id: string;
  url: string;
  help: string;
}

export interface Preset {
  game_type: GameType;
  image_repo: string;
  image_tag: string;
  image_help: string;
  ports: PresetPort[];
  console_kind: string;
  rcon_port: number;
  secret_env: string;
  secret_required: boolean;
  secret_help: string;
  game_version: string;
  version_help: string;
  version_required: boolean;
  server_type: string;
  ram_mb: number;
  heap_max_mb: number | null;
  heap_min_mb: number | null;
  memory_help: string;
  values: PresetValue[];
  acknowledgements: Acknowledgement[];
}

export interface PresetsResponse {
  key_pattern: string;
  data_root: string;
  network: string;
  presets: Preset[];
}

interface FormState {
  key: string;
  displayName: string;
  containerName: string | null; // null = follows the key
  dataDir: string | null;       // null = follows the key
  imageRepo: string;
  imageTag: string;
  gameVersion: string;
  serverType: string;
  ports: Record<string, string>;
  bindAddress: string;
  rconPort: string;
  secretMode: "env" | "value";
  secretEnv: string;
  secretValue: string;
  ramMb: string;
  heapMaxMb: string;
  heapMinMb: string;
  values: Record<string, string>;
  accepted: Record<string, boolean>;
}

function initialForm(p: Preset): FormState {
  return {
    key: "",
    displayName: "",
    containerName: null,
    dataDir: null,
    imageRepo: p.image_repo,
    imageTag: p.image_tag,
    gameVersion: p.game_version,
    serverType: p.server_type,
    ports: Object.fromEntries(p.ports.map((x) => [x.name, String(x.port)])),
    bindAddress: "0.0.0.0",
    rconPort: String(p.rcon_port),
    secretMode: "value",
    secretEnv: p.secret_env,
    secretValue: "",
    ramMb: String(p.ram_mb),
    heapMaxMb: p.heap_max_mb == null ? "" : String(p.heap_max_mb),
    heapMinMb: p.heap_min_mb == null ? "" : String(p.heap_min_mb),
    values: Object.fromEntries(p.values.map((v) => [v.name, v.value])),
    accepted: Object.fromEntries(p.acknowledgements.map((a) => [a.id, false])),
  };
}

const isPort = (v: string) => /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 65535;
const isWhole = (v: string) => /^\d+$/.test(v);

/** Everything that blocks creating, as catalog keys of `addServer` — empty means "ready". */
export function formProblems(p: Preset, f: FormState, keyPattern: string): string[] {
  const problems: string[] = [];
  if (!new RegExp(keyPattern).test(f.key)) problems.push("problem.key");
  if (!f.imageRepo.trim() || !f.imageTag.trim()) problems.push("problem.image");
  if (p.version_required && !f.gameVersion.trim()) problems.push("problem.version");
  if (p.ports.some((x) => !isPort(f.ports[x.name] ?? ""))) problems.push("problem.port");
  if (!isBindAddress(f.bindAddress)) problems.push("problem.bindAddress");
  if (p.console_kind !== "none") {
    if (!isPort(f.rconPort)) problems.push("problem.rconPort");
    const secret = f.secretMode === "env" ? f.secretEnv.trim() : f.secretValue;
    if (p.secret_required && !secret) problems.push("problem.secret");
  }
  if (!isWhole(f.ramMb) || (f.heapMaxMb && !isWhole(f.heapMaxMb)) || (f.heapMinMb && !isWhole(f.heapMinMb)))
    problems.push("problem.memory");
  if (p.values.some((v) => v.required && !(f.values[v.name] ?? "").trim())) problems.push("problem.value");
  if (p.acknowledgements.some((a) => !f.accepted[a.id])) problems.push("problem.accept");
  return problems;
}

/** The `POST /api/servers` body — the same field names as `ServerCreate`. */
export function createBody(p: Preset, f: FormState, dataRoot: string) {
  const key = f.key.trim();
  const containerName = (f.containerName ?? key).trim();
  const env: Record<string, string> = {};
  const extra: Record<string, unknown> = {};
  const gamePorts: Record<string, string> = {};
  for (const x of p.ports) {
    const number = f.ports[x.name] ?? String(x.port);
    gamePorts[x.name] = `${number}/${x.proto}`;
    // The game must LISTEN on the published port (host port == container port), and for these
    // games only a container variable tells it so.
    if (x.env) env[x.env] = number;
  }
  for (const v of p.values) {
    const value = f.values[v.name] ?? "";
    if (v.target === "env") env[v.name] = value;
    else if (value) extra[v.name] = value;
  }
  if (Object.keys(env).length) extra.env = env;
  const hasConsole = p.console_kind !== "none";
  return {
    key,
    game_type: p.game_type,
    display_name: f.displayName.trim(),
    enabled: false,
    container_name: containerName,
    data_dir: (f.dataDir ?? `${dataRoot}/${key}`).trim(),
    image_repo: f.imageRepo.trim(),
    image_tag: f.imageTag.trim(),
    console_kind: p.console_kind,
    // Same network as the dashboard, so the container name resolves.
    rcon_host: hasConsole ? containerName : "",
    rcon_port: hasConsole ? Number(f.rconPort) : 0,
    rcon_password_env: hasConsole && f.secretMode === "env" ? f.secretEnv.trim() : "",
    rcon_password: hasConsole && f.secretMode === "value" ? f.secretValue : "",
    game_ports: gamePorts,
    bind_address: f.bindAddress.trim() || "0.0.0.0",
    game_version: f.gameVersion.trim(),
    server_type: f.serverType.trim(),
    ram_mb: Number(f.ramMb),
    heap_max_mb: f.heapMaxMb ? Number(f.heapMaxMb) : null,
    heap_min_mb: f.heapMinMb ? Number(f.heapMinMb) : null,
    extra,
    // Checked server-side too: a Minecraft row is refused without it (`ServerCreate`).
    eula_accepted: p.acknowledgements.some((a) => a.id === "eula") && !!f.accepted.eula,
  };
}

export default function AddServer() {
  const { t } = useTranslation("addServer");
  const [data, setData] = useState<PresetsResponse | null>(null);
  const [error, setError] = useState("");
  const [game, setGame] = useState<GameType | null>(null);

  useEffect(() => {
    apiJson<PresetsResponse>("/servers/presets")
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);

  const preset = data?.presets.find((p) => p.game_type === game) ?? null;

  return (
    <div className="space-y-5">
      <SectionTitle icon={Plus} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {error && <Alert kind="error">{error}</Alert>}
      {!data && !error && <Skeleton className="h-48" />}

      {/* Presets come for active games only (`services/game_settings.py`). */}
      {data && data.presets.length === 0 && (
        <Alert kind="warn">
          {t("noGames")}{" "}
          <Link to="/settings/games" className="font-semibold underline-offset-2 hover:underline">
            {t("noGames.link")}
          </Link>
        </Alert>
      )}

      {data && data.presets.length > 0 && (
        <Card className="space-y-3">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.game")}</div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {data.presets.map((p) => (
              <button
                key={p.game_type}
                type="button"
                data-game={p.game_type}
                aria-pressed={game === p.game_type}
                onClick={() => setGame(p.game_type)}
                className={cx(
                  "flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition",
                  game === p.game_type ? "border-primary/50 bg-primary-soft" : "border-line hover:bg-surface-sunken",
                )}
              >
                <span
                  className="grid h-9 w-9 shrink-0 place-items-center border border-primary/30 bg-primary/[0.12] text-primary"
                  style={{ borderRadius: "var(--g-radius)" }}
                >
                  <GameEmblem game={p.game_type} className="h-5 w-5" strokeWidth={2} />
                </span>
                <span className="font-semibold text-fg">{GAME_LABEL[p.game_type]}</span>
              </button>
            ))}
          </div>
        </Card>
      )}

      {/* The key forces a fresh form per game: values typed for one game never leak into another. */}
      {data && preset && <PresetForm key={preset.game_type} preset={preset} meta={data} />}
    </div>
  );
}

function PresetForm({ preset: p, meta }: { preset: Preset; meta: PresetsResponse }) {
  const { t } = useTranslation("addServer");
  const nav = useNavigate();
  const { reload } = useServers();
  const [f, setF] = useState<FormState>(() => initialForm(p));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tried, setTried] = useState(false);

  const problems = useMemo(() => formProblems(p, f, meta.key_pattern), [p, f, meta.key_pattern]);
  const set = (patch: Partial<FormState>) => setF((old) => ({ ...old, ...patch }));
  const key = f.key.trim();
  const hasConsole = p.console_kind !== "none";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setTried(true);
    if (problems.length) return;
    setBusy(true);
    setError("");
    try {
      const res = await api("/servers", {
        method: "POST",
        body: JSON.stringify(createBody(p, f, meta.data_root)),
      });
      if (!res.ok) {
        setError(await errText(res, t("createFailed")));
        return;
      }
      await reload();
      nav(`/s/${key}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5" data-game={p.game_type} noValidate>
      <Card className="space-y-4">
        <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.identity")}</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("field.key")} hint={t("hint.key")}>
            <Input
              value={f.key}
              onChange={(e) => set({ key: e.target.value.toLowerCase() })}
              placeholder={t("placeholder.key")}
              aria-invalid={tried && problems.includes("problem.key")}
              autoComplete="off"
            />
          </Field>
          <Field label={t("field.displayName")} hint={t("hint.displayName")}>
            <Input value={f.displayName} onChange={(e) => set({ displayName: e.target.value })} placeholder={GAME_LABEL[p.game_type]} />
          </Field>
          <Field label={t("field.containerName")} hint={t("hint.containerName")}>
            <Input value={f.containerName ?? key} onChange={(e) => set({ containerName: e.target.value })} />
          </Field>
          <Field label={t("field.dataDir")} hint={t("hint.dataDir", { root: meta.data_root })}>
            <Input value={f.dataDir ?? `${meta.data_root}/${key}`} onChange={(e) => set({ dataDir: e.target.value })} />
          </Field>
        </div>
      </Card>

      <Card className="space-y-4">
        <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.image")}</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("field.imageRepo")}>
            <Input value={f.imageRepo} onChange={(e) => set({ imageRepo: e.target.value })} />
          </Field>
          <Field label={t("field.imageTag")} hint={tb(p.image_help)}>
            <Input value={f.imageTag} onChange={(e) => set({ imageTag: e.target.value })} />
          </Field>
          {(p.game_version || p.version_required) && (
            <Field label={t("field.gameVersion")} hint={tb(p.version_help)}>
              <Input value={f.gameVersion} onChange={(e) => set({ gameVersion: e.target.value })} />
            </Field>
          )}
          {p.server_type && (
            <Field label={t("field.serverType")}>
              <Input value={f.serverType} onChange={(e) => set({ serverType: e.target.value })} />
            </Field>
          )}
        </div>
      </Card>

      <Card className="space-y-4">
        <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.network")}</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("field.bindAddress")} hint={t("hint.bindAddress")}>
            <Input
              aria-label={t("field.bindAddress")}
              value={f.bindAddress}
              placeholder="0.0.0.0"
              onChange={(e) => set({ bindAddress: e.target.value.trim() })}
            />
          </Field>
          {p.ports.map((x) => (
            <Field
              key={x.name}
              label={t("field.port", { name: x.name, proto: x.proto.toUpperCase() })}
              hint={tb(x.help)}
            >
              <Input
                inputMode="numeric"
                value={f.ports[x.name] ?? ""}
                disabled={!x.editable}
                onChange={(e) => set({ ports: { ...f.ports, [x.name]: e.target.value.trim() } })}
              />
            </Field>
          ))}
        </div>
      </Card>

      {hasConsole && (
        <Card className="space-y-4">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.console")}</div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("field.rconPort")} hint={t("hint.rconPort")}>
              <Input inputMode="numeric" value={f.rconPort} onChange={(e) => set({ rconPort: e.target.value.trim() })} />
            </Field>
            <div className="space-y-1">
              <span className="text-xs font-medium uppercase tracking-wide text-fg-muted">
                {p.secret_required ? t("field.secretRequired") : t("field.secret")}
              </span>
              <div className="flex flex-wrap gap-3 text-sm text-fg">
                <label className="flex items-center gap-1.5">
                  <input type="radio" checked={f.secretMode === "value"} onChange={() => set({ secretMode: "value" })} />
                  {t("secret.value")}
                </label>
                <label className="flex items-center gap-1.5">
                  <input type="radio" checked={f.secretMode === "env"} onChange={() => set({ secretMode: "env" })} />
                  {t("secret.env")}
                </label>
              </div>
              {f.secretMode === "env" ? (
                <Input
                  aria-label={t("secret.envLabel")}
                  value={f.secretEnv}
                  onChange={(e) => set({ secretEnv: e.target.value.trim() })}
                  autoComplete="off"
                />
              ) : (
                <Input
                  type="password"
                  aria-label={t("secret.valueLabel")}
                  value={f.secretValue}
                  onChange={(e) => set({ secretValue: e.target.value })}
                  autoComplete="new-password"
                />
              )}
              <span className="block text-xs text-fg-dim">
                {f.secretMode === "env" ? t("secret.envHint") : t("secret.valueHint")} {tb(p.secret_help)}
              </span>
            </div>
          </div>
        </Card>
      )}

      <Card className="space-y-4">
        <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.resources")}</div>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label={t("field.ramMb")} hint={t("hint.ramMb")}>
            <Input inputMode="numeric" value={f.ramMb} onChange={(e) => set({ ramMb: e.target.value.trim() })} />
          </Field>
          {p.heap_max_mb != null && (
            <Field label={t("field.heapMaxMb")} hint={tb(p.memory_help)}>
              <Input inputMode="numeric" value={f.heapMaxMb} onChange={(e) => set({ heapMaxMb: e.target.value.trim() })} />
            </Field>
          )}
          {p.heap_min_mb != null && (
            <Field label={t("field.heapMinMb")}>
              <Input inputMode="numeric" value={f.heapMinMb} onChange={(e) => set({ heapMinMb: e.target.value.trim() })} />
            </Field>
          )}
        </div>
      </Card>

      {p.values.length > 0 && (
        <Card className="space-y-4">
          <div className="text-xs font-medium uppercase tracking-wide text-fg-muted">{t("step.settings")}</div>
          <div className="grid gap-4 sm:grid-cols-2">
            {p.values.map((v) => (
              <Field key={v.name} label={v.required ? t("field.required", { name: v.name }) : v.name} hint={tb(v.help)}>
                <Input
                  type={v.secret ? "password" : "text"}
                  autoComplete={v.secret ? "new-password" : "off"}
                  value={f.values[v.name] ?? ""}
                  onChange={(e) => set({ values: { ...f.values, [v.name]: e.target.value } })}
                />
              </Field>
            ))}
          </div>
        </Card>
      )}

      {p.acknowledgements.map((a) => (
        <Card key={a.id} className="space-y-2">
          <label className="flex items-start gap-2 text-sm text-fg">
            <input
              type="checkbox"
              className="mt-1"
              checked={!!f.accepted[a.id]}
              onChange={(e) => set({ accepted: { ...f.accepted, [a.id]: e.target.checked } })}
            />
            <span>{t(`accept.${a.id}`)}</span>
          </label>
          <a href={safeUrl(a.url)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-primary-text underline">
            {a.url} <ExternalLink className="h-3.5 w-3.5" />
          </a>
          <div className="text-xs text-fg-dim">{tb(a.help)}</div>
        </Card>
      ))}

      {tried && problems.length > 0 && (
        <Alert kind="warn">
          <ul className="list-inside list-disc">
            {problems.map((x) => (
              <li key={x}>{t(x)}</li>
            ))}
          </ul>
        </Alert>
      )}
      {error && <Alert kind="error">{error}</Alert>}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={busy}>
          <Server className="h-4 w-4" /> {t("create")}
        </Button>
        <Link to="/">
          <GhostButton type="button">{t("common:action.cancel")}</GhostButton>
        </Link>
        <Badge tone="info">{t("createdOff")}</Badge>
      </div>
    </form>
  );
}
