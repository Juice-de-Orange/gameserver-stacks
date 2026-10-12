// World and save management — for the games that keep several next to each other.
//
// For Factorio these are `saves/*.zip`, for Valheim `worlds_local/<name>/` (or an older `<name>.db|.fwl`).
//
// **The sentence that must never be missing here:** a switch only takes effect after the
// container is RECREATED, not after a restart. The active world depends on a container variable,
// and a restart does not pick that up. That is why the backend's note is passed on as it is —
// it is not summarised here.
import { useCallback, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Boxes, Check, Trash2 } from "lucide-react";
import { api, apiJson, detailText } from "../api";
import { tb } from "../i18n";
import { useConfirm, useToast } from "./feedback";
import { Alert, Badge, Card, EmptyState, GhostButton, SectionTitle, Spinner, cx } from "./ui";
import { fmtBytes, fmtRelative } from "../lib/format";

interface World {
  name: string;
  active: boolean;
  size: number;
  modified: string;
  files: string[];
  reason?: string;
}

interface WorldsResponse {
  worlds: World[];
  switchable: boolean;
  removable: boolean;
}

export function Worlds({ serverKey }: { serverKey: string }) {
  const { t } = useTranslation("worlds");
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<WorldsResponse | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await apiJson<WorldsResponse>(`/servers/${serverKey}/worlds`));
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [serverKey]);

  useEffect(() => {
    load();
  }, [load]);

  async function select(name: string) {
    const ok = await confirm({
      title: t("select.confirm.title", { name }),
      body: t("select.confirm.body"),
      confirmLabel: t("select.action"),
    });
    if (!ok) return;
    setBusy(name);
    try {
      const r = await api(`/servers/${serverKey}/worlds/${encodeURIComponent(name)}/select`, {
        method: "POST",
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("select.failed")));
      // The note comes from the backend as it is — it says what is still missing.
      toast.info(d.note ? tb(String(d.note)).replace(/\*\*/g, "") : t("select.done"));
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function remove(name: string) {
    const ok = await confirm({
      title: t("remove.confirm.title", { name }),
      body: t("remove.confirm.body"),
      danger: true,
      confirmLabel: t("remove.action"),
    });
    if (!ok) return;
    setBusy(name);
    try {
      const r = await api(`/servers/${serverKey}/worlds/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("remove.failed")));
      toast.success(t("remove.done", { name }));
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  if (err) return <Alert kind="error">{err}</Alert>;
  if (!data) return <Card><Spinner /></Card>;
  if (!data.switchable && data.worlds.length === 0) return null;

  return (
    <div className="space-y-3">
      <SectionTitle icon={Boxes} sub={t("sub")}>
        {t("title")}
      </SectionTitle>

      <Card className="space-y-2">
        {data.worlds.length === 0 ? (
          <EmptyState icon={Boxes}>{t("empty")}</EmptyState>
        ) : (
          data.worlds.map((s) => (
            <div
              key={s.name}
              className={cx(
                "flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2",
                s.active ? "border-primary/50 bg-primary-soft" : "border-line bg-surface-card/50",
              )}
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-mono text-sm text-fg">{s.name}</span>
                  {s.active && (
                    <Badge tone="online">
                      <Check className="h-3 w-3" /> {t("active")}
                    </Badge>
                  )}
                </div>
                <div className="text-[11px] text-fg-dim">
                  {fmtBytes(s.size)} · {fmtRelative(s.modified)}
                  {s.files.length > 1 ? ` · ${t("files", { count: s.files.length })}` : ""}
                  {/* For Factorio "active" is not a property of the file but of the container
                      environment: `LOAD_LATEST_SAVE` beats `SAVE_NAME`. The backend's reason
                      says so. */}
                  {s.active && s.reason ? ` · ${tb(s.reason)}` : ""}
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                {data.switchable && (
                  <GhostButton
                    disabled={!!busy || s.active}
                    disabledReason={s.active ? t("select.already") : undefined}
                    onClick={() => select(s.name)}
                  >
                    {t("select.action")}
                  </GhostButton>
                )}
                {data.removable && (
                  <GhostButton
                    title={t("remove.title")}
                    disabled={!!busy || s.active}
                    disabledReason={s.active ? t("remove.activeReason") : undefined}
                    onClick={() => remove(s.name)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </GhostButton>
                )}
              </div>
            </div>
          ))
        )}
        {data.switchable && (
          <p className="text-xs text-fg-dim">
            <Trans t={t} i18nKey="note" components={{ strong: <strong /> }} />
          </p>
        )}
      </Card>
    </div>
  );
}
