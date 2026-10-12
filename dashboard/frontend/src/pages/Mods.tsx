import { useEffect, useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  GripVertical,
  Info,
  Map as MapIcon,
  MapPin,
  Package,
  Power,
  PowerOff,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  Wand2,
  X,
} from "lucide-react";
import { api, apiJson, detailText, errText } from "../api";
import { ModFindings, PreflightData } from "../components/ModFindings";
import { ModPortal } from "../components/ModPortal";
import { safeUrl } from "../lib/url";
import { OperationPanel, useOperation } from "../components/Operation";
import PzMods from "./PzMods";

interface ForeignMod {
  filename: string;
  enabled: boolean;
  size: number;
  mod_id: string;
  name: string;
  version: string;
  loader: string;
  game_versions: string[];
  problems: string[];
}

interface ForeignData {
  loader: string;
  directory: string;
  mods: ForeignMod[];
  preflight: PreflightData;
  /** Upload limits — supplied by the backend, so that no second truth arises here. */
  upload?: { max_bytes: number; extensions: string[] };
}
import { useOverview } from "../components/overview";
import { useActiveServer } from "../components/ServerContext";
import { useConfirm, useToast } from "../components/feedback";
import { useLiveConsole } from "../components/liveconsole";
import { fmtBytes } from "../lib/format";
import { ServerInfo } from "../lib/game";
import { Alert, Badge, Button, Card, EmptyState, GhostButton, Input, SectionTitle, Skeleton, Spinner, cx } from "../components/ui";
import i18n, { tb } from "../i18n";

/** Picks the mods page: PZ = Steam Workshop management, Reforger = preset catalog,
 * Avorion = Workshop IDs, every other game = file list. */
export default function Mods() {
  const server = useActiveServer();
  // `key` as with `PzConfig`: these components hold state across a server switch —
  // `GenericMods` even the selected mod files (`sel`). Without a remount ONE server's selection
  // would go to the OTHER, mandatory backup and boot proof included.
  if (server && server.game_type === "arma_reforger")
    return <ReforgerMods key={server.key} server={server} />;
  // Avorion downloads its mods from Steam itself — there are no files, only Workshop IDs.
  if (server && server.game_type === "avorion")
    return <AvorionMods key={server.key} server={server} />;
  if (server && server.game_type !== "pz") return <GenericMods key={server.key} server={server} />;
  return <PzMods />;
}

/** File-based mod management for the games that load mods from a folder.
 *
 * As with Project Zomboid: uploading and toggling do NOT change the running server — the
 * selection only goes live with "Apply", including mandatory backup, boot proof and automatic
 * fallback. */
function GenericMods({ server }: { server: ServerInfo }) {
  const key = server.key;
  const { t } = useTranslation("mods");
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<ForeignData | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const { op, track } = useOperation(key, "mods.files.apply");

  /**
   * `keepSelection` is not a convenience but the separation this page rests on: the
   * checkboxes are the DRAFT, which only goes live with "Apply". A plain `load()` after an
   * upload would silently reset the draft to the live state — deselected mods would keep
   * running while the page shows no pending change.
   */
  async function load(keepSelection = false) {
    try {
      const d = await apiJson<ForeignData>(`/servers/${key}/mods/files`);
      setData(d);
      if (!keepSelection) {
        setSel(new Set(d.mods.filter((m) => m.enabled).map((m) => m.filename)));
      } else {
        // The new file is stored disabled and does not belong in the draft; everything else
        // stays as the user set it. Files that have disappeared meanwhile are dropped, otherwise
        // the draft would point at nothing.
        const present = new Set(d.mods.map((m) => m.filename));
        setSel((prev) => new Set([...prev].filter((f) => present.has(f))));
      }
      setErr("");
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => {
    if (op && !op.running) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [op?.id, op?.running]);

  const live = new Set((data?.mods || []).filter((m) => m.enabled).map((m) => m.filename));
  const dirty = sel.size !== live.size || [...sel].some((f) => !live.has(f));

  /**
   * The check of the PLANNED selection.
   *
   * `data.preflight` from `GET /mods/files` is computed over the files ACTIVE on disk. Hanging
   * the apply button on it would kill exactly the button that repairs a broken live state:
   * deselecting would not lift the lock, because that check does not know the selection.
   */
  const [draftCheck, setDraftCheck] = useState<PreflightData | null>(null);
  const selection = [...sel].sort().join("|");
  useEffect(() => {
    if (!key || !data) return;
    let current = true;
    apiJson<PreflightData>(`/servers/${key}/mods/files/check`, {
      method: "POST",
      body: JSON.stringify({ enabled: [...sel] }),
    })
      .then((p) => current && setDraftCheck(p))
      .catch(() => current && setDraftCheck(null));   // when in doubt, do not lock
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, selection, data?.mods?.length]);

  // What the user sees and what the button depends on is the same thing: the draft.
  const check = draftCheck ?? data?.preflight ?? null;

  async function upload(file: File) {
    // The limits come from the backend (`upload` in the response), so that no second truth
    // arises here. Without this check every file would travel across the wire at full size and
    // land in the API container's temp directory — on the disk that also holds the game worlds —
    // only to be rejected. Modpacks often have several hundred MB.
    const limit = data?.upload?.max_bytes;
    const extensions = data?.upload?.extensions ?? [];
    if (extensions.length && !extensions.some((e) => file.name.toLowerCase().endsWith(e))) {
      toast.error(t("upload.wrongType", { extensions: extensions.join(", "), name: file.name }));
      return;
    }
    if (limit && file.size > limit) {
      toast.error(
        t("upload.tooLarge", { name: file.name, size: fmtBytes(file.size), limit: fmtBytes(limit) }),
      );
      return;
    }
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const r = await api(`/servers/${key}/mods/files`, { method: "POST", body });
      if (!r.ok) throw new Error(await errText(r, t("upload.failed")));
      toast.success(t("upload.done", { name: file.name }));
      await load(true);   // the pending selection survives the upload
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function removeFile(filename: string) {
    const ok = await confirm({
      title: t("remove.confirm.title", { name: filename }),
      body: t("remove.confirm.body"),
      danger: true,
      confirmLabel: t("common:action.remove"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/files/${encodeURIComponent(filename)}`, {
        method: "DELETE",
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("remove.failed")));
      toast.success(t("remove.done", { name: filename }));
      await load();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    const ok = await confirm({
      title: t("apply.confirm.title"),
      body: t("apply.confirm.body"),
      danger: true,
      confirmLabel: t("apply.label"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/files/apply`, {
        method: "POST",
        body: JSON.stringify({ enabled: [...sel] }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("apply.failed")));
      track(d);
      toast.success(t("apply.started"));
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  function toggle(name: string) {
    const next = new Set(sel);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setSel(next);
  }

  return (
    <div className="space-y-4">
      <SectionTitle icon={Package} sub={t("files.sub", { server: server.display_name })}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}
      <OperationPanel op={op} title={t("files.operationTitle")} />

      <ModFindings data={check} />

      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("files.addFile")}
        </div>
        <input
          type="file"
          disabled={busy}
          accept={data?.upload?.extensions?.join(",")}
          onChange={(e) => {
            const f = e.target.files?.[0];
            // Reset the field, otherwise `change` does not fire when the SAME file is chosen
            // again — no request, no toast, no error, and the page looks frozen. That is the
            // first thing tried after a failure; it also covers "same name, new content".
            e.target.value = "";
            if (f) void upload(f);
          }}
          className="block w-full text-sm text-fg-muted file:mr-3 file:rounded-lg file:border file:border-line file:bg-surface-raised file:px-3 file:py-1.5 file:text-sm file:text-fg"
        />
        <div className="text-xs text-fg-dim">
          <Trans
            t={t}
            i18nKey="files.uploadNote"
            values={{ directory: data?.directory ?? "", loader: data?.loader ?? "" }}
            components={{ b: <b /> }}
          />
        </div>
      </Card>

      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("files.present", { n: data?.mods.length ?? 0 })}
        </div>
        {!data?.mods.length ? (
          <EmptyState icon={Package}>{t("files.empty")}</EmptyState>
        ) : (
          <div className="space-y-1.5">
            {data.mods.map((m) => (
              <label
                key={m.filename}
                className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-line px-3 py-2 text-sm"
              >
                <span className="flex min-w-0 items-center gap-2">
                  <input
                    type="checkbox"
                    checked={sel.has(m.filename)}
                    onChange={() => toggle(m.filename)}
                    disabled={busy}
                  />
                  <span className="min-w-0">
                    <span className="truncate font-mono text-fg">{m.filename}</span>
                    <span className="mt-0.5 flex flex-wrap items-center gap-1">
                      {m.mod_id && (
                        <Badge tone="info">
                          {m.mod_id}
                          {m.version ? ` ${m.version}` : ""}
                        </Badge>
                      )}
                      {m.loader && (
                        <Badge tone={m.loader === data.loader ? "offline" : "blood"}>{m.loader}</Badge>
                      )}
                      {m.problems?.length > 0 && <Badge tone="blood">{t("files.unreadable")}</Badge>}
                    </span>
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <Badge tone={m.enabled ? "online" : "offline"}>{t(m.enabled ? "files.active" : "files.off")}</Badge>
                  {/* The backend rejects removing an ACTIVE file with a 409: it would be gone
                      while the server still has it loaded. So the button is greyed out right
                      away instead of showing the error afterwards. */}
                  <GhostButton
                    title={t("files.removeTitle")}
                    disabled={busy}
                    disabledReason={m.enabled ? t("files.removeBlocked") : undefined}
                    onClick={(e) => {
                      e.preventDefault();
                      removeFile(m.filename);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </GhostButton>
                </span>
              </label>
            ))}
          </div>
        )}
      </Card>

      {/* Only Factorio has an open mod portal with an API — the other file-based games upload
          their files. */}
      {server?.game_type === "factorio" && <ModPortal serverKey={key!} onChange={load} />}

      <Card className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={apply}
            disabled={busy || !dirty || !!op?.running || check?.ok === false}
          >
            <Power className="h-4 w-4" /> {t("apply.button")}
          </Button>
          {!dirty && (
            <span className="text-sm text-fg-dim">
              {t("files.noChange")}
            </span>
          )}
          {check?.ok === false && (
            <span className="text-sm text-danger-text">
              {t("files.blocked")}
            </span>
          )}
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- Arma Reforger
interface RMod {
  modId: string;
  name?: string;
  version?: string;
}
interface RPreset {
  id: string;
  label: string;
  description: string;
  kind: string;
  group: string;
  mods: RMod[];
  scenarioId: string;
  sets_scenario: boolean;
}
interface RSaves {
  active_mode: string | null;
  slots: Record<string, { size?: number; modified?: number; active?: boolean }>;
}

/** Unix seconds → "… ago" (age of a save). Translated at call time. */
function fmtSaveAge(sec?: number): string {
  if (!sec) return "";
  const mins = Math.max(0, Math.floor(Date.now() / 1000 - sec) / 60);
  if (mins < 60) return i18n.t("mods:save.age.minutes", { n: Math.floor(mins) });
  const h = Math.floor(mins / 60);
  if (h < 48) return i18n.t("mods:save.age.hours", { n: h });
  return i18n.t("mods:save.age.days", { n: Math.floor(h / 24) });
}

/** Arma Reforger: game.mods (JSON) + a curated preset catalog with complete dependencies.
 * Reforger downloads mods itself at start → every change needs a restart. */
function ReforgerMods({ server }: { server: ServerInfo }) {
  const key = server.key;
  const { t } = useTranslation("mods");
  const toast = useToast();
  const confirm = useConfirm();
  const [mods, setMods] = useState<RMod[] | null>(null);
  const [scenarioId, setScenarioId] = useState("");
  const [presets, setPresets] = useState<RPreset[]>([]);
  const [err, setErr] = useState("");
  const [guid, setGuid] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saves, setSaves] = useState<RSaves | null>(null);

  async function reload() {
    try {
      const [cur, cat, sv] = await Promise.all([
        apiJson<{ mods: RMod[]; scenarioId: string }>(`/servers/${key}/mods`),
        apiJson<{ presets: RPreset[] }>(`/servers/${key}/mods/catalog`),
        apiJson<RSaves>(`/servers/${key}/saves`).catch(() => null),
      ]);
      setMods(cur.mods || []);
      setScenarioId(cur.scenarioId || "");
      setPresets(cat.presets || []);
      setSaves(sv);
      setErr("");
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  async function run(fn: () => Promise<any>, okMsg: string): Promise<boolean> {
    setBusy(true);
    try {
      await fn();
      toast.success(okMsg);
      setDirty(true);
      await reload();
      return true;
    } catch (e: any) {
      toast.error(e.message || t("failed"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const validGuid = /^[0-9A-Fa-f]{16}$/.test(guid.trim());

  async function addGuid() {
    const id = guid.trim().toUpperCase();
    if (!/^[0-9A-Fa-f]{16}$/.test(id)) return;
    // If the GUID is already there, "add" is a slip — and re-adding would do harm: the existing
    // entry would move to the END of the load order (which matters for Reforger) and lose its
    // name and version, because the new entry carries only the GUID.
    if ((mods || []).some((m) => (m.modId || "").toUpperCase() === id)) {
      toast.info(t("reforger.alreadyListed"));
      setGuid("");
      return;
    }
    const next = [...(mods || []), { modId: id }];
    const ok = await run(
      () => apiJson(`/servers/${key}/mods`, { method: "PUT", body: JSON.stringify({ mods: next }) }),
      t("reforger.added"),
    );
    if (ok) setGuid("");
  }

  function removeMod(modId: string) {
    run(() => apiJson(`/servers/${key}/mods/${modId}`, { method: "DELETE" }), t("reforger.removed"));
  }
  function move(i: number, dir: -1 | 1) {
    const list = mods || [];
    const j = i + dir;
    if (j < 0 || j >= list.length) return;
    const next = [...list];
    [next[i], next[j]] = [next[j], next[i]]; // load order: dependencies go up
    run(() => apiJson(`/servers/${key}/mods`, { method: "PUT", body: JSON.stringify({ mods: next }) }), t("reforger.orderChanged"));
  }

  async function applyPreset(p: RPreset) {
    const ok = await confirm({
      title: t("reforger.preset.confirm.title", { label: tb(p.label) }),
      body: t(p.sets_scenario ? "reforger.preset.confirm.bodyMode" : "reforger.preset.confirm.bodyBundle"),
      confirmLabel: t("apply.label"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const res = await apiJson<any>(`/servers/${key}/mods/preset`, {
        method: "POST",
        body: JSON.stringify({ preset_id: p.id }),
      });
      if (res.mode_switched) {
        toast.success(t("reforger.modeSwitched"));
      } else {
        toast.success(t("reforger.presetApplied"));
        setDirty(true);
      }
      await reload();
    } catch (e: any) {
      toast.error(e.message || t("failed"));
    } finally {
      setBusy(false);
    }
  }

  if (!mods && !err) {
    return (
      <div className="space-y-4">
        <SectionTitle icon={Package} sub={t("reforger.sub", { server: server.display_name })}>
          {t("title")}
        </SectionTitle>
        <Card>
          <Skeleton className="h-40 w-full" />
        </Card>
      </div>
    );
  }

  const groups = Array.from(new Set(presets.map((p) => p.group)));

  return (
    <div className="space-y-4">
      <SectionTitle icon={Package} sub={t("reforger.sub", { server: server.display_name })}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}
      {dirty && (
        <Alert kind="warn">
          <Trans t={t} i18nKey="reforger.restartNeeded" components={{ strong: <strong /> }} />
        </Alert>
      )}

      {/* Current mode + installed mods */}
      <Card className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{t("reforger.activeMode")}</div>
          <GhostButton onClick={reload} disabled={busy}>
            <RefreshCw className="h-3.5 w-3.5" /> {t("common:action.refresh")}
          </GhostButton>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <MapPin className="h-4 w-4 shrink-0 text-primary" />
          <span className="min-w-0 break-all font-mono text-xs text-fg">
            {scenarioId || t("reforger.noScenario")}
          </span>
        </div>

        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("reforger.installed", { n: mods?.length || 0 })}
        </div>
        {mods && mods.length === 0 && <EmptyState icon={Package}>{t("reforger.empty")}</EmptyState>}
        <div className="space-y-1.5">
          {(mods || []).map((m, i) => (
            <div
              key={m.modId}
              className="flex items-center gap-2 rounded-lg border border-line bg-surface-sunken/40 px-3 py-1.5"
            >
              <span className="w-5 shrink-0 text-center font-mono text-xs text-fg-dim">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-fg">{m.name || t("reforger.unnamed")}</div>
                <div className="truncate font-mono text-[11px] text-fg-dim">
                  {m.modId}
                  {m.version ? ` · v${m.version}` : ""}
                </div>
              </div>
              <button
                onClick={() => move(i, -1)}
                disabled={busy || i === 0}
                className="text-fg-dim hover:text-fg disabled:opacity-30"
                title={t("reforger.moveUpTitle")}
                aria-label={t("reforger.moveUp")}
              >
                <ChevronUp className="h-4 w-4" />
              </button>
              <button
                onClick={() => move(i, 1)}
                disabled={busy || i === (mods?.length || 0) - 1}
                className="text-fg-dim hover:text-fg disabled:opacity-30"
                title={t("reforger.moveDownTitle")}
                aria-label={t("reforger.moveDown")}
              >
                <ChevronDown className="h-4 w-4" />
              </button>
              <a
                href={`https://reforger.armaplatform.com/workshop/${m.modId}`}
                target="_blank"
                rel="noreferrer"
                className="text-fg-dim hover:text-fg"
                title={t("reforger.openWorkshop")}
              >
                <ExternalLink className="h-4 w-4" />
              </a>
              <button
                onClick={() => removeMod(m.modId)}
                disabled={busy}
                className="text-fg-dim hover:text-danger-text disabled:opacity-50"
                title={t("common:action.remove")}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>

        {/* Add a mod by GUID */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Input
            value={guid}
            onChange={(e) => setGuid(e.target.value)}
            placeholder={t("reforger.guidPlaceholder")}
            className="flex-1 font-mono"
            spellCheck={false}
          />
          <Button onClick={addGuid} disabled={busy || !validGuid}>
            {t("common:action.add")}
          </Button>
        </div>
        <div className="text-xs text-fg-dim">
          {t("reforger.guidHint")}
        </div>
      </Card>

      {/* Curated presets */}
      {groups.map((g) => (
        <Card key={g} className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tb(g)}</div>
          <div className="grid gap-2 lg:grid-cols-2">
            {presets
              .filter((p) => p.group === g)
              .map((p) => (
                <div key={p.id} className="flex flex-col gap-2 rounded-xl border border-line bg-surface-sunken/40 p-3">
                  <div className="flex items-center gap-2">
                    {p.kind === "mode" ? (
                      <ShieldCheck className="h-4 w-4 shrink-0 text-primary" />
                    ) : (
                      <Wand2 className="h-4 w-4 shrink-0 text-primary" />
                    )}
                    <span className="text-sm font-semibold text-fg">{tb(p.label)}</span>
                    {p.sets_scenario && <Badge tone="info">{t("reforger.badgeMode")}</Badge>}
                  </div>
                  <p className="text-xs text-fg-muted">{tb(p.description)}</p>
                  <div className="text-[11px] text-fg-dim">
                    {p.mods.length ? p.mods.map((m) => m.name || m.modId).join(" · ") : t("reforger.vanilla")}
                  </div>
                  {p.sets_scenario && (
                    <div className="text-[11px]">
                      {saves?.active_mode === p.id ? (
                        <span className="text-success-text">{t("reforger.save.active")}</span>
                      ) : saves?.slots?.[p.id] ? (
                        <span className="text-fg-muted">{t("reforger.save.present", { age: fmtSaveAge(saves.slots[p.id].modified) })}</span>
                      ) : (
                        <span className="text-fg-dim">{t("reforger.save.none")}</span>
                      )}
                    </div>
                  )}
                  <div className="mt-auto flex justify-end">
                    <Button onClick={() => applyPreset(p)} disabled={busy || saves?.active_mode === p.id}>
                      <CheckCircle2 className="h-4 w-4" /> {saves?.active_mode === p.id ? t("reforger.active") : t("apply.label")}
                    </Button>
                  </div>
                </div>
              ))}
          </div>
        </Card>
      ))}
    </div>
  );
}


// --- Avorion: Workshop IDs instead of files ----------------------------------------------------

interface AvorionData {
  workshop_ids: string[];
  /** The access list — a list of its OWN, not part of the mods.
   *
   * It is not edited here but sent back unchanged: sending `allowed: []` on apply would wipe
   * the access list, and merging it into `workshop_ids` would make the server load mods that
   * were only allowed before.
   */
  allowed: string[];
  present: boolean;
  preflight: PreflightData;
}

/** Mod management for Avorion.
 *
 * Unlike every other game, nothing is uploaded here. Avorion downloads its mods from the Steam
 * Workshop itself at start; the galaxy folder only holds a list of IDs. So this page edits a
 * list — and the promise stays the same as next door: typing changes nothing on the running
 * server, only "Apply" makes it live (with mandatory backup, boot proof and fallback).
 */
function AvorionMods({ server }: { server: ServerInfo }) {
  const key = server.key;
  const { t } = useTranslation("mods");
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<AvorionData | null>(null);
  const [ids, setIds] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const { op, track } = useOperation(key, "mods.files.apply");

  async function load(keepDraft = false) {
    try {
      const d = await apiJson<AvorionData>(`/servers/${key}/mods/workshop`);
      setData(d);
      if (!keepDraft) setIds(d.workshop_ids);
      setErr("");
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => {
    if (op && !op.running) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [op?.id, op?.running]);

  const live = (data?.workshop_ids || []).join("|");
  const dirty = ids.join("|") !== live;

  // The check of the DRAFT — the same separation as for the file-based games. The live check
  // from `load()` does not know the planned list; hanging the apply button on it would kill
  // exactly the button that repairs a broken state.
  const [draftCheck, setDraftCheck] = useState<PreflightData | null>(null);
  const draftKey = ids.join("|");
  useEffect(() => {
    if (!key || !data) return;
    let current = true;
    apiJson<PreflightData>(`/servers/${key}/mods/workshop/preflight`, {
      method: "POST",
      // **The same `allowed` list as on apply** — otherwise the preview checks a different
      // draft than the one the button writes afterwards.
      body: JSON.stringify({ workshop_ids: ids, allowed: data.allowed ?? [] }),
    })
      .then((p) => current && setDraftCheck(p))
      .catch(() => current && setDraftCheck(null)); // when in doubt, do not lock
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, draftKey, data]);

  const check = draftCheck ?? data?.preflight ?? null;
  const blocked = !!check?.blockers?.length;

  function add() {
    // Pull the ID out of a pasted Workshop URL: that is what is in the clipboard when coming
    // from the Workshop page.
    const raw = input.trim();
    const match = raw.match(/(\d{6,})/);
    if (!match) {
      toast.error(t("avorion.notAnId"));
      return;
    }
    const id = match[1];
    if (ids.includes(id)) {
      toast.error(t("avorion.alreadyListed", { id }));
      return;
    }
    setIds([...ids, id]);
    setInput("");
  }

  async function apply() {
    if (!(await confirm({
      title: t("avorion.confirm.title"),
      body: t("avorion.confirm.body"),
      confirmLabel: t("apply.label"),
    }))) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/workshop`, {
        method: "PUT",
        body: JSON.stringify({ workshop_ids: ids, allowed: data?.allowed ?? [] }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(detailText(d.detail, t("apply.failed")));
      track(d);
      toast.success(t("apply.started"));
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <SectionTitle icon={Package} sub={t("avorion.sub", { server: server.display_name })}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}
      <OperationPanel op={op} title={t("avorion.operationTitle")} />

      <ModFindings data={check} />

      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("avorion.addId")}
        </div>
        <div className="flex gap-2">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
            placeholder={t("avorion.idPlaceholder")}
            disabled={busy}
          />
          <Button onClick={add} disabled={busy}>
            {t("common:action.add")}
          </Button>
        </div>
        <p className="text-xs text-fg-dim">
          {t("avorion.hint")}
        </p>
      </Card>

      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("avorion.list", { n: ids.length })}
        </div>
        {ids.length === 0 && (
          <p className="text-sm text-fg-dim">
            {t("avorion.empty")}
          </p>
        )}
        {ids.map((id) => (
          <div key={id} className="flex items-center justify-between gap-2 text-sm">
            <a
              href={safeUrl(`https://steamcommunity.com/sharedfiles/filedetails/?id=${id}`)}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-primary-text hover:underline"
            >
              {id}
            </a>
            <GhostButton onClick={() => setIds(ids.filter((x) => x !== id))} disabled={busy}>
              {t("common:action.remove")}
            </GhostButton>
          </div>
        ))}
      </Card>

      <div className="flex items-center gap-3">
        <Button onClick={apply} disabled={!dirty || busy || blocked || !!op?.running}>
          {t("apply.label")}
        </Button>
        {!dirty && <span className="text-sm text-fg-dim">{t("avorion.inSync")}</span>}
        {blocked && (
          <span className="text-sm text-fg-dim">
            {t("avorion.blocked")}
          </span>
        )}
      </div>
    </div>
  );
}
