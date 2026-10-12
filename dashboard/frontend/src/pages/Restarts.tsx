import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { CalendarClock, Plus, Trash2 } from "lucide-react";
import { api, apiJson } from "../api";
import { useConfirm, useToast } from "../components/feedback";
import { useLiveConsole } from "../components/liveconsole";
import { useActiveServer } from "../components/ServerContext";
import { tb } from "../i18n";
import { Alert, Badge, Button, Card, EmptyState, Field, GhostButton, Input, SectionTitle, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";

export default function Restarts() {
  const { t } = useTranslation("restarts");
  const toast = useToast();
  const confirm = useConfirm();
  const { emit } = useLiveConsole();
  const server = useActiveServer();
  const key = server?.key;
  const [list, setList] = useState<any[]>([]);
  const [next, setNext] = useState<string | null>(null);
  // Whether a warning arrives at all is up to the GAME: Valheim, Avorion and Arma Reforger have
  // no way of telling all players something. The lead-time field is locked there.
  const [warn, setWarn] = useState<{ possible: boolean; reason: string }>({ possible: true, reason: "" });
  // **The zone the entered time applies in.** The scheduler runs in UTC by default, so "05:00"
  // may be a different local time. Whoever is not told looks for the fault in the server.
  const [zone, setZone] = useState("");
  const [form, setForm] = useState({ time_hhmm: "05:00", days: "daily", warn_minutes: "5,1", backup_first: false });
  // Without the lock a double click creates the schedule twice — the server then restarts twice
  // in a row, and the second warning runs into a restart already in progress.
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function load() {
    if (!key) return;
    try {
      const r = await apiJson<any>(`/servers/${key}/schedules`);
      setList(r.schedules);
      setNext(r.next_restart);
      // Older responses lack the field — then it stays "possible" (the normal case).
      setWarn({ possible: r.warn_possible !== false, reason: r.warn_reason || "" });
      setZone(String(r.timezone || ""));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!key || busy) return;
    // An empty field is almost always an oversight, not a decision against the warning: the
    // server then goes down without any announcement. The backend accepts it (an empty list is
    // valid), so the intent has to be stated here — but ONLY where a warning would arrive at
    // all. Where the game cannot announce, the empty field is the only right answer.
    if (warn.possible && !form.warn_minutes.trim()) {
      const ok = await confirm({
        title: t("noWarning.title"),
        body: t("noWarning.body"),
        danger: true,
        confirmLabel: t("noWarning.confirm"),
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      // apiJson throws on a 400 (invalid warning minutes/days) — otherwise "created" would be
      // reported and the form reset although no schedule exists.
      await apiJson(`/servers/${key}/schedules`, { method: "POST", body: JSON.stringify({ ...form, enabled: true }) });
      emit(t("create.consoleLine", { time: form.time_hhmm, days: form.days }));
      toast.success(t("create.done"));
      setForm({ time_hhmm: "05:00", days: "daily", warn_minutes: "5,1", backup_first: false });
      load();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function toggle(s: any) {
    try {
      await apiJson(`/servers/${key}/schedules/${s.id}`, { method: "PUT", body: JSON.stringify({ ...s, enabled: !s.enabled }) });
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function remove(s: any) {
    // Ask first: on a phone the bin icon sits right next to the active/off switch, and there
    // is no way back except creating the schedule again.
    const ok = await confirm({
      title: t("delete.title", { time: s.time_hhmm, days: s.days }),
      body: t("delete.body"),
      danger: true,
      confirmLabel: t("common:action.delete"),
    });
    if (!ok) return;
    const id = s.id;
    try {
      await apiJson(`/servers/${key}/schedules/${id}`, { method: "DELETE" });
      toast.success(t("delete.done"));
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={CalendarClock}
        sub={next ? t("next", { when: fmtDateTime(next) }) : t("nextNone")}
      >
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      <Card>
        <form onSubmit={create} className="flex flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-end">
          <Field label={t("form.time")} hint={zone ? t("form.timezone", { zone }) : undefined}>
            <Input type="time" value={form.time_hhmm} onChange={(e) => setForm({ ...form, time_hhmm: e.target.value })} className="max-w-[8rem]" />
          </Field>
          <Field label={t("form.days")} hint={t("form.daysHint")}>
            <Input value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} className="max-w-[12rem]" />
          </Field>
          <Field
            label={t("form.warning")}
            hint={warn.possible ? t("form.warningHint") : t("form.warningImpossible")}
          >
            <Input
              value={warn.possible ? form.warn_minutes : ""}
              disabled={!warn.possible}
              title={tb(warn.reason)}
              placeholder={warn.possible ? "" : "—"}
              onChange={(e) => setForm({ ...form, warn_minutes: e.target.value })}
              className="max-w-[8rem] disabled:cursor-not-allowed disabled:opacity-50"
            />
          </Field>
          <label className="flex items-center gap-2 pb-2 text-sm text-fg-muted">
            <input type="checkbox" checked={form.backup_first} onChange={(e) => setForm({ ...form, backup_first: e.target.checked })} />
            {t("form.backupFirst")}
          </label>
          <Button type="submit" disabled={busy}>
            <Plus className="h-4 w-4" /> {t("create.label")}
          </Button>
        </form>
      </Card>

      <Card className="space-y-2">
        {list.length === 0 && <EmptyState icon={CalendarClock}>{t("empty")}</EmptyState>}
        {list.map((s) => (
          <div key={s.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line-soft bg-surface-card/50 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono text-lg text-fg">{s.time_hhmm}</span>
              <Badge tone="info">{s.days}</Badge>
              {warn.possible ? (
                <span className="text-fg-dim">{t("list.warning", { minutes: s.warn_minutes })}</span>
              ) : (
                <span className="cursor-help text-warning-text" title={tb(warn.reason)}>
                  {t("list.noWarning")}
                </span>
              )}
              {s.backup_first && <Badge tone="toxic">{t("form.backupFirst")}</Badge>}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => toggle(s)}
                className={cx(
                  "rounded-md border px-3 py-1 text-xs",
                  s.enabled ? "border-success-border bg-success-soft/60 text-success-text" : "border-line text-fg-dim",
                )}
              >
                {s.enabled ? t("list.active") : t("list.off")}
              </button>
              <button onClick={() => remove(s)} className="text-fg-dim hover:text-danger-text" title={t("common:action.delete")}>
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}
