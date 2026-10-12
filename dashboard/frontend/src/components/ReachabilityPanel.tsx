// Reachability of one server (admins only): the checklist, the check from inside and the opt-in
// check from outside — `GET/POST /api/servers/:key/reachability` (`routers/reachability.py`).
//
// Collapsed until opened: the checklist is read once per opening, not on every overview poll.
// The outside check sends the public address to third-party services, so the panel lists which
// service receives what BEFORE the button can be pressed; the backend refuses while it is off.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, Copy, Globe2, Router, ShieldCheck } from "lucide-react";
import { apiJson, detailText } from "../api";
import { useToast } from "./feedback";
import { Alert, Badge, Card, GhostButton, Tabs } from "./ui";

interface Port {
  name: string;
  port: number;
  proto: string;
}

interface Coded {
  code: string;
  message: string;
  params: Record<string, unknown>;
}

interface Service {
  id: "steam" | "mcstatus" | "portcheck";
  name: string;
  url: string;
  sends: "ip" | "ip_port" | "ip_ports";
  enabled: boolean;
}

interface PlanRow extends Port {
  service: "steam" | "mcstatus" | "portcheck" | "none";
  state: "on" | "off" | "none";
}

export interface Reachability {
  key: string;
  game_type: string;
  ports: Port[];
  bind: { address: string; scope: "all" | "loopback" | "private" | "public" };
  public: { host: string; source: "server" | "installation" | "none"; kind: "name" | "public_ip" | "private_ip" | "none" };
  commands: { ufw: string[]; iptables: string[]; nftables: string[] };
  outside: { enabled: boolean; services: Service[]; plan: PlanRow[]; cache_s: number };
}

interface InsideResult {
  ran: boolean;
  reason: Coded | null;
  published: (Port & { ok: boolean; detail: Coded | null })[];
  answers: { channel: "rcon" | "battleye" | "query" | "a2s"; port: number | null; ok: boolean; latency_ms: number | null; error: Coded | null }[];
}

type OutsideStatus = "reachable" | "unreachable" | "listed" | "not_listed" | "not_checkable" | "service_off" | "error";

interface OutsideResult {
  ran: boolean;
  reason: Coded | null;
  address: string | null;
  ports: (Port & { service: string; status: OutsideStatus; detail: Coded })[];
}

const STATUS_TONE: Record<OutsideStatus, "online" | "blood" | "warn" | "info"> = {
  reachable: "online",
  listed: "online",
  unreachable: "blood",
  not_listed: "warn",
  error: "warn",
  not_checkable: "info",
  service_off: "info",
};

type Firewall = "iptables" | "nftables" | "ufw";

const portLabel = (p: Port) => `${p.port}/${p.proto.toUpperCase()}`;

export function ReachabilityPanel({ serverKey }: { serverKey: string }) {
  const { t } = useTranslation("reachabilityPanel");
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Reachability | null>(null);
  const [error, setError] = useState("");

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      setError("");
      apiJson<Reachability>(`/servers/${serverKey}/reachability`)
        .then(setData)
        .catch((e: Error) => setError(e.message));
    }
  };

  return (
    <Card className="space-y-4">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-left text-xs uppercase tracking-wide text-fg-muted hover:text-fg"
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <Globe2 className="h-4 w-4" /> {t("title")}
        <span className="ml-auto normal-case tracking-normal">{open ? t("hide") : t("show")}</span>
      </button>
      {open && error && <Alert kind="error">{error}</Alert>}
      {open && data && <Checklist data={data} />}
    </Card>
  );
}

function Checklist({ data }: { data: Reachability }) {
  const { t } = useTranslation("reachabilityPanel");
  const loopback = data.bind.scope === "loopback";
  return (
    <div className="space-y-5 text-sm">
      <section className="space-y-2">
        <h3 className="font-semibold text-fg">{t("ports.title")}</h3>
        {data.ports.length ? (
          <ul className="flex flex-wrap gap-2" data-testid="reachability-ports">
            {data.ports.map((p) => (
              <li key={`${p.name}-${p.port}-${p.proto}`} className="rounded-md border border-line bg-surface-raised px-2 py-1 font-mono text-xs">
                {portLabel(p)} <span className="text-fg-dim">{p.name}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-fg-muted">{t("ports.empty")}</p>
        )}
        <p className="text-fg-muted" data-testid="reachability-bind">
          {t(`bind.${data.bind.scope}`, { address: data.bind.address })}
        </p>
        <PublicAddress data={data} />
      </section>

      {!loopback && <FirewallCommands commands={data.commands} />}

      {!loopback && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 font-semibold text-fg">
            <Router className="h-4 w-4" /> {t("router.title")}
          </h3>
          <ul className="list-disc space-y-1 pl-5 text-fg-muted">
            <li>{t("router.forward")}</li>
            <li>{t("router.cgnat")}</li>
            <li>{t("router.cloud")}</li>
          </ul>
        </section>
      )}

      <InsideCheck serverKey={data.key} />
      <OutsideCheck data={data} />
    </div>
  );
}

function PublicAddress({ data }: { data: Reachability }) {
  const { t } = useTranslation("reachabilityPanel");
  const { host, source, kind } = data.public;
  if (kind === "none") {
    return (
      <p className="text-fg-muted" data-testid="reachability-public">
        {t("public.none")}{" "}
        <Link to="/settings/network" className="font-semibold text-primary-text underline-offset-2 hover:underline">
          {t("public.setIt")}
        </Link>
      </p>
    );
  }
  return (
    <p className={kind === "private_ip" ? "text-warning-text" : "text-fg-muted"} data-testid="reachability-public">
      {kind === "private_ip" ? t("public.private", { host }) : t(`public.${source}`, { host })}
    </p>
  );
}

function FirewallCommands({ commands }: { commands: Reachability["commands"] }) {
  const { t } = useTranslation(["reachabilityPanel", "common"]);
  const toast = useToast();
  const [tab, setTab] = useState<Firewall>("iptables");
  const lines = commands[tab];
  return (
    <section className="space-y-2">
      <h3 className="flex items-center gap-2 font-semibold text-fg">
        <ShieldCheck className="h-4 w-4" /> {t("firewall.title")}
      </h3>
      <Alert kind="warn">{t("firewall.docker")}</Alert>
      <Tabs
        items={(["iptables", "nftables", "ufw"] as Firewall[]).map((k) => ({ key: k, label: t(`firewall.tab.${k}`) }))}
        active={tab}
        onSelect={(k) => setTab(k as Firewall)}
      />
      <p className="text-fg-muted">{t(`firewall.hint.${tab}`)}</p>
      {lines.length > 0 && (
        <div className="space-y-2">
          <pre data-testid="reachability-commands" className="overflow-x-auto rounded-md border border-line bg-surface-raised p-3 font-mono text-xs text-fg">
            {lines.join("\n")}
          </pre>
          <GhostButton
            onClick={() => {
              navigator.clipboard?.writeText(lines.join("\n"));
              toast.success(t("common:state.copied"));
            }}
          >
            <Copy className="h-4 w-4" /> {t("common:action.copy")}
          </GhostButton>
        </div>
      )}
    </section>
  );
}

function InsideCheck({ serverKey }: { serverKey: string }) {
  const { t } = useTranslation("reachabilityPanel");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InsideResult | null>(null);
  const [error, setError] = useState("");

  const run = () => {
    setBusy(true);
    setError("");
    apiJson<InsideResult>(`/servers/${serverKey}/reachability/inside`, { method: "POST" })
      .then(setResult)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <section className="space-y-2">
      <h3 className="font-semibold text-fg">{t("inside.title")}</h3>
      <p className="text-fg-muted">{t("inside.text")}</p>
      <GhostButton onClick={run} disabled={busy}>
        {busy ? t("checking") : t("inside.run")}
      </GhostButton>
      {error && <Alert kind="error">{error}</Alert>}
      {result && (
        <ul className="space-y-1" data-testid="reachability-inside">
          {result.reason && <li className="text-fg-muted">{detailText(result.reason)}</li>}
          {result.published.map((p) => (
            <li key={`${p.port}-${p.proto}`} className="flex flex-wrap items-center gap-2">
              <Badge tone={p.ok ? "online" : "blood"}>{p.ok ? t("inside.published") : t("inside.notPublished")}</Badge>
              <span className="font-mono text-xs">{portLabel(p)}</span>
              {p.detail && <span className="text-fg-muted">{detailText(p.detail)}</span>}
            </li>
          ))}
          {result.answers.map((a) => {
            const channel = a.port
              ? t("inside.channelPort", { channel: t(`channel.${a.channel}`), port: a.port })
              : t(`channel.${a.channel}`);
            return (
              <li key={a.channel} className="flex flex-wrap items-center gap-2">
                <Badge tone={a.ok ? "online" : "blood"}>{a.ok ? t("inside.answers") : t("inside.silent")}</Badge>
                <span>
                  {a.ok
                    ? t("inside.answerOk", { channel, ms: a.latency_ms ?? 0 })
                    : t("inside.answerFail", { channel, error: detailText(a.error) })}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function OutsideCheck({ data }: { data: Reachability }) {
  const { t } = useTranslation("reachabilityPanel");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OutsideResult | null>(null);
  const [error, setError] = useState("");
  const { enabled, services, plan, cache_s } = data.outside;
  const names = Object.fromEntries(services.map((s) => [s.id, s.name]));

  const run = () => {
    setBusy(true);
    setError("");
    apiJson<OutsideResult>(`/servers/${data.key}/reachability/outside`, { method: "POST" })
      .then(setResult)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <section className="space-y-2">
      <h3 className="font-semibold text-fg">{t("outside.title")}</h3>
      {!enabled ? (
        <p className="text-fg-muted" data-testid="reachability-outside-off">
          {t("outside.off")}
        </p>
      ) : (
        <>
          <div className="rounded-md border border-line bg-surface-sunken/60 p-3" data-testid="reachability-disclosure">
            <div className="mb-1 font-semibold text-fg">{t("outside.sends.title")}</div>
            <ul className="list-disc space-y-1 pl-5 text-fg-muted">
              {services.map((s) => (
                <li key={s.id}>
                  {s.enabled
                    ? t("outside.sends.line", { name: s.name, url: s.url, what: t(`outside.sends.${s.sends}`) })
                    : t("outside.sends.off", { name: s.name })}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-fg-muted">{t("outside.sends.note", { seconds: cache_s })}</p>
          </div>
          <ul className="space-y-1" data-testid="reachability-plan">
            {plan.map((p) => (
              <li key={`${p.name}-${p.port}-${p.proto}`} className="flex flex-wrap gap-2">
                <span className="font-mono text-xs">{portLabel(p)}</span>
                <span className="text-fg-muted">
                  {p.service === "none"
                    ? t("outside.plan.none")
                    : p.state === "off"
                      ? t("outside.plan.off", { name: names[p.service] })
                      : t(`outside.plan.${p.service}`, { name: names[p.service] })}
                </span>
              </li>
            ))}
          </ul>
          <GhostButton onClick={run} disabled={busy}>
            {busy ? t("checking") : t("outside.run")}
          </GhostButton>
        </>
      )}
      {error && <Alert kind="error">{error}</Alert>}
      {result && (
        <ul className="space-y-1" data-testid="reachability-outside">
          {result.reason && <li className="text-fg-muted">{detailText(result.reason)}</li>}
          {result.ports.map((p) => (
            <li key={`${p.name}-${p.port}-${p.proto}`} className="flex flex-wrap items-center gap-2">
              <Badge tone={STATUS_TONE[p.status]}>{t(`status.${p.status}`)}</Badge>
              <span className="font-mono text-xs">{portLabel(p)}</span>
              <span className="text-fg-muted">{detailText(p.detail)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
