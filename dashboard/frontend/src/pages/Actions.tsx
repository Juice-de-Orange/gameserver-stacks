import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, HelpCircle, Swords, Zap } from "lucide-react";
import { apiJson } from "../api";
import { useActiveServer } from "../components/ServerContext";
import { useConfirm, useToast } from "../components/feedback";
import { tb } from "../i18n";
import { Alert, Badge, Button, Card, EmptyState, Field, Input, SectionTitle, Select } from "../components/ui";

interface Param {
  name: string;
  kind: string;
  label: string;
  required: boolean;
  default: string;
  placeholder: string;
  choices: string[];
}
interface Action {
  id: string;
  label: string;
  group: string;
  template: string;
  params: Param[];
  needs_online_player: boolean;
  danger: boolean;
  note: string;
}

// Reference link per parameter (by parameter name) — only relevant for Project Zomboid.
// `text` is a catalog key: translated when rendering, not at import.
const PARAM_HELP: Record<string, { text: string; url: string }> = {
  item: { text: "help.item", url: "https://pzwiki.net/wiki/Items" },
  vehicle: { text: "help.vehicle", url: "https://pzwiki.net/wiki/Vehicles" },
  perk: { text: "help.perk", url: "https://pzwiki.net/wiki/Skills" },
  coords: { text: "help.coords", url: "https://map.projectzomboid.com/" },
};

function ActionCard({
  action,
  online,
  helpEnabled,
  onRun,
  running,
}: {
  action: Action;
  online: string[];
  helpEnabled: boolean;
  onRun: (a: Action, params: Record<string, string>) => void;
  running: boolean;
}) {
  const { t } = useTranslation("actions");
  const init = useMemo(() => {
    const o: Record<string, string> = {};
    for (const p of action.params) o[p.name] = p.default || "";
    return o;
  }, [action]);
  const [vals, setVals] = useState<Record<string, string>>(init);
  const set = (k: string, v: string) => setVals((s) => ({ ...s, [k]: v }));

  return (
    <div className="rounded-xl border border-line bg-surface-sunken/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-fg">{tb(action.label)}</span>
          {action.danger && <Badge tone="warn">{t("badge.danger")}</Badge>}
        </div>
        <code className="font-mono text-[11px] text-fg-dim">{action.id}</code>
      </div>

      {action.params.length > 0 && (
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {action.params.map((p) => (
            <Field key={p.name} label={tb(p.label) || p.name}>
              {p.kind === "player" ? (
                <Select value={vals[p.name]} onChange={(e) => set(p.name, e.target.value)} className="w-full">
                  <option value="">{t("select.player")}</option>
                  {online.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </Select>
              ) : p.kind === "enum" ? (
                <Select value={vals[p.name]} onChange={(e) => set(p.name, e.target.value)} className="w-full">
                  <option value="">{t("select.choose")}</option>
                  {p.choices.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              ) : p.kind === "toggle" ? (
                <Select value={vals[p.name]} onChange={(e) => set(p.name, e.target.value)} className="w-full">
                  <option value="true">{t("toggle.on")}</option>
                  <option value="false">{t("toggle.off")}</option>
                </Select>
              ) : (
                <Input
                  value={vals[p.name]}
                  placeholder={tb(p.placeholder)}
                  onChange={(e) => set(p.name, e.target.value)}
                />
              )}
              {helpEnabled && PARAM_HELP[p.name] && (
                <a
                  href={PARAM_HELP[p.name].url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 flex items-center gap-1 text-xs text-primary-text hover:underline"
                >
                  <HelpCircle className="h-3 w-3 shrink-0" /> {t(PARAM_HELP[p.name].text)}
                </a>
              )}
            </Field>
          ))}
        </div>
      )}

      {action.note && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-warning-text">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {tb(action.note)}
        </p>
      )}

      <div className="mt-2.5">
        <Button
          tone={action.danger ? "danger" : "primary"}
          onClick={() => onRun(action, vals)}
          disabled={running}
          className="text-xs"
        >
          <Zap className="h-4 w-4" /> {running ? t("run.busy") : t("run.label")}
        </Button>
      </div>
    </div>
  );
}

export default function Actions() {
  const { t } = useTranslation("actions");
  const server = useActiveServer();
  const key = server?.key;
  const confirm = useConfirm();
  const toast = useToast();
  const [catalog, setCatalog] = useState<Action[]>([]);
  const [over, setOver] = useState<any>(null);
  const [err, setErr] = useState("");
  const [log, setLog] = useState<string[]>([]);
  // Which action is running: a double tap would otherwise send the RCON command twice.
  const [runningId, setRunningId] = useState<string | null>(null);

  const online: string[] = over?.players?.names || [];
  // Three states, not two: not loaded yet · loaded · fetch failed. An empty `online` alone must
  // not be shown as "no player online" — neither during the first fetch nor after a failed one.
  const [overviewError, setOverviewError] = useState("");

  async function loadOverview() {
    if (!key) return;
    try {
      setOver(await apiJson<any>(`/servers/${key}/overview`));
      setOverviewError("");
    } catch (e: any) {
      setOverviewError(e.message);
    }
  }

  useEffect(() => {
    if (!key) return;
    setOver(null);
    setOverviewError("");
    apiJson<{ actions: Action[] }>(`/servers/${key}/actions/catalog`)
      .then((r) => setCatalog(r.actions))
      .catch((e) => setErr(e.message));
    loadOverview();
    // Keep the player list fresh: whoever joins after the page load must show up in the
    // selects. Same interval as the app-wide overview.
    const t = setInterval(loadOverview, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const groups = useMemo(() => {
    const g: Record<string, Action[]> = {};
    for (const a of catalog) (g[a.group] ||= []).push(a);
    return g;
  }, [catalog]);

  async function run(action: Action, params: Record<string, string>) {
    // Without the lock a double tap runs the RCON command TWICE (two spawned items, two weather
    // toggles) — the confirmation dialog of the dangerous actions only stops the first click.
    if (!key || runningId) return;
    if (action.danger) {
      const ok = await confirm({
        title: t("confirm.title", { action: tb(action.label) }),
        body: tb(action.note) || t("confirm.body"),
        danger: true,
        confirmLabel: t("run.label"),
      });
      if (!ok) return;
    }
    setRunningId(action.id);
    try {
      const r = await apiJson<{ command: string; output: string }>(`/servers/${key}/actions/run`, {
        method: "POST",
        body: JSON.stringify({ action_id: action.id, params }),
      });
      toast.success(t("run.done", { command: r.command }));
      setLog((l) => [`$ ${r.command}\n${r.output || t("log.noOutput")}`, ...l].slice(0, 20));
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setRunningId(null);
    }
  }

  return (
    <div className="space-y-5">
      <SectionTitle icon={Swords} sub={`${t("subtitle")}${server ? ` · ${server.display_name}` : ""}`}>
        {t("title")}
      </SectionTitle>

      {err && <Alert>{err}</Alert>}
      {over && !over?.container?.running && (
        <Alert kind="warn">{t("alert.offline")}</Alert>
      )}
      {overviewError && (
        <Alert kind="error">
          {t("alert.playersFailed", { error: overviewError })}
        </Alert>
      )}
      {!over && !overviewError && (
        <Alert kind="info">{t("alert.playersLoading")}</Alert>
      )}
      {over && !overviewError && online.length === 0 && (
        <Alert kind="info">
          {t("alert.nobodyOnline")}
        </Alert>
      )}

      {Object.entries(groups).map(([group, actions]) => (
        <Card key={group} className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tb(group)}</div>
          <div className="grid gap-3 md:grid-cols-2">
            {actions.map((a) => (
              <ActionCard
                key={a.id}
                action={a}
                online={online}
                helpEnabled={server?.game_type === "pz"}
                onRun={run}
                running={runningId === a.id}
              />
            ))}
          </div>
        </Card>
      ))}

      {catalog.length === 0 && !err && (
        <Card>
          <EmptyState icon={Swords}>{t("catalog.loading")}</EmptyState>
        </Card>
      )}

      {log.length > 0 && (
        <Card className="!p-0">
          <div className="border-b border-line px-4 py-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            {t("log.title")}
          </div>
          <div className="max-h-72 space-y-2 overflow-auto p-3 font-mono text-xs text-fg-muted">
            {log.map((l, i) => (
              <pre key={i} className="whitespace-pre-wrap border-b border-line pb-2">
                {l}
              </pre>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
