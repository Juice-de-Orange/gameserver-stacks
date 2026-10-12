// Factorio's mod portal in the UI: search, install, see updates.
//
// **Why searching happens on a button press and not while typing:** the portal itself cannot
// search — `?q=` and `?search=` are silently ignored there. So the backend fetches the full
// list (more than ten MB) and filters locally; search-as-you-type would be one such download
// per keystroke.
//
// **And why "Install" can be greyed out:** without a factorio.com account the portal does not
// deliver downloads, it redirects to the login page. The backend then refuses instead of storing
// an HTML page with the extension `.zip` in the mod folder — the server would not come up again.
import { useCallback, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { ArrowUpCircle, Download, Package, Search } from "lucide-react";
import { api, apiJson, detailText } from "../api";
import { useToast } from "./feedback";
import { Alert, Badge, Button, Card, GhostButton, Input, SectionTitle, Spinner, cx } from "./ui";
import { currentLanguage, tb } from "../i18n";

interface PortalHit {
  name: string;
  title: string;
  owner: string;
  summary: string;
  downloads_count: number;
  version: string;
  factorio_version: string;
}

interface UpdateRow {
  name: string;
  title?: string;
  installed: string;
  available: string;
  update: boolean;
  unknown: boolean;
  note?: string;
}

export function ModPortal({ serverKey, onChange }: { serverKey: string; onChange: () => void }) {
  const { t } = useTranslation("modPortal");
  const toast = useToast();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<PortalHit[] | null>(null);
  const [updates, setUpdates] = useState<UpdateRow[] | null>(null);
  const [updatesLoading, setUpdatesLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [access, setAccess] = useState<{ possible: boolean; reason: string }>({
    possible: true,
    reason: "",
  });

  const loadUpdates = useCallback(async () => {
    setUpdatesLoading(true);
    try {
      const d = await apiJson<{ mods: UpdateRow[]; download_possible: boolean; download_reason: string }>(
        `/servers/${serverKey}/mods/portal/updates`,
      );
      setUpdates(d.mods);
      setAccess({ possible: d.download_possible, reason: d.download_reason });
      setErr("");
    } catch (e) {
      // An unreachable portal is NOT an empty list — otherwise "everything up to date" would
      // read like an answer although nothing was checked.
      setUpdates(null);
      setErr((e as Error).message);
    } finally {
      setUpdatesLoading(false);
    }
  }, [serverKey]);

  useEffect(() => {
    loadUpdates();
  }, [loadUpdates]);

  async function runSearch() {
    if (q.trim().length < 2) {
      toast.error(t("search.tooShort"));
      return;
    }
    setBusy("search");
    try {
      const d = await apiJson<{ results: PortalHit[]; download_possible: boolean; download_reason: string }>(
        `/servers/${serverKey}/mods/portal/search?q=${encodeURIComponent(q.trim())}`,
      );
      setHits(d.results);
      setAccess({ possible: d.download_possible, reason: d.download_reason });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function install(name: string) {
    setBusy(name);
    try {
      const r = await api(`/servers/${serverKey}/mods/portal/${encodeURIComponent(name)}`, {
        method: "POST",
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailText(d.detail, t("install.failed")));
      toast.success(t("install.done", { file: d.file }));
      onChange();
      await loadUpdates();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  const outdated = (updates || []).filter((u) => u.update);
  const unknownRows = (updates || []).filter((u) => u.unknown);

  return (
    <div className="space-y-3">
      <SectionTitle icon={Package} sub={t("sub")}>
        {t("title")}
      </SectionTitle>

      {!access.possible && <Alert kind="warn">{tb(access.reason)}</Alert>}
      {err && <Alert kind="error">{t("updates.error", { error: err })}</Alert>}

      <Card className="space-y-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-fg-muted">
          {t("updates.heading")}
        </div>
        {updatesLoading ? (
          <Spinner />
        ) : updates === null ? (
          <div className="text-sm text-fg-dim">
            {t("updates.notChecked")}
          </div>
        ) : outdated.length === 0 ? (
          <div className="text-sm text-fg-dim">
            {unknownRows.length ? t("updates.allCurrentUnknown", { n: unknownRows.length }) : t("updates.allCurrent")}
          </div>
        ) : (
          outdated.map((u) => (
            <div
              key={u.name}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line bg-surface-card/50 px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-sm text-fg">{u.title || u.name}</div>
                <div className="font-mono text-[11px] text-fg-dim">
                  {u.installed} → {u.available}
                </div>
              </div>
              <GhostButton
                disabled={!!busy}
                disabledReason={access.possible ? undefined : tb(access.reason)}
                onClick={() => install(u.name)}
              >
                <ArrowUpCircle className="h-3.5 w-3.5" /> {t("updates.download")}
              </GhostButton>
            </div>
          ))
        )}
        {unknownRows.length > 0 && (
          <p className="text-xs text-fg-dim">
            <Trans
              t={t}
              i18nKey="updates.unknown"
              count={unknownRows.length}
              values={{ names: unknownRows.map((u) => u.name).join(", ") }}
              components={{ names: <span className="font-mono" /> }}
            />
          </p>
        )}
      </Card>

      <Card className="space-y-2">
        <div className="flex flex-wrap items-end gap-2">
          <Input
            value={q}
            placeholder={t("search.placeholder")}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") runSearch();
            }}
            className="max-w-sm"
          />
          <Button disabled={busy === "search"} onClick={runSearch}>
            <Search className={cx("h-4 w-4", busy === "search" && "animate-pulse")} /> {t("common:action.search")}
          </Button>
          <span className="text-xs text-fg-dim">
            {t("search.note")}
          </span>
        </div>

        {hits !== null && hits.length === 0 && (
          <div className="text-sm text-fg-dim">{t("search.empty")}</div>
        )}
        {(hits || []).map((hit) => (
          <div
            key={hit.name}
            className="flex flex-wrap items-start justify-between gap-2 rounded-md border border-line bg-surface-card/50 px-3 py-2"
          >
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="truncate text-sm text-fg">{hit.title || hit.name}</span>
                <Badge tone="info">{hit.version}</Badge>
                {hit.factorio_version && (
                  <Badge tone="offline">{t("result.forVersion", { version: hit.factorio_version })}</Badge>
                )}
              </div>
              <div className="truncate text-[11px] text-fg-dim">
                {t("result.downloads", {
                  owner: hit.owner,
                  downloads: hit.downloads_count.toLocaleString(currentLanguage()),
                })}
              </div>
              {hit.summary && <div className="mt-0.5 line-clamp-2 text-xs text-fg-muted">{hit.summary}</div>}
            </div>
            <GhostButton
              disabled={!!busy}
              disabledReason={access.possible ? undefined : tb(access.reason)}
              onClick={() => install(hit.name)}
            >
              <Download className="h-3.5 w-3.5" /> {t("result.install")}
            </GhostButton>
          </div>
        ))}
      </Card>
    </div>
  );
}
