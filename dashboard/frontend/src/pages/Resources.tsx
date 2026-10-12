// Resources page: RAM/CPU allocation per server + shared budget + idle auto-stop.
// Uses the endpoints /api/budget, /api/budget/usage and /api/servers/:key/resources|idle.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Save, Sliders } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { useAuth } from "../auth";
import { useActiveServer } from "../components/ServerContext";
import { useToast } from "../components/feedback";
import { Alert, Button, Card, Field, Input, ResourceBar, SectionTitle, Slider, Toggle } from "../components/ui";
import { fmtMb, fmtSpan } from "../lib/format";

interface ResState {
  ram_mb: number;
  cpu_cores: number;
  heap_max_mb: number | null;
  heap_min_mb: number | null;
  // From THIS response, not from the registry copy in `ServerContext`: that one is not
  // refreshed after a save, so `load()` would revert the value that was just set.
  idle_autostop_enabled: boolean;
  idle_timeout_minutes: number | null;
  live: { running?: boolean | null; cgroup_memory?: number; rss_bytes?: number };
}
interface BudgetUsage {
  total_ram_mb: number;
  used_ram_mb: number;
  free_ram_mb: number | null;
  running: { key: string; effective_ram_mb: number; uncapped: boolean }[];
  // Running servers whose usage cannot be measured right now: `used_ram_mb` is a lower bound.
  demand_unknown?: string[];
}

export default function Resources() {
  const { t } = useTranslation("resources");
  const server = useActiveServer();
  const toast = useToast();
  const { can } = useAuth();
  const key = server?.key;
  const [res, setRes] = useState<ResState | null>(null);
  const [budget, setBudget] = useState<BudgetUsage | null>(null);
  const [ram, setRam] = useState(0);
  const [cpu, setCpu] = useState(0);
  const [cap, setCap] = useState(0); // shared upper limit (editable by admins)
  const [capBusy, setCapBusy] = useState(false);
  const [idle, setIdle] = useState(false);
  const [idleDays, setIdleDays] = useState("");           // "" = global default
  const [idleDaysSaved, setIdleDaysSaved] = useState("");
  const [idleBusy, setIdleBusy] = useState(false);
  const [defaultIdleMin, setDefaultIdleMin] = useState<number | null>(null);
  // The headroom on top of the heap ceiling is a SETTING (`mem_headroom_pct`), not the constant
  // 1.25. `validate_allocation` uses the stored value, so the hint must as well — otherwise the
  // backend rejects an allocation the hint next to it declared sufficient.
  const [headroomPct, setHeadroomPct] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    if (!key || !server) return;
    try {
      const r = await apiJson<ResState>(`/servers/${key}/resources`);
      setRes(r);
      setRam(r.ram_mb);
      setCpu(r.cpu_cores);
      setIdle(r.idle_autostop_enabled);
      const days = r.idle_timeout_minutes == null ? "" : String(Math.round(r.idle_timeout_minutes / 1440));
      setIdleDays(days);
      setIdleDaysSaved(days);
      const b = await apiJson<BudgetUsage>(`/budget/usage`);
      setBudget(b);
      setCap(b.total_ram_mb);
      // The shared default is only in /budget, not in /budget/usage — without it the hint would
      // say "default" without a number, and the user would not know what they reset to.
      const bd = await apiJson<{ default_idle_timeout_minutes: number; mem_headroom_pct: number }>(`/budget`);
      setDefaultIdleMin(bd.default_idle_timeout_minutes);
      setHeadroomPct(bd.mem_headroom_pct);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  useEffect(() => {
    // Drop the old state on a server switch — otherwise the sliders keep server A's ram/cpu
    // values and a click on "Apply" would write them to B (or 0/0 = unlimited before the first
    // load). The apply button stays disabled until a load succeeded (disabled=!res).
    setRes(null);
    setBudget(null);
    setErr(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (!server) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;

  async function save() {
    if (!key) return;
    setBusy(true);
    try {
      const r = await api(`/servers/${key}/resources`, {
        method: "PUT",
        body: JSON.stringify({ ram_mb: ram, cpu_cores: cpu }),
      });
      if (!r.ok) throw new Error(await errText(r, t("error.apply")));
      const d = await r.json();
      toast.success(
        d.applied === "live"
          ? t("apply.live")
          : d.recreate_required
            ? t("apply.restartNeeded")
            : t("saved"),
      );
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function saveBudget() {
    setCapBusy(true);
    try {
      const r = await api(`/budget`, { method: "PUT", body: JSON.stringify({ total_ram_mb: cap }) });
      if (!r.ok) throw new Error(await errText(r, t("error.save")));
      toast.success(cap > 0 ? t("budget.limitSet", { limit: fmtMb(cap) }) : t("budget.limitRemoved"));
      await load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setCapBusy(false);
    }
  }

  async function saveIdle(v: boolean) {
    if (!key) return;
    setIdle(v);
    try {
      // Check res.ok: api() does NOT throw on HTTP errors — without the check the toggle would
      // show "Saved." on a 4xx/5xx and stay in the wrong state.
      const r = await api(`/servers/${key}/idle`, { method: "PUT", body: JSON.stringify({ idle_autostop_enabled: v }) });
      if (!r.ok) throw new Error(await errText(r, t("error.save")));
      toast.success(t("saved"));
    } catch (e) {
      toast.error((e as Error).message);
      setIdle(!v);
    }
  }

  // Entered in DAYS, because the value in minutes (28800) tells nobody anything.
  async function saveIdleTimeout() {
    if (!key) return;
    const raw = idleDays.trim();
    const days = raw === "" ? null : Number(raw);
    if (days !== null && (!Number.isFinite(days) || days < 1)) {
      toast.error(t("idle.invalid"));
      return;
    }
    setIdleBusy(true);
    try {
      const r = await api(`/servers/${key}/idle`, {
        method: "PUT",
        body: JSON.stringify({ idle_timeout_minutes: days === null ? null : Math.round(days * 1440) }),
      });
      if (!r.ok) throw new Error(await errText(r, t("error.save")));
      setIdleDaysSaved(raw);
      toast.success(
        days === null ? t("idle.reset", { default: fmtSpan(defaultIdleMin) })
                      : t("idle.set", { count: days }),
      );
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setIdleBusy(false);
    }
  }

  const total = budget?.total_ram_mb ?? 0;
  const used = budget?.used_ram_mb ?? 0;
  const factor = 1 + (headroomPct ?? 25) / 100;
  const heapFloor = server.heap_max_mb ? Math.ceil(server.heap_max_mb * factor) : null;

  return (
    <div className="space-y-5">
      <SectionTitle icon={Sliders} sub={server.display_name}>
        {t("title")}
      </SectionTitle>
      {err && <Alert kind="error">{err}</Alert>}

      <Card className="space-y-3">
        <div className="text-xs uppercase tracking-wide text-fg-muted">{t("budget.title")}</div>
        <ResourceBar used={used} total={total} label={t("budget.used")} />
        {!!budget?.demand_unknown?.length && (
          <div className="text-xs text-warning-text">
            {t("budget.demandUnknown", { servers: budget.demand_unknown.join(", ") })}
          </div>
        )}
        {total <= 0 && (
          <div className="text-xs text-fg-dim">
            {t("budget.advisory")}
          </div>
        )}
        {can("can_manage_resources") && (
          <div className="space-y-3 border-t border-line pt-3">
            <Slider
              label={t("budget.limit")}
              value={cap}
              min={0}
              max={65536}
              step={1024}
              unit=" MB"
              onChange={setCap}
              hint={t("budget.limitHint")}
            />
            <Button disabled={capBusy || !budget || cap === total} onClick={saveBudget}>
              <Save className="h-4 w-4" /> {t("budget.setLimit")}
            </Button>
          </div>
        )}
      </Card>

      <Card className="space-y-4">
        <div className="text-xs uppercase tracking-wide text-fg-muted">{t("allocation.title", { name: server.display_name })}</div>
        <Slider
          label={t("allocation.ram")}
          value={ram}
          min={0}
          max={32768}
          step={512}
          unit=" MB"
          onChange={setRam}
          hint={
            heapFloor
              ? t("allocation.ramHint", { percent: headroomPct ?? 25, floor: heapFloor })
              : undefined
          }
        />
        <Slider
          label={t("allocation.cpu")}
          value={cpu}
          min={0}
          max={8}
          step={0.5}
          unit={` ${t("allocation.cores")}`}
          onChange={setCpu}
        />
        {res?.live?.rss_bytes ? (
          <div className="text-xs text-fg-dim">
            {t("allocation.live", { mb: Math.round(res.live.rss_bytes / 1024 / 1024) })}
          </div>
        ) : null}
        <Button disabled={busy || !res} onClick={save}>
          <Save className="h-4 w-4" /> {t("apply.label")}
        </Button>
      </Card>

      <Card className="space-y-3">
        <Toggle checked={idle} onChange={saveIdle} label={t("idle.title")} />
        <div className="text-xs text-fg-dim">
          {t("idle.intro")}
        </div>
        {idle && can("can_manage_resources") && (
          <div className="flex flex-wrap items-end gap-3 border-t border-line pt-3">
            <Field
              label={t("idle.stopAfter")}
              hint={t("idle.defaultHint", { default: fmtSpan(defaultIdleMin) })}
            >
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  min={1}
                  step={1}
                  className="w-24"
                  value={idleDays}
                  placeholder={String(Math.round((defaultIdleMin ?? 0) / 1440) || "")}
                  onChange={(e) => setIdleDays(e.target.value)}
                />
                <span className="text-sm text-fg-dim">{t("idle.days")}</span>
              </div>
            </Field>
            <Button disabled={idleBusy || idleDays === idleDaysSaved} onClick={saveIdleTimeout}>
              <Save className="h-4 w-4" /> {t("idle.setLabel")}
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
