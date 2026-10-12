// The Workshop check: do the mods still exist on Steam — and a button that clears away the
// dead ones.
//
// **Why this is a card of its own and not part of the preflight:** the preflight
// (`services/mods/preflight.py`) is deliberately offline and stays that way. This answer comes
// from the network, so it can be missing. That is why it has three states instead of two —
// "cannot be checked" is visible here and is NOT glossed over as "fine".
//
// An unclear answer from Steam is not proof that a mod is gone. So the button only clears away
// what is **proven** gone, and the backend rejects the call when the check was incomplete.
import { useCallback, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, HelpCircle, RefreshCw, Trash2 } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { useConfirm, useToast } from "./feedback";
import { Alert, Badge, Button, Card, GhostButton, SectionTitle, Spinner } from "./ui";
import { safeUrl } from "../lib/url";

type Availability = "available" | "removed" | "unverifiable";

interface HealthItem {
  workshop_id: string;
  title: string;
  url: string;
  availability: Availability;
  mod_ids: string[];
}

interface HealthIssue {
  level: "error" | "warn";
  code: string;
  text: string;
  workshop_id: string;
}

interface HealthReport {
  items: HealthItem[];
  issues: HealthIssue[];
  summary: {
    ok: boolean;
    errors: number;
    warnings: number;
    mod_count: number;
    steam_ok: boolean;
    removed: string[];
    list_readable: boolean;
    inventory_readable: boolean;
  };
}

export function WorkshopHealth({
  serverKey,
  onChanged,
}: {
  serverKey: string;
  onChanged: () => void | Promise<void>;
}) {
  const { t } = useTranslation("workshopHealth");
  const confirm = useConfirm();
  const toast = useToast();
  const [rep, setRep] = useState<HealthReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(
    async (force = false) => {
      if (!serverKey) return;
      setBusy(true);
      try {
        setRep(await apiJson<HealthReport>(
          `/servers/${serverKey}/mods/health${force ? "?force=true" : ""}`,
        ));
        setErr("");
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [serverKey],
  );

  useEffect(() => {
    void load();
  }, [load]);

  async function recheck() {
    await load(true);
  }

  async function cleanUp() {
    const removed = rep?.summary.removed ?? [];
    const ok = await confirm({
      title: t("cleanup.confirm.title"),
      body: t("cleanup.confirm.body", { count: removed.length }),
      confirmLabel: t("cleanup.label"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${serverKey}/mods/cleanup`, { method: "POST" });
      if (!r.ok) throw new Error(await errText(r, t("cleanup.failed")));
      const data = (await r.json()) as { removed_items: string[] };
      toast.success(
        data.removed_items.length
          ? t("cleanup.done", { count: data.removed_items.length })
          : t("cleanup.nothing"),
      );
      await onChanged();
      await load(true);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!rep && busy) {
    return (
      <Card className="flex items-center gap-2 text-sm text-muted">
        <Spinner /> {t("loading")}
      </Card>
    );
  }
  if (err) return <Alert kind="error">{err}</Alert>;
  if (!rep) return null;

  const { summary } = rep;
  const unverifiable = rep.items.filter((i) => i.availability === "unverifiable");
  const removed = rep.items.filter((i) => i.availability === "removed");

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionTitle icon={summary.ok ? CheckCircle2 : AlertTriangle}>
          {t("title")}
        </SectionTitle>
        <GhostButton onClick={recheck} disabled={busy} title={t("recheck.title")}>
          <RefreshCw className="h-4 w-4" /> {t("recheck.button")}
        </GhostButton>
      </div>

      {!summary.list_readable && (
        <Alert kind="error">
          <Trans t={t} i18nKey="listUnreadable" components={{ strong: <strong /> }} />
        </Alert>
      )}

      {summary.list_readable && !summary.steam_ok && (
        <Alert kind="warn">
          <Trans t={t} i18nKey="incomplete" components={{ strong: <strong /> }} />
        </Alert>
      )}

      {removed.length > 0 ? (
        <div className="space-y-2">
          <div className="text-sm font-semibold text-danger-text">
            {t("removed.heading", { count: removed.length })}
          </div>
          <p className="text-xs text-muted">
            {t("removed.explain")}
          </p>
          <ul className="space-y-1 text-sm">
            {removed.map((i) => (
              <li key={i.workshop_id} className="flex flex-wrap items-center gap-2">
                <Badge tone="blood">{t("removed.badge")}</Badge>
                <a
                  href={safeUrl(i.url)}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="underline"
                >
                  {i.title}
                </a>
                <span className="font-mono text-xs text-muted">{i.workshop_id}</span>
              </li>
            ))}
          </ul>
          <Button onClick={cleanUp} disabled={busy || !summary.steam_ok}>
            <Trash2 className="h-4 w-4" /> {t("cleanup.label")}
          </Button>
        </div>
      ) : (
        summary.list_readable &&
        summary.steam_ok && (
          <div className="text-sm text-muted">
            {t("allAvailable", { n: summary.mod_count })}
          </div>
        )
      )}

      {unverifiable.length > 0 && (
        <div className="space-y-1">
          <div className="flex items-center gap-1 text-sm font-semibold">
            <HelpCircle className="h-4 w-4" /> {t("unverifiable.heading", { n: unverifiable.length })}
          </div>
          <p className="text-xs text-muted">
            {t("unverifiable.explain")}
          </p>
          <ul className="text-xs text-muted">
            {unverifiable.map((i) => (
              <li key={i.workshop_id} className="font-mono">
                {i.workshop_id} {i.title}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
