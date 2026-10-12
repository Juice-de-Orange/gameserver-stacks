// The opt-in firewall helper (`console-firewall`, compose profile `firewall`): what it would open,
// the exact rule diff, and the apply button — plus a one-line status on a server's page.
//
// Backend: `routers/firewall.py`. `GET /api/settings/firewall` never changes anything; it returns
// the helper's plan for the current servers. `POST /api/settings/firewall/apply` sends back that
// plan's id, and the helper applies it only if nothing changed in between (409 otherwise: show the
// new preview).
// Opening needs this button; ports of switched-off or removed servers close by themselves.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { RefreshCw, Shield, ShieldCheck } from "lucide-react";
import { api, apiJson, detailText, errText } from "../api";
import { useAuth } from "../auth";
import { useToast } from "./feedback";
import { Alert, Badge, Button, Card, GhostButton, Skeleton } from "./ui";

export interface FwAllow {
  server: string;
  port: number;
  proto: string;
  address: string;
  /** In a plan: `open` (rule present), `opens` (added on apply), `skipped` (no managed family). */
  state?: "open" | "opens" | "skipped";
}

interface FwFamilyPlan {
  create_chain: boolean;
  hook: "ok" | "insert" | "move" | "dedupe";
  add: string[];
  remove: string[];
  changes: number;
}

interface FwFamily {
  status: "ok" | "absent";
  plan?: FwFamilyPlan;
}

export interface FwPlan {
  plan_id: string;
  backend: string;
  families: Record<string, FwFamily>;
  allows: FwAllow[];
  closes: FwAllow[];
  changes: number;
  applied?: number;
}

export interface FirewallState {
  status: "off" | "ready" | "error";
  desired: { allows: FwAllow[]; sources: string[] };
  plan: FwPlan | null;
  error: unknown;
}

const FAMILIES = ["ipv4", "ipv6"] as const;

function portLabel(a: FwAllow): string {
  return `${a.port}/${a.proto}`;
}

/** The response, or null if it is not one (an older backend without the route answers otherwise). */
function asState(data: unknown): FirewallState | null {
  const s = data as FirewallState | null;
  if (!s || !["off", "ready", "error"].includes(s.status) || !Array.isArray(s.desired?.allows)) return null;
  return { ...s, desired: { allows: s.desired.allows, sources: s.desired.sources ?? [] } };
}

function problemsOf(error: unknown): string[] {
  const problems = (error as { problems?: unknown } | null)?.problems;
  return Array.isArray(problems) ? problems.map(String) : [];
}

/** Settings → Network: status, the ports, the rule diff and "Apply". */
export function FirewallPanel() {
  const { t } = useTranslation("firewall");
  const toast = useToast();
  const [state, setState] = useState<FirewallState | null>(null);
  const [loadError, setLoadError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoadError("");
    return apiJson<unknown>("/settings/firewall")
      .then((data) => {
        const next = asState(data);
        setState(next);
        if (!next) setLoadError(t("unexpected"));
      })
      .catch((e: Error) => setLoadError(e.message));
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function apply() {
    if (!state?.plan) return;
    setBusy(true);
    setApplyError("");
    try {
      const res = await api("/settings/firewall/apply", { method: "POST", body: JSON.stringify({ plan_id: state.plan.plan_id }) });
      if (!res.ok) {
        setApplyError(await errText(res, t("applyFailed")));
        // A 409 means the preview is out of date: show the current one.
        if (res.status === 409) await load();
        return;
      }
      const next = asState(await res.json());
      setState(next);
      toast.success(t("applied", { count: next?.plan?.applied ?? 0 }));
    } finally {
      setBusy(false);
    }
  }

  const plan = state?.plan ?? null;
  const badge =
    state?.status === "ready" ? (
      <Badge tone="online">{t("status.ready")}</Badge>
    ) : state?.status === "error" ? (
      <Badge tone="blood">{t("status.error")}</Badge>
    ) : state ? (
      <Badge tone="offline">{t("status.off")}</Badge>
    ) : null;

  return (
    <Card className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Shield className="h-4 w-4 text-primary" /> {t("title")} {badge}
        </div>
        {state && state.status !== "off" && (
          <GhostButton type="button" disabled={busy} onClick={() => void load()}>
            <RefreshCw className="h-4 w-4" /> {t("refresh")}
          </GhostButton>
        )}
      </div>
      <p className="text-xs text-fg-dim">{t("intro")}</p>

      {!state && !loadError && <Skeleton className="h-24" />}
      {loadError && <Alert kind="error">{loadError}</Alert>}

      {state?.status === "off" && <Alert kind="info">{t("off")}</Alert>}
      {state?.status === "error" && (
        <Alert kind="error">
          <div>{detailText(state.error, t("applyFailed"))}</div>
          {problemsOf(state.error).length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs">
              {problemsOf(state.error).map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
        </Alert>
      )}

      {state && <PortTable state={state} />}

      {plan && (
        <div className="space-y-3">
          <div className="text-xs text-fg-muted">
            {t("backend", { name: plan.backend })} ·{" "}
            {state && state.desired.sources.length > 0
              ? t("sources.some", { list: state.desired.sources.join(", ") })
              : t("sources.any")}
          </div>
          {FAMILIES.map((family) => (
            <FamilyDiff key={family} family={family} entry={plan.families[family]} />
          ))}
          {plan.closes.length > 0 && (
            <div className="text-xs text-fg-muted" data-testid="firewall-closes">
              {t("closes", { list: plan.closes.map((c) => `${c.server} ${portLabel(c)}`).join(", ") })}
            </div>
          )}
          {applyError && <Alert kind="error">{applyError}</Alert>}
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" disabled={busy || plan.changes === 0} onClick={() => void apply()}>
              <ShieldCheck className="h-4 w-4" /> {t("apply", { count: plan.changes })}
            </Button>
            {plan.changes === 0 && <span className="text-sm text-fg-muted">{t("inSync")}</span>}
          </div>
          <div className="text-xs text-fg-dim">{t("closesByItself")}</div>
        </div>
      )}
    </Card>
  );
}

function PortTable({ state }: { state: FirewallState }) {
  const { t } = useTranslation("firewall");
  const rows: FwAllow[] = state.plan?.allows ?? state.desired.allows;
  if (rows.length === 0) return <div className="text-sm text-fg-muted">{t("noPorts")}</div>;
  return (
    <table className="w-full text-left text-sm" data-testid="firewall-ports">
      <thead>
        <tr className="text-xs uppercase tracking-wide text-fg-muted">
          <th className="py-1 pr-3 font-medium">{t("col.server")}</th>
          <th className="py-1 pr-3 font-medium">{t("col.port")}</th>
          <th className="py-1 pr-3 font-medium">{t("col.address")}</th>
          <th className="py-1 font-medium">{t("col.state")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={`${a.server}-${a.port}-${a.proto}-${a.address}`} className="border-t border-line">
            <td className="py-1 pr-3">{a.server}</td>
            <td className="py-1 pr-3 font-mono text-xs">{portLabel(a)}</td>
            <td className="py-1 pr-3 font-mono text-xs">{a.address || t("anyAddress")}</td>
            <td className="py-1">{a.state ? <StateBadge state={a.state} /> : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StateBadge({ state }: { state: NonNullable<FwAllow["state"]> }) {
  const { t } = useTranslation("firewall");
  if (state === "open") return <Badge tone="online">{t("state.open")}</Badge>;
  if (state === "opens") return <Badge tone="warn">{t("state.opens")}</Badge>;
  return <Badge tone="offline">{t("state.skipped")}</Badge>;
}

function FamilyDiff({ family, entry }: { family: string; entry: FwFamily | undefined }) {
  const { t } = useTranslation("firewall");
  const name = family === "ipv6" ? "IPv6" : "IPv4";
  if (!entry || entry.status === "absent" || !entry.plan) {
    return <div className="text-xs text-fg-muted">{t("family.absent", { name })}</div>;
  }
  const p = entry.plan;
  if (p.changes === 0) return <div className="text-xs text-fg-muted">{t("family.inSync", { name })}</div>;
  return (
    <div className="space-y-1" data-testid={`firewall-diff-${family}`}>
      <div className="text-xs font-semibold text-fg">{t("family.changes", { name, count: p.changes })}</div>
      <pre className="overflow-x-auto rounded-lg border border-line bg-surface-sunken p-2 font-mono text-[11px] leading-5">
        {p.create_chain && <div className="text-success-text">{t("diff.createChain")}</div>}
        {p.hook !== "ok" && <div className="text-success-text">{t(`diff.hook.${p.hook}`)}</div>}
        {p.remove.map((r, i) => (
          <div key={`r${i}`} className="text-danger-text">
            − {r}
          </div>
        ))}
        {p.add.map((r) => (
          <div key={r} className="text-success-text">
            + {r}
          </div>
        ))}
      </pre>
    </div>
  );
}

/** One line on a server's page (admins only): are its game ports open in the firewall? */
export function FirewallServerStatus({ serverKey }: { serverKey: string }) {
  const { t } = useTranslation("firewall");
  const { me } = useAuth();
  const [state, setState] = useState<FirewallState | null>(null);
  const isAdmin = me?.role === "admin";

  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    apiJson<unknown>("/settings/firewall")
      .then((s) => alive && setState(asState(s)))
      .catch(() => alive && setState(null));
    return () => {
      alive = false;
    };
  }, [isAdmin, serverKey]);

  if (!isAdmin || !state || state.status === "off") return null;
  const mine = (state.plan?.allows ?? state.desired.allows).filter((a) => a.server === serverKey);
  const closing = (state.plan?.closes ?? []).filter((a) => a.server === serverKey);
  if (state.status === "ready" && mine.length === 0 && closing.length === 0) return null;
  const pending = mine.filter((a) => a.state === "opens");
  const text =
    state.status === "error"
      ? t("server.error")
      : pending.length > 0
        ? t("server.pending", { list: pending.map(portLabel).join(", ") })
        : closing.length > 0 && mine.length === 0
          ? t("server.closing", { list: closing.map(portLabel).join(", ") })
          : t("server.open", { list: mine.map(portLabel).join(", ") });
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted" data-testid="firewall-server-status">
      <Shield className="h-3.5 w-3.5" />
      <span>{text}</span>
      <Link to="/settings/network" className="text-primary hover:underline">
        {t("server.link")}
      </Link>
    </div>
  );
}
