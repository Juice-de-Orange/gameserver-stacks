import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Archive, Download, HardDriveDownload, Plus } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { useAuth } from "../auth";
import { useConfirm, useToast } from "../components/feedback";
import { OperationPanel, useOperation } from "../components/Operation";
import { useLiveConsole } from "../components/liveconsole";
import { useActiveServer } from "../components/ServerContext";
import { Alert, Button, DataTable, EmptyState, GhostButton, SectionTitle } from "../components/ui";
import { fmtBytes, fmtDateTime } from "../lib/format";
import { hasCap } from "../lib/game";

/**
 * From this size on the download asks first instead of just trying.
 *
 * The browser has to hold the WHOLE archive as a blob in memory before the first byte reaches
 * the disk — the backend streams, but the bearer token travels in a header, so a plain link is
 * not an option. A world archive can be several GB; on a phone the allocation fails.
 */
const GROSS_BYTES = 500 * 1024 * 1024;

interface Backup {
  name: string;
  size: number;
  created: string;
}

export default function Backups() {
  const { t } = useTranslation("backups");
  const confirm = useConfirm();
  const toast = useToast();
  const { emit } = useLiveConsole();
  const { me } = useAuth();
  const server = useActiveServer();
  const key = server?.key;
  const [list, setList] = useState<Backup[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // The restore runs as a background operation (mandatory backup + boot verification).
  const { op, track } = useOperation(key, "backup.restore");
  // So does the manual backup: a large world takes minutes, longer than a reverse proxy keeps
  // the request open. A synchronous call would report an error while the archive is still being
  // written — and the obvious second click would start a second run.
  const { op: backupOp, track: trackBackup } = useOperation(key, "backup.create");

  // Both restore paths need `require_admin` in the backend, the page itself only
  // `can_manage_backups`. Greyed out with a reason rather than hidden: the right is explainable,
  // and nobody clicks through the destructive dialog just to get a 403.
  const admin = me?.role === "admin";
  const restoreLock = admin ? undefined : t("lock.adminOnly");
  // `busy` only covers the request. The restore continues as a background operation — and a
  // backup taken while the world is being unpacked would save an intermediate state.
  const operationLock = op?.running
    ? t("lock.restoreRunning")
    : backupOp?.running
      ? t("lock.backupRunning")
      : undefined;

  async function load() {
    if (!key) return;
    try {
      setList(await apiJson(`/servers/${key}/backups`));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  async function create() {
    if (!key) return;
    setBusy(true);
    try {
      trackBackup(await apiJson<{ id: number }>(`/servers/${key}/backups`, { method: "POST" }));
      toast.success(t("create.started"));
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  // Once the backup has finished it belongs in the list — the fresh archive is exactly what
  // one looks for next.
  const backupRunning = backupOp?.running;
  useEffect(() => {
    if (backupOp && !backupOp.running) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backupRunning]);
  // The same after a restore: it writes a `pre-restore` safety archive, and that is the way back
  // the operator may want right away.
  const restoreRunning = op?.running;
  useEffect(() => {
    if (op && !op.running) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restoreRunning]);
  async function download(name: string, size: number) {
    if (size >= GROSS_BYTES) {
      const ok = await confirm({
        title: t("download.large.title"),
        body: t("download.large.body", { name, size: fmtBytes(size) }),
        confirmLabel: t("download.large.confirm"),
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const res = await api(`/servers/${key}/backups/${encodeURIComponent(name)}/download`);
      if (!res.ok) {
        // Without this check a 404 (rotated away) / 403 would save the JSON error body as the archive.
        toast.error(await errText(res, res.statusText));
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      // Do not revoke right away: the anchor is not in the document, and with several GB the
      // revocation would hit the middle of the save. A minute is enough for the browser to start
      // the download; after that the call frees the memory.
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e: any) {
      // A dropped connection or exhausted memory must be reported — otherwise nothing visible
      // happens at all: no file, no message.
      toast.error(t("download.failed", { error: e.message }));
    } finally {
      setBusy(false);
    }
  }
  /**
   * `onlyConfig` sends the symbolic `parts=config` — WHICH folders that means is the driver's
   * knowledge, not this page's. The button is additionally tied to the capability
   * "backup-config-only": for a game without it the restore would stop the server and then find
   * nothing to restore.
   */
  async function restore(name: string, onlyConfig = false) {
    const ok = await confirm({
      title: onlyConfig ? t("restore.config.title") : t("restore.full.title"),
      body: onlyConfig ? t("restore.config.body", { name }) : t("restore.full.body", { name }),
      danger: !onlyConfig,
      confirmLabel: onlyConfig ? t("restore.config.confirm") : t("restore.full.confirm"),
    });
    if (!ok) return;
    emit(t("restore.consoleLine", { name }));
    setBusy(true);
    try {
      const query = onlyConfig ? "?parts=config" : "";
      const started = await apiJson<{ id: number }>(
        `/servers/${key}/backups/${encodeURIComponent(name)}/restore${query}`,
        { method: "POST" },
      );
      track(started);   // from here on the operation panel shows the progress
      toast.success(t("restore.started"));
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <OperationPanel op={op} title={t("operation.restore")} />
      <OperationPanel op={backupOp} title={t("operation.create")} />
      <SectionTitle
        icon={Archive}
        sub={`${t("subtitle")}${server ? ` · ${server.display_name}` : ""}`}
        right={
          <Button disabled={busy} disabledReason={operationLock} onClick={create}>
            <Plus className="h-4 w-4" /> {t("create.label")}
          </Button>
        }
      >
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      <DataTable<Backup>
        rows={list}
        rowKey={(b) => b.name}
        empty={<EmptyState icon={Archive}>{t("empty")}</EmptyState>}
        columns={[
          { header: t("column.backup"), cell: (b) => <span className="font-mono text-xs text-fg">{b.name}</span> },
          { header: t("column.size"), cell: (b) => fmtBytes(b.size) },
          { header: t("column.created"), cell: (b) => <span className="text-fg-dim">{fmtDateTime(b.created)}</span> },
        ]}
        actions={(b) => (
          <>
            <GhostButton onClick={() => download(b.name, b.size)} disabled={busy} aria-label={t("download.aria", { name: b.name })}>
              <Download className="h-4 w-4" />
            </GhostButton>
            {hasCap(server, "backup-config-only") && (
              <GhostButton
                onClick={() => restore(b.name, true)}
                disabled={busy}
                disabledReason={restoreLock || operationLock}
                title={t("restore.config.hint")}
              >
                {t("restore.config.label")}
              </GhostButton>
            )}
            <GhostButton onClick={() => restore(b.name)} disabled={busy} disabledReason={restoreLock || operationLock}>
              <HardDriveDownload className="h-4 w-4" /> {t("restore.full.label")}
            </GhostButton>
          </>
        )}
      />
    </div>
  );
}
