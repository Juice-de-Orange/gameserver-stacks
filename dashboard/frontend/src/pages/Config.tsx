import { FormEvent, useEffect, useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Eye, EyeOff, Info, Megaphone, Plus, SlidersHorizontal, Trash2 } from "lucide-react";
import { api, apiJson } from "../api";
import { useToast } from "../components/feedback";
import { useLiveConsole } from "../components/liveconsole";
import { useActiveServer } from "../components/ServerContext";
import { GameType, ServerInfo, hasCap } from "../lib/game";
import { Alert, Button, Card, EmptyState, Field, GhostButton, Input, SectionTitle, Select, Textarea, cx } from "../components/ui";
import { tb } from "../i18n";

interface CfgField {
  key: string;
  label: string;
  group: string;
  type: "bool" | "int" | "str";
  help: string;
  value: string;
}

const isTrue = (v: string) => ["true", "1", "yes", "on"].includes((v || "").toLowerCase());

/** Editor for list-valued config fields (e.g. Reforger game.admins / supportedPlatforms).
 * The value is a JSON array string — chips to remove, input + Enter to add. */
function ListField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation("config");
  const [draft, setDraft] = useState("");
  let items: string[] = [];
  try {
    const p = JSON.parse(value || "[]");
    items = Array.isArray(p) ? p.map(String) : [];
  } catch {
    items = [];
  }
  const commit = (next: string[]) => onChange(JSON.stringify(next));
  const add = () => {
    const v = draft.trim();
    if (v && !items.includes(v)) commit([...items, v]);
    setDraft("");
  };
  return (
    <div className="w-full max-w-[16rem] space-y-1.5">
      <div className="flex flex-wrap gap-1">
        {items.length === 0 && <span className="text-xs text-fg-dim">—</span>}
        {items.map((it) => (
          <span
            key={it}
            className="inline-flex items-center gap-1 rounded-md border border-line bg-surface-sunken px-2 py-0.5 text-xs text-fg"
          >
            <span className="font-mono">{it}</span>
            <button
              type="button"
              aria-label={t("list.remove", { item: it })}
              onClick={() => commit(items.filter((x) => x !== it))}
              className="text-fg-dim hover:text-danger-text"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-1">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          placeholder={t("list.addPlaceholder")}
          spellCheck={false}
          className="!min-h-8 flex-1 text-xs"
        />
        <GhostButton type="button" onClick={add} className="!min-h-8 px-2">
          +
        </GhostButton>
      </div>
    </div>
  );
}

// Keys only: translated text in a module-level constant would freeze the language at import.
const SANDBOX_HINTS: [string, string][] = [
  ["Zombies", "sandbox.hint.zombies"],
  ["DayLength", "sandbox.hint.dayLength"],
  ["XpMultiplier", "sandbox.hint.xpMultiplier"],
  ["ZombieLore.Speed", "sandbox.hint.speed"],
  ["ZombieLore.Strength / Toughness", "sandbox.hint.strength"],
  ["ZombieLore.Cognition", "sandbox.hint.cognition"],
  ["LootRespawn / SeenHoursPreventLootRespawn", "sandbox.hint.loot"],
  ["WaterShutoff / ElecShutoffModifier", "sandbox.hint.shutoff"],
];

/** The join password — visible, but not visible by accident.
 *
 * Unlike a `password` field this one holds a real value: every player knows the join password
 * anyway, and the operator needs to be able to look it up. The field still starts HIDDEN, so
 * that nobody who opens the page with a spectator next to them has it on screen unasked.
 *
 * An empty field really means "no password" here (not "unchanged" as with `password`), which
 * is why the placeholder says so.
 */
function JoinPasswordField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const { t } = useTranslation("config");
  const [visible, setVisible] = useState(false);
  return (
    <div className="flex items-center gap-1.5 sm:max-w-[14rem]">
      <Input
        type={visible ? "text" : "password"}
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("joinPassword.placeholder")}
        autoComplete="off"
      />
      <GhostButton
        type="button"
        aria-label={t(visible ? "joinPassword.hide" : "joinPassword.show", { label })}
        aria-pressed={visible}
        onClick={() => setVisible(!visible)}
        className="shrink-0 px-2"
      >
        {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </GhostButton>
    </div>
  );
}

// What this game's configuration really consists of — shown as the subtitle, so that it is clear
// which file is being edited. Keys, translated when rendering. A game without an entry gets a
// calm general sentence instead of a wrong file name.
const CONFIG_SOURCE: Partial<Record<GameType, string>> = {
  minecraft: "source.minecraft",
  vintagestory: "source.vintagestory",
  arma_reforger: "source.arma_reforger",
  factorio: "source.factorio",
  avorion: "source.avorion",
  valheim: "source.valheim",
};

/** Picks the editor: Project Zomboid gets the curated one, every other game the generic one. */
export default function Config() {
  const { t } = useTranslation("config");
  const server = useActiveServer();
  if (!server) return <div className="py-8 text-center text-fg-dim">{t("noServer")}</div>;
  // The React `key` is not a formality: the route does not remount on a param change, and
  // between two servers of the same kind the component stays the same type in the same place.
  // Without a forced remount it would carry its whole editor state (raw .ini, SandboxVars,
  // changed fields) over to the new server — a "Save" before the reload has finished would
  // write ONE server's file into the OTHER.
  if (server.game_type !== "pz") return <GenericConfig key={server.key} server={server} />;
  // The server is PASSED IN, not guessed: every path below is scoped to this server's key.
  return <PzConfig key={server.key} server={server} />;
}

/** "There is something unsaved here" — four editors on one page, two files underneath. */
function Unsaved({ show }: { show: boolean }) {
  const { t } = useTranslation("config");
  if (!show) return null;
  return <span className="text-xs text-warning-text">{t("unsaved")}</span>;
}

function PzConfig({ server }: { server: ServerInfo }) {
  const key = server.key;
  const { t } = useTranslation("config");
  const toast = useToast();
  const { emit } = useLiveConsole();
  const [fields, setFields] = useState<CfgField[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [orig, setOrig] = useState<Record<string, string>>({});
  const [envManaged, setEnvManaged] = useState<string[]>([]);
  // Each raw buffer has two companions: the server state loaded last (`…Orig`, tells whether
  // something unsaved is in it) and the file stamp from the GET (`…Revision`, sent back on save).
  // Without the stamp this editor writes the WHOLE file over everything that arrived since the
  // page was opened — including the user's own change in the GUI tab.
  const [raw, setRaw] = useState("");
  const [rawOrig, setRawOrig] = useState("");
  const [rawRevision, setRawRevision] = useState("");
  const [sandbox, setSandbox] = useState("");
  const [sandboxOrig, setSandboxOrig] = useState("");
  const [sandboxRevision, setSandboxRevision] = useState("");
  // The quick settings write this file too — see `savePresets`.
  const [presetRevision, setPresetRevision] = useState("");
  const [presets, setPresets] = useState<any[]>([]);
  const [pvals, setPvals] = useState<Record<string, string>>({});
  const [bcasts, setBcasts] = useState<any[]>([]);
  const [bform, setBform] = useState({ message: "", mode: "interval", interval_minutes: "60", time_hhmm: "12:00", days: "daily" });
  const [bcastBusy, setBcastBusy] = useState(false);
  const [tab, setTab] = useState<"gui" | "raw" | "sandbox" | "broadcasts">("gui");
  const [err, setErr] = useState("");

  // `loaded` enables the save buttons. Without it a failed load would leave the OLD content in
  // the text fields — only a red banner would show, and a click on "Save" would write that
  // content to the file while the display looks normal.
  const [loaded, setLoaded] = useState(false);

  async function load() {
    setLoaded(false);
    setErr("");
    try {
      const c = await apiJson<any>(`/servers/${key}/config/ini`);
      setFields(c.fields);
      const v: Record<string, string> = {};
      for (const f of c.fields) v[f.key] = f.value;
      setValues(v);
      setOrig({ ...v });
      setEnvManaged(c.env_managed);
      const r = await apiJson<any>(`/servers/${key}/config/raw`);
      setRaw(r.content);
      setRawOrig(r.content);
      setRawRevision(r.revision);
      const s = await apiJson<any>(`/servers/${key}/config/sandbox`);
      setSandbox(s.content);
      setSandboxOrig(s.content);
      setSandboxRevision(s.revision);
      const sp = await apiJson<any>(`/servers/${key}/config/sandbox-presets`);
      setPresets(sp.fields);
      setPresetRevision(sp.revision);
      const pv: Record<string, string> = {};
      for (const f of sp.fields) pv[f.path] = f.value;
      setPvals(pv);
      setLoaded(true);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  // Four editors, two files: "Settings" and "server.ini (raw)" both write the `.ini`, the quick
  // settings and the SandboxVars field both write the `.lua`. Saving in one makes the buffers of
  // the others stale, so the display is refreshed after every save — except where something
  // unsaved sits.
  const guiDirty = Object.keys(values).some((k) => values[k] !== orig[k]);
  const presetsDirty = presets.some((f) => pvals[f.path] !== f.value);
  const rawDirty = raw !== rawOrig;
  const sandboxDirty = sandbox !== sandboxOrig;

  /** Reload after a successful save — `keep.X = true` leaves buffer X alone. */
  async function refresh(keep: { gui: boolean; raw: boolean; sandbox: boolean; presets: boolean }) {
    try {
      if (!keep.gui) {
        const c = await apiJson<any>(`/servers/${key}/config/ini`);
        setFields(c.fields);
        setEnvManaged(c.env_managed);
        const v: Record<string, string> = {};
        for (const f of c.fields) v[f.key] = f.value;
        setValues(v);
        setOrig({ ...v });
      }
      if (!keep.raw) {
        const r = await apiJson<any>(`/servers/${key}/config/raw`);
        setRaw(r.content);
        setRawOrig(r.content);
        setRawRevision(r.revision);
      }
      if (!keep.sandbox) {
        const s = await apiJson<any>(`/servers/${key}/config/sandbox`);
        setSandbox(s.content);
        setSandboxOrig(s.content);
        setSandboxRevision(s.revision);
      }
      if (!keep.presets) {
        const sp = await apiJson<any>(`/servers/${key}/config/sandbox-presets`);
        setPresets(sp.fields);
        setPresetRevision(sp.revision);
        const pv: Record<string, string> = {};
        for (const f of sp.fields) pv[f.path] = f.value;
        setPvals(pv);
      }
    } catch (e: any) {
      // The stamp of a buffer that was not reloaded stays old — a save there ends in a 409
      // instead of a silent overwrite. That is the safe direction, but it has to be visible.
      setErr(t("error.reloadAfterSave", { error: e.message }));
    }
  }

  async function loadBcasts() {
    try {
      setBcasts((await apiJson<any>(`/servers/${key}/broadcasts`)).broadcasts);
    } catch {
      /* ignore */
    }
  }

  async function savePresets() {
    const diff: Record<string, string> = {};
    for (const f of presets) if (pvals[f.path] !== f.value) diff[f.path] = pvals[f.path];
    if (Object.keys(diff).length === 0) {
      toast.info(t("toast.nothingChanged"));
      return;
    }
    try {
      // The file stamp goes along: the quick settings write the same `.lua` as the large field
      // right below, and `write_values` rewrites it completely. Without `revision` everything that
      // arrived since loading would be lost silently.
      const r = await apiJson<any>(`/servers/${key}/config/sandbox-presets`, { method: "PUT", body: JSON.stringify({ values: diff, revision: presetRevision }) });
      toast.success(t(r.reloaded ? "toast.savedReloaded" : "toast.saved"));
      await refresh({ gui: guiDirty, raw: rawDirty, sandbox: sandboxDirty, presets: false });
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  // `key` MUST be in the dependencies: the paths are scoped, so without it the page would keep
  // showing the previous server's configuration after a switch — and "Save" would write the
  // displayed values into the NEW server's file.
  useEffect(() => {
    load();
    loadBcasts();
  }, [key]);

  const groups = useMemo(() => {
    const g: Record<string, CfgField[]> = {};
    for (const f of fields) (g[f.group] ||= []).push(f);
    return g;
  }, [fields]);

  async function saveGui() {
    const diff: Record<string, string> = {};
    for (const k of Object.keys(values)) if (values[k] !== orig[k]) diff[k] = values[k];
    if (Object.keys(diff).length === 0) {
      toast.info(t("toast.nothingChanged"));
      return;
    }
    try {
      const r = await apiJson<any>(`/servers/${key}/config/ini`, { method: "PUT", body: JSON.stringify({ values: diff }) });
      toast.success(t(r.reloaded ? "toast.savedReloaded" : "toast.saved"));
      setOrig({ ...values });   // fallback if `refresh` fails: the buttons are right anyway
      await refresh({ gui: false, raw: rawDirty, sandbox: sandboxDirty, presets: presetsDirty });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  /**
   * The two raw editors. They send the WHOLE file, so the file stamp from the GET goes along:
   * if somebody else has written in the meantime, the server answers 409 instead of silently
   * overwriting that change.
   */
  async function saveRaw(which: "raw" | "sandbox") {
    const isRaw = which === "raw";
    const path = `/servers/${key}/config/${isRaw ? "raw" : "sandbox"}`;
    const content = isRaw ? raw : sandbox;
    const revision = isRaw ? rawRevision : sandboxRevision;
    const label = isRaw ? "server.ini" : "SandboxVars";
    try {
      const r = await apiJson<any>(path, { method: "PUT", body: JSON.stringify({ content, revision }) });
      toast.success(t(r.reloaded ? "toast.fileSavedReloaded" : "toast.fileSaved", { file: label }));
      await refresh({
        gui: guiDirty,
        presets: presetsDirty,
        raw: isRaw ? false : rawDirty,
        sandbox: isRaw ? sandboxDirty : false,
      });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // --- Announcements (broadcasts) ---
  async function createBcast(e: FormEvent) {
    e.preventDefault();
    // Without the lock a double click creates the same announcement twice — it then goes out
    // twice to all players, and nothing tells the second from the first.
    if (bcastBusy) return;
    if (!bform.message.trim()) {
      toast.error(t("broadcast.messageMissing"));
      return;
    }
    const payload =
      bform.mode === "interval"
        ? { message: bform.message, interval_minutes: Number(bform.interval_minutes) || 0, enabled: true }
        : { message: bform.message, time_hhmm: bform.time_hhmm, days: bform.days, enabled: true };
    setBcastBusy(true);
    try {
      // apiJson throws on a 400 (e.g. interval 0 / invalid days) — otherwise "created" would be
      // reported and the message cleared although the backend rejected the announcement.
      await apiJson(`/servers/${key}/broadcasts`, { method: "POST", body: JSON.stringify(payload) });
      emit(t("broadcast.console.created", { message: bform.message }));
      toast.success(t("broadcast.created"));
      setBform({ ...bform, message: "" });
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBcastBusy(false);
    }
  }
  async function toggleBcast(b: any) {
    try {
      await apiJson(`/servers/${key}/broadcasts/${b.id}`, {
        method: "PUT",
        body: JSON.stringify({
          message: b.message,
          interval_minutes: b.interval_minutes,
          time_hhmm: b.time_hhmm,
          days: b.days,
          enabled: !b.enabled,
        }),
      });
      emit(t(b.enabled ? "broadcast.console.disabled" : "broadcast.console.enabled", { message: b.message }));
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function deleteBcast(id: number) {
    try {
      await apiJson(`/servers/${key}/broadcasts/${id}`, { method: "DELETE" });
      emit(t("broadcast.console.deleted"));
      toast.success(t("broadcast.deleted"));
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const tabCls = (t: string) =>
    cx(
      "inline-flex min-h-11 items-center rounded-lg px-3.5 py-1.5 text-sm font-medium transition sm:min-h-9",
      tab === t
        ? "bg-surface-card text-primary-text shadow-sm ring-1 ring-primary/25"
        : "text-fg-muted hover:bg-surface-sunken hover:text-fg",
    );

  return (
    <div className="space-y-5">
      <SectionTitle icon={SlidersHorizontal} sub={t("source.pz")}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      <div className="flex flex-wrap gap-1 rounded-md border border-line-soft bg-surface-card/50 p-1">
        <button className={tabCls("gui")} onClick={() => setTab("gui")}>
          {t("tab.settings")}
        </button>
        <button className={tabCls("raw")} onClick={() => setTab("raw")}>
          {t("tab.raw")}
        </button>
        <button className={tabCls("sandbox")} onClick={() => setTab("sandbox")}>
          SandboxVars
        </button>
        <button className={tabCls("broadcasts")} onClick={() => setTab("broadcasts")}>
          {t("tab.announcements")}
        </button>
      </div>

      {tab === "gui" && (
        <div className="space-y-4">
          {Object.entries(groups).map(([group, gf]) => (
            <Card key={group} className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tb(group)}</div>
              <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
                {gf.map((f) => (
                  <div key={f.key} className="space-y-1">
                    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between sm:gap-2">
                      <span className="text-sm text-fg" title={f.key}>
                        {tb(f.label)}
                      </span>
                      {f.type === "bool" ? (
                        <div className="flex shrink-0 self-start overflow-hidden rounded-lg border border-line sm:self-auto">
                          {["true", "false"].map((b) => (
                            <button
                              key={b}
                              aria-label={t(b === "true" ? "bool.onLabel" : "bool.offLabel", { label: tb(f.label) })}
                              onClick={() => setValues({ ...values, [f.key]: b })}
                              className={cx(
                                "min-h-9 px-4 text-sm font-medium transition",
                                isTrue(values[f.key]) === (b === "true")
                                  ? b === "true"
                                    ? "bg-primary-btn text-primary-fg"
                                    : "bg-danger text-white"
                                  : "bg-surface-raised text-fg-dim hover:text-fg",
                              )}
                            >
                              {t(b === "true" ? "bool.on" : "bool.off")}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <Input
                          aria-label={tb(f.label)}
                          type={f.type === "int" ? "number" : "text"}
                          value={values[f.key] ?? ""}
                          onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                          className={f.type === "int" ? "sm:max-w-[8rem]" : "sm:max-w-[16rem]"}
                        />
                      )}
                    </div>
                    {f.help && <p className="text-xs text-fg-dim">{tb(f.help)}</p>}
                  </div>
                ))}
              </div>
            </Card>
          ))}
          <div className="flex items-center gap-3">
            <Button onClick={saveGui} disabled={!loaded}>{t("common:action.save")}</Button>
            <Unsaved show={guiDirty} />
            <span className="text-xs text-fg-dim">
              {t("envManaged", { keys: envManaged.join(", ") })}
            </span>
          </div>
        </div>
      )}

      {tab === "raw" && (
        <Card className="space-y-3">
          <Textarea
            aria-label={t("raw.ariaLabel")}
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            spellCheck={false}
            className="h-96 font-mono text-xs"
          />
          <div className="flex items-center gap-3">
            <Button onClick={() => saveRaw("raw")} disabled={!loaded}>{t("common:action.save")}</Button>
            <Unsaved show={rawDirty} />
          </div>
        </Card>
      )}

      {tab === "sandbox" && (
        <div className="space-y-4">
          {presets.length > 0 && (
            <Card className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{t("sandbox.quickSettings")}</div>
              <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
                {presets.map((f) => (
                  <div key={f.path} className="space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm text-fg" title={f.path}>
                        {tb(f.label)}
                      </span>
                      <Input
                        aria-label={tb(f.label)}
                        type="number"
                        value={pvals[f.path] ?? ""}
                        onChange={(e) => setPvals({ ...pvals, [f.path]: e.target.value })}
                        className="max-w-[7rem]"
                      />
                    </div>
                    {f.help && <p className="text-xs text-fg-dim">{tb(f.help)}</p>}
                  </div>
                ))}
              </div>
              <div className="flex items-center gap-3">
                <Button onClick={savePresets} disabled={!loaded}>{t("common:action.save")}</Button>
                <Unsaved show={presetsDirty} />
              </div>
            </Card>
          )}
          <Card className="space-y-2 border-line bg-surface-card/40">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
              <Info className="h-4 w-4" /> {t("sandbox.cheatSheet")}
            </div>
            <div className="grid gap-1 text-xs sm:grid-cols-2">
              {SANDBOX_HINTS.map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <code className="shrink-0 font-mono text-success-text">{k}</code>
                  <span className="text-fg-dim">{t(v)}</span>
                </div>
              ))}
            </div>
            <p className="text-xs text-warning-text">
              {t("sandbox.syntaxWarning")}
            </p>
          </Card>
          <Card className="space-y-3">
            <Textarea
              aria-label={t("sandbox.ariaLabel")}
              value={sandbox}
              onChange={(e) => setSandbox(e.target.value)}
              spellCheck={false}
              className="h-96 font-mono text-xs"
            />
            <div className="flex items-center gap-3">
              <Button onClick={() => saveRaw("sandbox")} disabled={!loaded}>{t("common:action.save")}</Button>
              <Unsaved show={sandboxDirty} />
            </div>
          </Card>
        </div>
      )}

      {tab === "broadcasts" && (
        <div className="space-y-4">
          <Alert kind="info">
            <Trans t={t} i18nKey="broadcast.introPz" components={{ b: <b /> }} />
          </Alert>
          <Card>
            <form onSubmit={createBcast} className="flex flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-end">
              <Field label={t("broadcast.field.message")}>
                <Input
                  value={bform.message}
                  onChange={(e) => setBform({ ...bform, message: e.target.value })}
                  placeholder={t("broadcast.messagePlaceholder")}
                  className="sm:w-72"
                />
              </Field>
              <Field label={t("broadcast.field.mode")}>
                <Select value={bform.mode} onChange={(e) => setBform({ ...bform, mode: e.target.value })}>
                  <option value="interval">{t("broadcast.mode.interval")}</option>
                  <option value="time">{t("broadcast.mode.time")}</option>
                </Select>
              </Field>
              {bform.mode === "interval" ? (
                <Field label={t("broadcast.field.interval")}>
                  <Input
                    type="number"
                    value={bform.interval_minutes}
                    onChange={(e) => setBform({ ...bform, interval_minutes: e.target.value })}
                    className="max-w-[8rem]"
                  />
                </Field>
              ) : (
                <>
                  <Field label={t("broadcast.field.time")}>
                    <Input type="time" value={bform.time_hhmm} onChange={(e) => setBform({ ...bform, time_hhmm: e.target.value })} className="max-w-[8rem]" />
                  </Field>
                  <Field label={t("broadcast.field.days")} hint={t("broadcast.daysHint")}>
                    <Input value={bform.days} onChange={(e) => setBform({ ...bform, days: e.target.value })} className="max-w-[12rem]" />
                  </Field>
                </>
              )}
              <Button type="submit" disabled={bcastBusy}>
                <Plus className="h-4 w-4" /> {t("broadcast.create")}
              </Button>
            </form>
          </Card>

          <Card className="space-y-2">
            {bcasts.length === 0 && <EmptyState icon={Megaphone}>{t("broadcast.empty")}</EmptyState>}
            {bcasts.map((b) => (
              <div key={b.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line-soft bg-surface-card/50 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm text-fg">{b.message}</div>
                  <div className="text-xs text-fg-dim">
                    {b.interval_minutes ? t("broadcast.every", { minutes: b.interval_minutes }) : `${b.time_hhmm} · ${b.days}`}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => toggleBcast(b)}
                    className={cx(
                      "rounded-md border px-3 py-1 text-xs",
                      b.enabled ? "border-success-border bg-success-soft/60 text-success-text" : "border-line text-fg-dim",
                    )}
                  >
                    {t(b.enabled ? "broadcast.active" : "broadcast.off")}
                  </button>
                  <button onClick={() => deleteBcast(b.id)} aria-label={t("broadcast.delete")} className="text-fg-dim hover:text-danger-text">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))}
          </Card>
        </div>
      )}
    </div>
  );
}

// --- Generic editor for the other games (server.properties, serverconfig.json, …) ---
interface GField {
  key: string;
  label: string;
  kind: string;
  value: string;
  group: string;
  choices: string[];
  env_managed: boolean;
  help: string;
}

function GenericConfig({ server }: { server: ServerInfo }) {
  const key = server.key;
  const { t } = useTranslation("config");
  const toast = useToast();
  const { emit } = useLiveConsole();
  const [fields, setFields] = useState<GField[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [orig, setOrig] = useState<Record<string, string>>({});
  const [restart, setRestart] = useState(false);
  // What exactly is needed for the change to take effect: "live" | "restart" | "recreate".
  // `restart` alone cannot carry the difference — a recreate is not a restart.
  const [reload, setReload] = useState("");
  const [err, setErr] = useState("");
  // As in `PzConfig`: nothing is saved until loading has finished. On an error `load()` only sets
  // `err` and leaves `fields`/`values`/`orig` as they were — after a server switch those would be
  // the PREVIOUS server's values.
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<"gui" | "broadcasts">("gui");
  const [bcasts, setBcasts] = useState<any[]>([]);
  const [bform, setBform] = useState({ message: "", mode: "interval", interval_minutes: "60", time_hhmm: "12:00", days: "daily" });
  const [bcastBusy, setBcastBusy] = useState(false);
  // Announcements only if the game can broadcast to the server (Reforger cannot → tab hidden).
  const canAnnounce = hasCap(server, "announce");

  async function load() {
    setLoaded(false);
    try {
      const c = await apiJson<any>(`/servers/${key}/config`);
      setFields(c.fields);
      setRestart(!!c.restart_required);
      setReload(c.config_reload || "");
      const v: Record<string, string> = {};
      for (const f of c.fields) v[f.key] = f.value;
      setValues(v);
      setOrig({ ...v });
      setLoaded(true);
    } catch (e: any) {
      setErr(e.message);
    }
  }
  async function loadBcasts() {
    try {
      setBcasts((await apiJson<any>(`/servers/${key}/broadcasts`)).broadcasts);
    } catch {
      /* ignore */
    }
  }
  useEffect(() => {
    load();
    if (canAnnounce) loadBcasts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const groups = useMemo(() => {
    const g: Record<string, GField[]> = {};
    for (const f of fields) (g[f.group] ||= []).push(f);
    return g;
  }, [fields]);

  async function save() {
    const diff: Record<string, string> = {};
    for (const k of Object.keys(values)) if (values[k] !== orig[k]) diff[k] = values[k];
    if (Object.keys(diff).length === 0) {
      toast.info(t("toast.nothingChanged"));
      return;
    }
    try {
      const r = await apiJson<any>(`/servers/${key}/config`, { method: "PUT", body: JSON.stringify({ values: diff }) });
      // The response says what is REALLY needed — a single field can be more expensive than the
      // rest (Valheim and the PZ join password live in the container env). Reporting "restart"
      // where a recreate is needed would be a success message about something with no effect.
      setReload(r.config_reload || "");
      toast.success(
        t(
          r.config_reload === "recreate"
            ? "toast.savedRecreate"
            : r.restart_required
              ? "toast.savedRestart"
              : "toast.saved",
        ),
      );
      setOrig({ ...values });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function createBcast(e: FormEvent) {
    e.preventDefault();
    // Without the lock a double click creates the same announcement twice — it then goes out
    // twice to all players, and nothing tells the second from the first.
    if (bcastBusy) return;
    if (!bform.message.trim()) {
      toast.error(t("broadcast.messageMissing"));
      return;
    }
    const payload =
      bform.mode === "interval"
        ? { message: bform.message, interval_minutes: Number(bform.interval_minutes) || 0, enabled: true }
        : { message: bform.message, time_hhmm: bform.time_hhmm, days: bform.days, enabled: true };
    setBcastBusy(true);
    try {
      await apiJson(`/servers/${key}/broadcasts`, { method: "POST", body: JSON.stringify(payload) });
      emit(t("broadcast.console.created", { message: bform.message }));
      toast.success(t("broadcast.created"));
      setBform({ ...bform, message: "" });
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBcastBusy(false);
    }
  }
  async function toggleBcast(b: any) {
    try {
      await apiJson(`/servers/${key}/broadcasts/${b.id}`, {
        method: "PUT",
        body: JSON.stringify({ message: b.message, interval_minutes: b.interval_minutes, time_hhmm: b.time_hhmm, days: b.days, enabled: !b.enabled }),
      });
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function deleteBcast(id: number) {
    try {
      await apiJson(`/servers/${key}/broadcasts/${id}`, { method: "DELETE" });
      toast.success(t("broadcast.deleted"));
      loadBcasts();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const tabCls = (t: string) =>
    cx("rounded-lg px-3 py-1.5 text-sm font-medium transition", tab === t ? "bg-surface-sunken text-fg" : "text-fg-muted hover:text-fg");

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={SlidersHorizontal}
        sub={t(CONFIG_SOURCE[server.game_type] ?? "source.fallback")}
      >
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}

      {/* Tab bar only when there are announcements — otherwise only "Settings" remains (Reforger). */}
      {canAnnounce && (
        <div className="flex flex-wrap gap-1 rounded-xl border border-line bg-surface-card p-1">
          <button className={tabCls("gui")} onClick={() => setTab("gui")}>
            {t("tab.settings")}
          </button>
          <button className={tabCls("broadcasts")} onClick={() => setTab("broadcasts")}>
            {t("tab.announcements")}
          </button>
        </div>
      )}

      {tab === "gui" && (
        <div className="space-y-4">
          {restart && (
            <Alert kind="info">
              {t(reload === "recreate" ? "restartNotice.recreate" : "restartNotice.restart")}
            </Alert>
          )}
          {fields.length === 0 && (
            <EmptyState icon={SlidersHorizontal}>{t("empty")}</EmptyState>
          )}
          {Object.entries(groups).map(([group, gf]) => (
            <Card key={group} className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tb(group)}</div>
              <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
                {gf.map((f) => (
                  <div key={f.key} className="space-y-1">
                    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between sm:gap-2">
                      <span className="min-w-0 text-sm text-fg sm:flex-1 sm:truncate" title={f.key}>
                        {tb(f.label)}
                      </span>
                      {f.env_managed ? (
                        <span className="shrink-0 self-start text-xs text-fg-dim sm:self-auto">{t("systemManaged")}</span>
                      ) : f.kind === "list" ? (
                        <ListField
                          value={values[f.key] ?? "[]"}
                          onChange={(v) => setValues({ ...values, [f.key]: v })}
                        />
                      ) : f.kind === "join_password" ? (
                        <JoinPasswordField
                          label={tb(f.label)}
                          value={values[f.key] ?? ""}
                          onChange={(v) => setValues({ ...values, [f.key]: v })}
                        />
                      ) : f.kind === "password" ? (
                        <Input
                          type="password"
                          aria-label={tb(f.label)}
                          value={values[f.key] ?? ""}
                          onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                          placeholder={t("password.placeholder")}
                          autoComplete="new-password"
                          className="sm:max-w-[14rem]"
                        />
                      ) : f.choices && f.choices.length > 0 ? (
                        <Select
                          aria-label={tb(f.label)}
                          value={values[f.key] ?? ""}
                          onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                          className="w-full sm:w-auto sm:max-w-[12rem]"
                        >
                          {f.choices.map((c) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </Select>
                      ) : f.kind === "bool" ? (
                        <div className="flex shrink-0 self-start overflow-hidden rounded-lg border border-line sm:self-auto">
                          {["true", "false"].map((b) => (
                            <button
                              key={b}
                              type="button"
                              aria-label={t(b === "true" ? "bool.onLabel" : "bool.offLabel", { label: tb(f.label) })}
                              onClick={() => setValues({ ...values, [f.key]: b })}
                              className={cx(
                                "min-h-9 px-4 text-sm font-medium transition",
                                isTrue(values[f.key]) === (b === "true")
                                  ? b === "true"
                                    ? "bg-primary-btn text-primary-fg"
                                    : "bg-danger text-white"
                                  : "bg-surface-raised text-fg-dim hover:text-fg",
                              )}
                            >
                              {t(b === "true" ? "bool.on" : "bool.off")}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <Input
                          aria-label={tb(f.label)}
                          type={f.kind === "int" || f.kind === "float" ? "number" : "text"}
                          value={values[f.key] ?? ""}
                          onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                          className="sm:max-w-[14rem]"
                        />
                      )}
                    </div>
                    {f.help && <p className="text-xs text-fg-dim">{tb(f.help)}</p>}
                  </div>
                ))}
              </div>
            </Card>
          ))}
          {fields.length > 0 && <Button onClick={save} disabled={!loaded}>{t("common:action.save")}</Button>}
        </div>
      )}

      {tab === "broadcasts" && (
        <div className="space-y-4">
          <Alert kind="info">{t("broadcast.intro")}</Alert>
          <Card>
            <form onSubmit={createBcast} className="flex flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-end">
              <Field label={t("broadcast.field.message")}>
                <Input
                  value={bform.message}
                  onChange={(e) => setBform({ ...bform, message: e.target.value })}
                  placeholder={t("broadcast.messagePlaceholder")}
                  className="sm:w-72"
                />
              </Field>
              <Field label={t("broadcast.field.mode")}>
                <Select value={bform.mode} onChange={(e) => setBform({ ...bform, mode: e.target.value })}>
                  <option value="interval">{t("broadcast.mode.interval")}</option>
                  <option value="time">{t("broadcast.mode.time")}</option>
                </Select>
              </Field>
              {bform.mode === "interval" ? (
                <Field label={t("broadcast.field.interval")}>
                  <Input
                    type="number"
                    value={bform.interval_minutes}
                    onChange={(e) => setBform({ ...bform, interval_minutes: e.target.value })}
                    className="max-w-[8rem]"
                  />
                </Field>
              ) : (
                <>
                  <Field label={t("broadcast.field.time")}>
                    <Input type="time" value={bform.time_hhmm} onChange={(e) => setBform({ ...bform, time_hhmm: e.target.value })} className="max-w-[8rem]" />
                  </Field>
                  <Field label={t("broadcast.field.days")} hint={t("broadcast.daysHint")}>
                    <Input value={bform.days} onChange={(e) => setBform({ ...bform, days: e.target.value })} className="max-w-[12rem]" />
                  </Field>
                </>
              )}
              <Button type="submit" disabled={bcastBusy}>
                <Plus className="h-4 w-4" /> {t("broadcast.create")}
              </Button>
            </form>
          </Card>

          <Card className="space-y-2">
            {bcasts.length === 0 && <EmptyState icon={Megaphone}>{t("broadcast.empty")}</EmptyState>}
            {bcasts.map((b) => (
              <div key={b.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line bg-surface-sunken/50 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm text-fg">{b.message}</div>
                  <div className="text-xs text-fg-dim">
                    {b.interval_minutes ? t("broadcast.every", { minutes: b.interval_minutes }) : `${b.time_hhmm} · ${b.days}`}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => toggleBcast(b)}
                    className={cx(
                      "rounded-lg border px-3 py-1 text-xs",
                      b.enabled ? "border-success/40 bg-success-soft text-success-text" : "border-line text-fg-dim",
                    )}
                  >
                    {t(b.enabled ? "broadcast.active" : "broadcast.off")}
                  </button>
                  <button onClick={() => deleteBcast(b.id)} aria-label={t("broadcast.delete")} className="text-fg-dim hover:text-danger-text">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))}
          </Card>
        </div>
      )}
    </div>
  );
}
