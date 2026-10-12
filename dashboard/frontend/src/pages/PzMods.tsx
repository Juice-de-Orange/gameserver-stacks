// Mod management for Project Zomboid — edit the draft, check it, apply it.
//
// Guiding idea: **editing changes nothing on the running server.** Everything here writes a
// draft in the database. Only "Apply" transfers it — as a transaction with mandatory backup,
// boot proof via RCON and automatic fallback. Writing every click straight into the server's
// `.ini` would make a half-finished configuration go live at the next restart, whatever its
// cause.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ArrowDown, ArrowUp, Check, Download, History, Info, Map as MapIcon,
  Package, Power, RotateCcw, Undo2, X,
} from "lucide-react";
import { api, apiJson, detailText, errText } from "../api";
import { ModFindings, PreflightData } from "../components/ModFindings";
import { ModHealthPanel } from "../components/ModHealth";
import { WorkshopHealth } from "../components/WorkshopHealth";
import { OperationPanel, useOperation } from "../components/Operation";
import { useActiveServerKey } from "../components/ServerContext";
import { useConfirm, useToast } from "../components/feedback";
import {
  Alert, Badge, Button, Card, EmptyState, GhostButton, Input, SectionTitle, Spinner,
} from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { tb } from "../i18n";

interface Plan {
  workshop_items: string[];
  mods: string[];
  maps: string[];
}

interface DraftData {
  live: Plan;
  draft: Plan;
  diff: Record<string, string[] | boolean>;
  has_changes: boolean;
  summary: string;
}

interface AvailableItem {
  workshop_id: string;
  mod_ids: string[];
  requires: string[];
  tile_maps: string[];
  spawn_regions: string[];
  builds: string[];
  active: boolean;
  cells: Record<string, number>;
}

interface ModChangeRow {
  id: number;
  at: string;
  status: string;
  summary: string;
  backup_name: string;
  quarantine_name: string;
  detail: string;
}

export default function PzMods() {
  // Rendered only below `FeatureGate`, which requires an active server, so the key is there.
  const key = useActiveServerKey() ?? "";
  const { t } = useTranslation("pzMods");
  const confirm = useConfirm();
  const toast = useToast();

  const [draft, setDraft] = useState<DraftData | null>(null);
  const [items, setItems] = useState<AvailableItem[]>([]);
  const [pre, setPre] = useState<PreflightData | null>(null);
  const [history, setHistory] = useState<ModChangeRow[]>([]);
  const [wid, setWid] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Applying runs as a background operation — the display re-attaches after a page reload.
  const { op, track } = useOperation(key, "mods.apply");

  /**
   * `draftOnly` skips the expensive requests.
   *
   * `/mods/available` walks recursively over the Workshop content directory, on the same disk
   * the running server writes its world to — and moving a mod from place 20 to place 1 is 19
   * single steps (`moveMod` only knows single steps). What a reorder CAN change is the check;
   * the inventory on disk and the history do not change, so they stay as they are until
   * something is installed, deleted or applied.
   */
  const load = useCallback(async (draftOnly = false) => {
    if (!key) return;
    try {
      const [d, p] = await Promise.all([
        apiJson<DraftData>(`/servers/${key}/mods/draft`),
        apiJson<PreflightData>(`/servers/${key}/mods/preflight`),
      ]);
      setDraft(d);
      setPre(p);
      setErr("");
      if (!draftOnly) {
        const a = await apiJson<{ items: AvailableItem[]; readable: boolean }>(
          `/servers/${key}/mods/available`,
        );
        setItems(a.items);
      }
    } catch (e) {
      setErr((e as Error).message);
    }
    if (draftOnly) return;
    try {
      setHistory(await apiJson<ModChangeRow[]>(`/servers/${key}/mods/history`));
    } catch {
      setHistory([]);
    }
  }, [key]);

  useEffect(() => {
    void load();
  }, [load]);

  // `active` also comes from `/mods/available`, but is derived from the DRAFT here. Otherwise
  // every "Add to draft"/"Remove from draft" click would need the expensive disk walk just to
  // flip a badge — the answer is already in the draft that the same click returns.
  const inDraft = new Set(draft?.draft.workshop_items ?? []);

  // Once an operation has finished, draft/check/history reflect the new state.
  useEffect(() => {
    if (op && !op.running) void load();
  }, [op?.id, op?.running]);   // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * Every edit goes through here — it touches the draft only.
   *
   * `draftOnly`: whatever does NOT touch the inventory on disk (reordering, adding to the
   * draft, removing from it) needs no new disk walk either. `label` is the error text shown
   * when the response carries none.
   */
  async function edit(path: string, init: RequestInit, label: string, draftOnly = true) {
    if (!key) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/${path}`, init);
      if (!r.ok) throw new Error(await errText(r, label));
      await load(draftOnly);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function install() {
    const id = wid.trim();
    if (!/^\d+$/.test(id)) {
      toast.error(t("install.notNumeric"));
      return;
    }
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/items`, {
        method: "POST",
        body: JSON.stringify({ workshop_id: id }),
      });
      if (!r.ok) throw new Error(await errText(r, t("install.failed")));
      setWid("");
      toast.success(t("install.done", { id }));
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!draft?.has_changes || !pre) return;
    const worldFiles = pre.world_reset_files ?? 0;
    const ok = await confirm({
      title: t("apply.confirm.title"),
      body:
        worldFiles > 0
          ? t("apply.confirm.bodyWorld", { summary: tb(draft.summary), count: worldFiles })
          : t("apply.confirm.body", { summary: tb(draft.summary) }),
      danger: true,
      confirmLabel: t("apply.label"),
    });
    if (!ok) return;

    setBusy(true);
    try {
      const r = await api(`/servers/${key}/mods/apply`, {
        method: "POST",
        body: JSON.stringify({ confirm_world_reset: worldFiles > 0 }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("apply.failed")));
      track(d);   // from here on the operation panel shows the progress
      toast.success(t("apply.started"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function restoreLastGood() {
    const ok = await confirm({
      title: t("restore.confirm.title"),
      body: t("restore.confirm.body"),
      confirmLabel: t("restore.confirm.label"),
    });
    if (ok) await edit("restore-last-good", { method: "POST" }, t("edit.failed.restore"));
  }

  function moveMod(i: number, dir: -1 | 1) {
    if (!draft) return;
    const next = [...draft.draft.mods];
    const j = i + dir;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    void edit("order", { method: "PUT", body: JSON.stringify({ mods: next }) }, t("edit.failed.order"));
  }

  function moveMap(i: number, dir: -1 | 1) {
    if (!draft) return;
    const next = [...draft.draft.maps];
    const j = i + dir;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    void edit("maps", { method: "PUT", body: JSON.stringify({ maps: next }) }, t("edit.failed.maps"));
  }

  if (!key) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;

  const d = draft?.draft;
  const blocked = !!pre && !pre.ok;
  const pending = !!draft?.has_changes;

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={Package}
        sub={t("sub")}
        right={
          <div className="flex items-center gap-2">
            <GhostButton onClick={restoreLastGood} disabled={busy} title={t("restore.title")}>
              <Undo2 className="h-4 w-4" /> {t("restore.button")}
            </GhostButton>
            {pending && (
              <GhostButton
                onClick={() => edit("draft/reset", { method: "POST" }, t("edit.failed.discard"))}
                disabled={busy}
              >
                <RotateCcw className="h-4 w-4" /> {t("draft.discard")}
              </GhostButton>
            )}
          </div>
        }
      >
        {t("title")}
      </SectionTitle>

      {err && <Alert kind="error">{err}</Alert>}

      <ModHealthPanel />

      {/* The network part of the check — deliberately separate from the offline preflight. */}
      {key && <WorkshopHealth serverKey={key} onChanged={() => load(true)} />}

      <OperationPanel op={op} title={t("operationTitle")} />

      {/* What would change? */}
      {pending && (
        <Card className="space-y-2 border-primary/40">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <Info className="h-4 w-4" /> {t("draft.pending", { summary: tb(draft?.summary) ?? "" })}
          </div>
          <DiffList diff={draft!.diff} />
          <div className="text-xs text-fg-dim">
            {t("draft.notSeen")}
          </div>
        </Card>
      )}

      <ModFindings data={pre} />

      {/* Install */}
      <Card className="space-y-3">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("install.heading")}
        </div>
        <div className="flex flex-wrap gap-2">
          <Input
            aria-label={t("install.ariaLabel")}
            placeholder={t("install.placeholder")}
            value={wid}
            onChange={(e) => setWid(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && install()}
            className="w-64"
          />
          <Button onClick={install} disabled={busy || !wid.trim()}>
            {busy ? <Spinner /> : <Download className="h-4 w-4" />} {t("install.button")}
          </Button>
        </div>
        <div className="text-xs text-fg-dim">
          {t("install.hint")}
        </div>
      </Card>

      {/* Installed items */}
      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("items.heading", { n: items.length })}
        </div>
        {items.length === 0 ? (
          <EmptyState icon={Package}>{t("items.empty")}</EmptyState>
        ) : (
          <div className="space-y-1.5">
            {items.map((it) => (
              <div
                key={it.workshop_id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line px-3 py-2 text-sm"
              >
                <div className="min-w-0">
                  <a
                    className="font-mono text-fg underline-offset-2 hover:underline"
                    href={`https://steamcommunity.com/sharedfiles/filedetails/?id=${it.workshop_id}`}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {it.workshop_id}
                  </a>
                  <div className="mt-0.5 flex flex-wrap items-center gap-1">
                    {inDraft.has(it.workshop_id) ? (
                      <Badge tone="online">{t("items.inDraft")}</Badge>
                    ) : (
                      <Badge tone="offline">{t("items.inactive")}</Badge>
                    )}
                    {it.tile_maps.map((m) => (
                      <Badge key={m} tone="info">
                        {t("items.map", { name: m, count: it.cells[m] ?? 0 })}
                      </Badge>
                    ))}
                    {it.spawn_regions.length > 0 && <Badge tone="info">{t("items.spawn")}</Badge>}
                    {it.builds.includes("41") && !it.builds.includes("42") && (
                      <Badge tone="blood">{t("items.build41Only")}</Badge>
                    )}
                  </div>
                  {it.mod_ids.length > 0 && (
                    <div className="mt-0.5 font-mono text-[11px] text-fg-dim">
                      {it.mod_ids.join(", ")}
                    </div>
                  )}
                </div>
                <div className="flex gap-2">
                  {inDraft.has(it.workshop_id) ? (
                    <GhostButton
                      disabled={busy}
                      onClick={() =>
                        edit(`items/${it.workshop_id}`, { method: "DELETE" }, t("edit.failed.remove"))
                      }
                    >
                      <X className="h-4 w-4" /> {t("items.removeFromDraft")}
                    </GhostButton>
                  ) : (
                    <GhostButton
                      disabled={busy}
                      onClick={() =>
                        edit("items", {
                          method: "POST",
                          body: JSON.stringify({ workshop_id: it.workshop_id }),
                        }, t("edit.failed.add"))
                      }
                    >
                      <Check className="h-4 w-4" /> {t("items.addToDraft")}
                    </GhostButton>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Load order */}
      {d && d.mods.length > 0 && (
        <Card className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
              {t("order.heading")}
            </div>
            <GhostButton
              disabled={busy}
              onClick={() => edit("order/sort", { method: "POST" }, t("edit.failed.sort"))}
              title={t("order.sortTitle")}
            >
              {t("order.sort")}
            </GhostButton>
          </div>
          <OrderedList items={d.mods} onMove={moveMod} busy={busy} />
          <div className="text-xs text-fg-dim">
            {t("order.hint")}
          </div>
        </Card>
      )}

      {/* Maps */}
      {d && (
        <Card className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            <MapIcon className="h-4 w-4" /> {t("maps.heading")}
          </div>
          <OrderedList items={d.maps} onMove={moveMap} busy={busy} />
          <div className="text-xs text-fg-dim">
            {t("maps.hint")}
          </div>
        </Card>
      )}

      {/* Apply */}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={apply} disabled={busy || !pending || blocked || !!op?.running}>
            <Power className="h-4 w-4" /> {t("apply.button")}
          </Button>
          {!pending && <span className="text-sm text-fg-dim">{t("apply.noChanges")}</span>}
          {blocked && (
            <span className="text-sm text-danger-text">
              {t("apply.blocked")}
            </span>
          )}
        </div>
        <Alert kind="warn">
          {t("apply.warning")}
        </Alert>
      </Card>

      {/* History */}
      <Card>
        <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
          <History className="h-4 w-4" /> {t("history.heading")}
        </div>
        {history.length === 0 ? (
          <div className="text-sm text-fg-dim">{t("history.empty")}</div>
        ) : (
          <div className="space-y-1.5">
            {history.map((h) => (
              <div
                key={h.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded border border-line px-3 py-2 text-sm"
              >
                <span className="text-fg-muted">
                  {fmtDateTime(h.at, true)} — {tb(h.summary) || t("history.fallbackSummary")}
                  {h.quarantine_name && (
                    <span className="ml-2 font-mono text-xs text-fg-dim">
                      {t("history.quarantine", { name: h.quarantine_name })}
                    </span>
                  )}
                </span>
                <Badge
                  tone={h.status === "ok" ? "online" : h.status === "failed" ? "blood" : "warn"}
                >
                  {h.status}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

/** Compact before/after list of the draft. */
function DiffList({ diff }: { diff: Record<string, string[] | boolean> }) {
  const { t } = useTranslation("pzMods");
  // Pin it down as a tuple list first, then filter: `.filter()` directly on the literal widens
  // the type to `(string | string[])[][]` and the tuple promise is lost. The labels are keys.
  const all: Array<[string, string[]]> = [
    ["diff.modsAdded", (diff.mods_added as string[]) ?? []],
    ["diff.modsRemoved", (diff.mods_removed as string[]) ?? []],
    ["diff.mapsAdded", (diff.maps_added as string[]) ?? []],
    ["diff.mapsRemoved", (diff.maps_removed as string[]) ?? []],
    ["diff.itemsAdded", (diff.items_added as string[]) ?? []],
    ["diff.itemsRemoved", (diff.items_removed as string[]) ?? []],
  ];
  const rows = all.filter(([, v]) => v.length > 0);

  return (
    <div className="space-y-1 text-sm">
      {rows.map(([label, values]) => (
        <div key={label} className="flex flex-wrap gap-1">
          <span className="text-fg-dim">{t(label)}</span>
          <span className="font-mono text-fg">{values.join(", ")}</span>
        </div>
      ))}
      {(diff.order_changed || diff.map_order_changed) && (
        <div className="text-fg-muted">{t("diff.orderChanged")}</div>
      )}
    </div>
  );
}

/** Sortable list with ↑/↓ — deliberately without drag & drop, so that it works on a phone too. */
function OrderedList({
  items,
  onMove,
  busy,
}: {
  items: string[];
  onMove: (i: number, dir: -1 | 1) => void;
  busy: boolean;
}) {
  const { t } = useTranslation("pzMods");
  return (
    <div className="space-y-1">
      {items.map((name, i) => (
        <div
          key={`${name}-${i}`}
          className="flex items-center justify-between gap-2 rounded border border-line px-3 py-1.5 text-sm"
        >
          <span className="font-mono text-fg-muted">
            <span className="mr-2 text-fg-dim">{i + 1}.</span>
            {name}
          </span>
          <span className="flex gap-1">
            <GhostButton disabled={busy || i === 0} onClick={() => onMove(i, -1)} aria-label={t("order.moveUp")}>
              <ArrowUp className="h-3.5 w-3.5" />
            </GhostButton>
            <GhostButton
              disabled={busy || i === items.length - 1}
              onClick={() => onMove(i, 1)}
              aria-label={t("order.moveDown")}
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </GhostButton>
          </span>
        </div>
      ))}
    </div>
  );
}
