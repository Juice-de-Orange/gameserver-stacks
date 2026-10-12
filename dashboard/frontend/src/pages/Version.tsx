// Version page: pick a version from the catalog that is refreshed daily (dropdown + changes),
// switch with mandatory backup + create-then-swap + rollback.
// Games whose version is the image tag may need a multi-GB image download first; the others
// switch by game version.
// Uses /api/servers/:key/version/{available,refresh,download,change,rollback,history}
// and /api/servers/:key/recreate (recreate without a version change — for env-managed values).
import { useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Boxes, Download, RefreshCw, RotateCcw, Tag } from "lucide-react";
import { api, apiJson, detailText, errText } from "../api";
import { useActiveServer, useServers } from "../components/ServerContext";
import { useConfirm, useToast } from "../components/feedback";
import { OperationPanel, useOperation } from "../components/Operation";
import { Worlds } from "../components/Worlds";
import { Alert, Badge, Button, Card, Field, GhostButton, Input, Select, SectionTitle } from "../components/ui";
import { fmtBytes, fmtDate, fmtDateTime } from "../lib/format";
import { versionChangePullsImage } from "../lib/game";
import { safeUrl } from "../lib/url";
import { tb } from "../i18n";

interface VersionChange {
  id: number;
  at: string;
  direction: string;
  status: string;
  backup_name: string;
  old: string | null;
  new: string | null;
}

interface CatalogEntry {
  version: string;
  ref: string;
  channel: string;
  channel_label: string;
  released_at: string | null;
  changelog_title: string;
  changelog_url: string;
  changelog_summary: string;
  current: boolean;
  newer: boolean;
  is_new: boolean;
  installed?: boolean;
}

interface Catalog {
  supported: boolean;
  entries: CatalogEntry[];
  current: string;
  update_available: CatalogEntry | null;
  // `false`: the running version could not be determined, so "no update" means "unknown".
  running_version_known?: boolean;
  source_ok?: boolean;
  checked_at: string | null;
  note: string;
  // Only set in the `supported: false` branch: the image the container runs with, whether it
  // is pinned by digest, and whether its tag names a channel that moves on (`latest`). Any
  // other tag is fixed unless its publisher re-pushes it. For games without a version list
  // this is the only thing that can be checked at all.
  image_ref?: string;
  image_pinned?: boolean;
  image_floating?: boolean;
}

interface PullState {
  tag: string;
  status: string;
  current: number;
  total: number;
  percent: number | null;
  done: boolean;
  ok: boolean;
  error: string;
  running: boolean;
}

// Order of the channel groups in the dropdown (stable first — that is what is wanted most).
const CHANNEL_ORDER = ["stable", "unstable", "snapshot", "legacy"];

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default function Version() {
  const { t } = useTranslation("version");
  const server = useActiveServer();
  const { reload: reloadServers } = useServers();
  const confirm = useConfirm();
  const toast = useToast();
  const key = server?.key;
  const [target, setTarget] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loaded, setLoaded] = useState(false);   // catalog request finished (successfully or not)
  const [history, setHistory] = useState<VersionChange[]>([]);
  const [pull, setPull] = useState<PullState | null>(null);
  const [manual, setManual] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const alive = useRef(true);
  // The switch runs as a background operation — up to 15 minutes of boot verification do not
  // survive a single HTTP request behind a proxy with a request timeout. A recreate is an
  // operation of its own kind (`version.recreate`); the panel shows the newer of the two.
  const { op: changeOp, track } = useOperation(key || "", "version.change");
  const { op: recreateOp, track: trackRecreate } = useOperation(key || "", "version.recreate");
  const op = !recreateOp || (changeOp && changeOp.id > recreateOp.id) ? changeOp : recreateOp;
  const anyRunning = !!changeOp?.running || !!recreateOp?.running;
  // Image tags already ensured in this session — the catalog only knows `installed` after the
  // next load, otherwise the downgrade confirmation path would start a second pull.
  const ensured = useRef<Set<string>>(new Set());

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function load() {
    if (!key) return;
    try {
      setHistory(await apiJson<VersionChange[]>(`/servers/${key}/version/history`));
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
    try {
      setCatalog(await apiJson<Catalog>(`/servers/${key}/version/available`));
    } catch {
      setCatalog(null); // The catalog is a convenience — without it the manual field stays usable.
    } finally {
      // Only from here on may the page decide WHAT it shows. Before, `catalog === null` could
      // not be told from "catalog not available" — and for a game without a version list the
      // page would have offered a switch form that does not exist there.
      setLoaded(true);
    }
  }

  useEffect(() => {
    if (server) setTarget(server.game_version || "");
    setPull(null);
    setManual(false);
    // The old catalog no longer applies after a server switch: otherwise the page would show
    // the PREVIOUS server's version list for a moment — possibly a switch form for a game that
    // has no selectable version.
    setLoaded(false);
    setCatalog(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Once the operation has finished, history and registry reflect the new state.
  useEffect(() => {
    if (op && !op.running) {
      void load();
      void reloadServers();
    }
  }, [op?.id, op?.running]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!server) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;
  if (!loaded) {
    return (
      <div className="space-y-5">
        <SectionTitle icon={Tag} sub={server.display_name}>
          {t("title")}
        </SectionTitle>
        <Card>
          <div className="py-6 text-center text-sm text-fg-dim">{t("loading")}</div>
        </Card>
      </div>
    );
  }
  // "Does a switch download an image?" — not "is this PZ". The answer comes from the driver
  // (`version_source`), not from a list here.
  const pullsImage = versionChangePullsImage(server);
  const current = catalog?.current || server.game_version || "?";
  const entries = catalog?.supported ? catalog.entries : [];
  const useDropdown = entries.length > 0 && !manual;
  const selected = entries.find((e) => e.ref === target) || null;
  const upgrade = catalog?.update_available || null;

  const groups = CHANNEL_ORDER.map((channel) => ({
    channel,
    label: tb(entries.find((e) => e.channel === channel)?.channel_label) || channel,
    items: entries.filter((e) => e.channel === channel),
  })).filter((g) => g.items.length > 0);

  function optionLabel(e: CatalogEntry): string {
    const parts = [e.version];
    if (e.released_at) parts.push(fmtDate(e.released_at, true));
    if (e.current) parts.push(t("option.running"));
    else if (e.installed) parts.push(t("option.downloaded"));
    if (e.is_new && e.newer) parts.push(t("option.new"));
    return parts.join(" · ");
  }

  async function checkNow() {
    if (!key) return;
    setChecking(true);
    try {
      const r = await apiJson<{ ok: boolean; total: number; new: number; detail?: string }>(
        `/servers/${key}/version/refresh`,
        { method: "POST" },
      );
      if (!r.ok) toast.error(tb(r.detail) || t("check.unreachable"));
      else if (r.new > 0) toast.success(t("check.found", { count: r.new }));
      else toast.success(t("check.none"));
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  /** Pull the target image if needed (several GB) and show the progress. */
  async function ensureDownloaded(entry: CatalogEntry | null): Promise<boolean> {
    if (!key || !pullsImage) return true;
    const tag = entry?.ref || target;
    if (entry?.installed || ensured.current.has(tag)) return true;
    const r = await api(`/servers/${key}/version/download`, {
      method: "POST",
      body: JSON.stringify({ image_tag: tag }),
    });
    if (!r.ok) throw new Error(await errText(r, t("download.startFailed")));
    // Evaluate the POST response BEFORE polling. If the image is already local, the backend
    // answers `{done: true, ok: true}` right away and deliberately keeps no pull entry. A GET
    // afterwards would find nothing and return the idle state `{done: true, ok: false}` — the
    // loop would then report a failed download and the switch would never be sent.
    const start = (await r.json()) as PullState;
    setPull(start);
    if (start.done) {
      if (!start.ok) throw new Error(tb(start.error) || t("download.failed"));
      ensured.current.add(tag);
      return true;
    }
    for (;;) {
      await sleep(2000);
      if (!alive.current) return false;
      const st = await apiJson<PullState>(
        `/servers/${key}/version/download?image_tag=${encodeURIComponent(tag)}`,
      );
      setPull(st);
      if (st.done) {
        if (!st.ok) throw new Error(tb(st.error) || t("download.failed"));
        ensured.current.add(tag);
        return true;
      }
    }
  }

  async function change(confirmDowngrade: boolean) {
    if (!key || !target || target === current) return;
    if (!confirmDowngrade) {
      const needsPull = pullsImage && !!selected && !selected.installed;
      const ok = await confirm({
        title: t("change.confirm.title", { version: selected?.version || target }),
        body: t(needsPull ? "change.confirm.bodyPull" : "change.confirm.body"),
        danger: true,
        confirmLabel: t("change.confirm.label"),
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      // Also on the downgrade confirmation path: without a local image the multi-GB pull would
      // otherwise run synchronously inside the switch request and hit the proxy timeout.
      if (!(await ensureDownloaded(selected))) return;
      const body = pullsImage
        ? { image_tag: target, confirm_downgrade: confirmDowngrade }
        : { game_version: target, confirm_downgrade: confirmDowngrade };
      const r = await api(`/servers/${key}/version/change`, { method: "POST", body: JSON.stringify(body) });
      if (r.status === 400) {
        const d = await r.json().catch(() => ({}));
        // The downgrade gate is a 400 with its own code; the detail carries `old`/`new`.
        if (d.detail?.code === "version.downgrade_needs_confirmation") {
          setBusy(false);
          const downgradeOk = await confirm({
            title: t("downgrade.confirm.title"),
            body: t("downgrade.confirm.body", { old: d.detail.old, new: d.detail.new }),
            danger: true,
            confirmLabel: t("downgrade.confirm.label"),
          });
          if (downgradeOk) return change(true);
          return;
        }
        throw new Error(detailText(d.detail, t("change.invalid")));
      }
      if (!r.ok) throw new Error(await errText(r, t("change.startFailed")));
      track(await r.json());   // from here on the operation panel shows the progress
      toast.success(t("change.started"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function recreate() {
    if (!key) return;
    const ok = await confirm({
      title: t("recreate.confirm.title"),
      body: t("recreate.confirm.body"),
      danger: true,
      confirmLabel: t("recreate.label"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/recreate`, { method: "POST" });
      if (!r.ok) throw new Error(await errText(r, t("recreate.failed")));
      trackRecreate(await r.json());
      toast.success(t("recreate.started"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function rollback() {
    if (!key) return;
    const ok = await confirm({
      title: t("rollback.confirm.title"),
      body: t("rollback.confirm.body"),
      danger: true,
      confirmLabel: t("rollback.label"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/version/rollback`, { method: "POST" });
      if (!r.ok) throw new Error(await errText(r, t("rollback.failed")));
      // The endpoint starts a background operation — it answers IMMEDIATELY with the operation
      // id; the actual work (create-then-swap + boot proof, up to 900 s) runs afterwards. So
      // this reports a start, not a completion, and the operation panel carries the outcome —
      // including a failed boot proof.
      track(await r.json());   // from here on the operation panel shows the progress
      toast.success(t("rollback.started"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Games without a version list (today: Arma Reforger) get information instead of a dropdown.
  // An empty select with a switch button would be a lie here — there is nothing to choose. What
  // there is: which game version is running and which image the server is pinned to.
  if (catalog && !catalog.supported) {
    return (
      <div className="space-y-5">
        {/* Recreate stays: it is the only way to apply container settings (for Valheim every
            setting and the world choice), with or without a version list. */}
        <SectionTitle
          icon={Tag}
          sub={server.display_name}
          right={
            <GhostButton disabled={busy} onClick={recreate} title={t("recreate.title")}>
              <Boxes className="h-4 w-4" /> {t("recreate.label")}
            </GhostButton>
          }
        >
          {t("title")}
        </SectionTitle>
        {err && <Alert kind="error">{err}</Alert>}

        <OperationPanel op={op} title={t("operationTitle")} />

        <Card className="space-y-3">
          <div className="text-sm text-fg-muted">
            <Trans
              t={t}
              i18nKey="unsupported.running"
              values={{ version: catalog.current || t("unsupported.notDetected") }}
              components={{ v: <span className="font-mono text-fg" /> }}
            />
          </div>
          {catalog.image_ref && (
            <div className="text-sm text-fg-muted">
              <Trans
                t={t}
                i18nKey="unsupported.image"
                values={{ image: catalog.image_ref }}
                components={{ v: <span className="break-all font-mono text-fg" /> }}
              />
              <span className="ml-2 text-xs text-fg-dim">
                {t(catalog.image_pinned ? "unsupported.pinned"
                  : catalog.image_floating ? "unsupported.floating" : "unsupported.tag")}
              </span>
            </div>
          )}
          {catalog.note && <div className="text-xs leading-relaxed text-fg-dim">{tb(catalog.note)}</div>}
          <Alert kind="info">{t("unsupported.updateInfo")}</Alert>
        </Card>

        {/* Valheim has no version list but several worlds; without this its worlds could not be
            selected or removed anywhere. Games without worlds render nothing here. */}
        <Worlds serverKey={key} />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={Tag}
        sub={server.display_name}
        right={
          <div className="flex items-center gap-2">
            <GhostButton disabled={checking || busy} onClick={checkNow} title={t("check.title")}>
              <RefreshCw className={checking ? "h-4 w-4 animate-spin" : "h-4 w-4"} /> {t("check.button")}
            </GhostButton>
            <GhostButton
              disabled={busy}
              onClick={recreate}
              title={t("recreate.title")}
            >
              <Boxes className="h-4 w-4" /> {t("recreate.label")}
            </GhostButton>
            <GhostButton disabled={busy} onClick={rollback}>
              <RotateCcw className="h-4 w-4" /> {t("rollback.label")}
            </GhostButton>
          </div>
        }
      >
        {t("title")}
      </SectionTitle>
      {err && <Alert kind="error">{err}</Alert>}

      <OperationPanel
        op={op}
        title={t(op?.kind === "version.recreate" ? "operationTitleRecreate" : "operationTitle")}
      />

      {/* The worlds sit here and not with the mods: a switch only takes effect with "Recreate",
          and the button for that is in this header. */}
      <Worlds serverKey={key} />

      {catalog?.supported && catalog.running_version_known === false && (
        <Alert kind="warn">{t("runningUnknown")}</Alert>
      )}
      {upgrade && (
        <Alert kind="info">
          <Trans
            t={t}
            i18nKey={upgrade.released_at ? "upgrade.availableDated" : "upgrade.available"}
            values={{
              version: upgrade.version,
              date: upgrade.released_at ? fmtDate(upgrade.released_at, true) : "",
            }}
            components={{
              v: <span className="font-mono" />,
              pick: (
                <button
                  className="underline underline-offset-2"
                  onClick={() => { setManual(false); setTarget(upgrade.ref); }}
                />
              ),
            }}
          />
        </Alert>
      )}

      <Card className="space-y-3">
        <div className="text-sm text-fg-muted">
          <Trans t={t} i18nKey="current" values={{ version: current }} components={{ v: <span className="font-mono text-fg" /> }} />
          {server.server_type ? ` (${server.server_type})` : ""}
        </div>

        {useDropdown ? (
          <Field
            label={t(pullsImage ? "target.labelImage" : "target.label")}
            hint={t("target.hint")}
          >
            <Select value={target} onChange={(e) => setTarget(e.target.value)} className="w-full">
              <option value="">{t("target.choose")}</option>
              {groups.map((g) => (
                <optgroup key={g.channel} label={g.label}>
                  {g.items.map((e) => (
                    <option key={e.ref} value={e.ref}>
                      {optionLabel(e)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </Select>
          </Field>
        ) : (
          <Field
            label={t(pullsImage ? "target.manualLabelImage" : "target.manualLabel")}
            hint={t("target.hint")}
          >
            <Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder={current} />
          </Field>
        )}

        {entries.length > 0 && (
          <button
            className="text-xs text-fg-dim underline underline-offset-2 hover:text-fg-muted"
            onClick={() => setManual((m) => !m)}
          >
            {t(manual ? "manual.back" : "manual.enter")}
          </button>
        )}

        {catalog && !catalog.supported && <Alert kind="info">{tb(catalog.note)}</Alert>}
        {catalog?.supported && catalog.source_ok === false && (
          <Alert kind="warn">
            {t("source.stale")}
          </Alert>
        )}

        {selected && (
          <div className="rounded-xl border border-line bg-surface-sunken p-3 text-sm">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Badge tone={selected.channel === "stable" ? "online" : "info"}>{tb(selected.channel_label)}</Badge>
              {selected.installed && <Badge tone="offline">{t("selected.downloaded")}</Badge>}
              <span className="text-xs text-fg-dim">
                {selected.released_at ? t("selected.released", { date: fmtDate(selected.released_at, true) }) : ""}
              </span>
            </div>
            {selected.changelog_title ? (
              <>
                <div className="font-medium text-fg">{selected.changelog_title}</div>
                {selected.changelog_summary && (
                  <p className="mt-1 text-fg-muted">{selected.changelog_summary}</p>
                )}
                {safeUrl(selected.changelog_url) && (
                  <a
                    className="mt-1 inline-block text-xs underline underline-offset-2 text-fg-muted hover:text-fg"
                    href={safeUrl(selected.changelog_url)}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {t("selected.changelogLink")}
                  </a>
                )}
              </>
            ) : (
              <div className="text-fg-dim">
                {t("selected.noChangelog")}
              </div>
            )}
          </div>
        )}

        {pull && (pull.running || !pull.ok) && pull.status && (
          <div className="rounded-xl border border-line bg-surface-sunken p-3 text-sm">
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-2 text-fg-muted">
                <Download className="h-4 w-4" /> {t("pull.status", { status: tb(pull.status) })}
              </span>
              <span className="font-mono text-xs text-fg-dim">
                {pull.total > 0 ? `${fmtBytes(pull.current)} / ${fmtBytes(pull.total)}` : ""}
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-surface-raised">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${Math.min(100, pull.percent ?? 0)}%` }}
              />
            </div>
            {pull.error && <div className="mt-1 text-xs text-danger-text">{tb(pull.error)}</div>}
          </div>
        )}

        <Button disabled={busy || !target || target === current || anyRunning} onClick={() => change(false)}>
          {t("change.button")}
        </Button>
        <Alert kind="warn">
          {t("change.warning")}
        </Alert>
        {catalog?.checked_at && (
          <div className="text-xs text-fg-dim">
            {t("checkedAt", { when: fmtDateTime(catalog.checked_at, true) })}
          </div>
        )}
      </Card>

      <Card>
        <div className="mb-2 text-xs uppercase tracking-wide text-fg-muted">{t("history.heading")}</div>
        {history.length === 0 ? (
          <div className="text-sm text-fg-dim">{t("history.empty")}</div>
        ) : (
          <div className="space-y-1.5">
            {history.map((h) => (
              <div
                key={h.id}
                className="flex items-center justify-between gap-2 rounded border border-line-soft px-3 py-2 text-sm"
              >
                <span className="font-mono text-fg-muted">
                  {h.old} → {h.new}
                </span>
                <Badge tone={h.status === "ok" ? "online" : h.status === "failed" ? "blood" : "info"}>
                  {h.direction}/{h.status}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
