import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Ban, Gavel, ShieldX, UserMinus, UserPlus } from "lucide-react";
import { apiJson, noteText } from "../api";
import { useActiveServer } from "../components/ServerContext";
import { useConfirm, useToast } from "../components/feedback";
import i18n, { tb } from "../i18n";
import { Alert, Badge, Button, Card, DataTable, EmptyState, GhostButton, Input, SectionTitle, Select } from "../components/ui";

interface WlUser {
  username: string;
  accesslevel?: string;
  banned?: boolean;
  steamid?: string;
}
interface Spec {
  kick: boolean;
  ban: boolean;
  ban_ip: boolean;
  unban: boolean;
  access_levels: string[];
  whitelist: boolean;
  /** Why a sub-action is missing (or what to mind for one that exists). */
  reasons?: Record<string, string>;
}

/**
 * Principle from `lib/features.ts`: a role hides, a game capability greys out.
 *
 * What is missing here are **subsets of an existing** capability, and hiding those reads as
 * "does not exist" where the truth may be "unproven whether the RCON mod can do it". So the
 * control stays visible, greyed out, with the reason the driver supplies
 * (`ModerationSpec.reasons`).
 */
function lockReason(
  spec: Spec | null,
  field: string,
  can: boolean | undefined,
  error?: string,
): string | undefined {
  if (can) return undefined;
  // "Loading" must not stay when the fetch has failed — the user would wait for something that
  // is not coming any more.
  if (!spec) return error ? i18n.t("moderation:lock.loadFailed", { error }) : i18n.t("moderation:lock.loading");
  return tb(spec.reasons?.[field]) || i18n.t("moderation:lock.noReason");
}

/** Note on an action that EXISTS (e.g. "target must be online"). */
function hint(spec: Spec | null, field: string): string | undefined {
  return tb(spec?.reasons?.[field]) ?? undefined;
}

export default function Moderation() {
  const { t } = useTranslation("moderation");
  const server = useActiveServer();
  const key = server?.key;
  const confirm = useConfirm();
  const toast = useToast();

  const [over, setOver] = useState<any>(null);
  const [spec, setSpec] = useState<Spec | null>(null);
  const [manual, setManual] = useState("");
  const [level, setLevel] = useState("");
  const [reason, setReason] = useState("");
  // `note`: a fixed sentence (Valheim) or a coded one with the current mode in it (Avorion).
  const [wl, setWl] = useState<{ available: boolean; users: WlUser[]; note?: unknown }>({ available: false, users: [] });
  const [err, setErr] = useState("");

  const online: string[] = over?.players?.names || [];
  const levels = spec?.access_levels || [];

  async function loadWl() {
    if (!key || !spec?.whitelist) return;
    try {
      setWl(await apiJson(`/servers/${key}/moderation/whitelist`));
    } catch {
      /* the whitelist is optional (file/DB may not exist yet) */
    }
  }
  async function loadOver() {
    if (!key) return;
    try {
      setOver(await apiJson<any>(`/servers/${key}/overview`));
    } catch {
      /* offline */
    }
  }
  useEffect(() => {
    if (!key) return;
    setErr("");
    // Drop the old spec, otherwise switching servers shows the previous game's capabilities for
    // a moment — e.g. "Unban" and "Whitelist" as available where they are not.
    setSpec(null);
    apiJson<Spec>(`/servers/${key}/moderation/spec`)
      .then((s) => {
        setSpec(s);
        // NO preselection. Picking the first level that is not "none" hits "admin" for Project
        // Zomboid — the selector would sit on the most powerful level from the first load, and
        // "Set level" would hand out in-game admin rights without anyone touching the selector.
        setLevel("");
      })
      .catch((e) => setErr(e.message));
    loadOver();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Load the whitelist only once the spec is known (and only if the game has one).
  useEffect(() => {
    loadWl();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, key]);

  /**
   * `action` describes what was SENT — not what happened.
   *
   * The backend returns `{ok: true, output: <raw RCON answer>}` for EVERY round trip that did
   * not throw; only an `RconError` (server down) becomes a 502. Whether the game carried the
   * action out is in `output` alone — whatever the GAME rejects ("That player does not exist")
   * comes back as 200. Nobody can classify the answer, the games word it freely. So it is SHOWN
   * instead of interpreted.
   */
  async function call(path: string, body: any, action: string) {
    if (!key) return;
    try {
      const r = await apiJson<{ ok: boolean; output?: string }>(`/servers/${key}${path}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const answer = (r.output || "").trim();
      if (answer) toast.info(t("result.answered", { action, answer }));
      else toast.success(t("result.accepted", { action }));
      loadWl();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  function kick(username: string) {
    call("/moderation/kick", { username, reason }, t("sent.kick", { name: username }));
  }
  async function ban(username: string, ip = false) {
    const okc = await confirm({
      title: t("ban.title", { name: username }),
      body: ip ? t("ban.bodyIp") : t("ban.body"),
      danger: true,
      confirmLabel: t("ban.label"),
    });
    if (!okc) return;
    call("/moderation/ban", { username, reason, ip }, t("sent.ban", { name: username }));
  }
  async function access(username: string, lvl: string) {
    const okc = await confirm({
      title: t("level.title", { name: username, level: lvl }),
      body:
        lvl === "none"
          ? t("level.bodyNone", { name: username })
          : t("level.body", { name: username, level: lvl }),
      danger: lvl !== "none",
      confirmLabel: t("level.set"),
    });
    if (!okc) return;
    call("/moderation/access", { username, level: lvl }, t("sent.level", { name: username, level: lvl }));
  }

  /**
   * Unbanning and the whitelist are access decisions, not display options — and there is no
   * way back except the opposite action. They work on the freely typed name field right next to
   * "Ban"; on a phone the row wraps, and "Whitelist +" and "Whitelist −" differ by one character.
   * Hence the dialog.
   */
  async function confirmed(title: string, text: string, label: string, fn: () => void) {
    if (await confirm({ title, body: text, danger: true, confirmLabel: label })) fn();
  }

  // The subtitle lists only what this server REALLY can do — the locked controls are below,
  // greyed out with their reason; they do not belong in the list of capabilities.
  const subParts = [
    spec?.kick && t("cap.kick"),
    spec?.ban && t("cap.ban"),
    spec?.unban && t("cap.unban"),
    spec?.access_levels?.length && t("cap.accessLevels"),
    spec?.whitelist && t("cap.whitelist"),
  ].filter(Boolean);

  return (
    <div className="space-y-5">
      <SectionTitle icon={Gavel} sub={subParts.join(" · ") || t("title")}>
        {t("title")}
      </SectionTitle>
      {err && <Alert>{err}</Alert>}
      {over && !over?.container?.running && (
        <Alert kind="warn">{t("alert.offline")}</Alert>
      )}

      <Card className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          {t("online.title", { n: online.length })}
        </div>
        {online.length === 0 && <EmptyState>{t("online.empty")}</EmptyState>}
        {online.map((p) => (
          <div key={p} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line bg-surface-sunken/50 px-3 py-2">
            <span className="font-medium text-fg">{p}</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <Select
                defaultValue=""
                onChange={(e) => e.target.value && access(p, e.target.value)}
                title={hint(spec, "access_levels") || t("level.setHint")}
                aria-label={t("level.placeholder")}
                disabledReason={lockReason(spec, "access_levels", levels.length > 0, err)}
              >
                <option value="" disabled>
                  {t("level.placeholder")}
                </option>
                {levels.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </Select>
              <GhostButton
                onClick={() => kick(p)}
                title={hint(spec, "kick")}
                disabledReason={lockReason(spec, "kick", spec?.kick, err)}
              >
                <ShieldX className="h-4 w-4" /> {t("kick.label")}
              </GhostButton>
              <Button
                tone="danger"
                className="text-xs"
                onClick={() => ban(p)}
                title={hint(spec, "ban")}
                disabledReason={lockReason(spec, "ban", spec?.ban, err)}
              >
                <Ban className="h-4 w-4" /> {t("ban.short")}
              </Button>
            </div>
          </div>
        ))}
      </Card>

      <Card className="space-y-3">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{t("manual.title")}</div>
        <div className="grid gap-2 sm:grid-cols-2">
          <Input value={manual} onChange={(e) => setManual(e.target.value)} placeholder={t("manual.name")} />
          <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("manual.reason")} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <GhostButton
            disabled={!manual.trim()}
            onClick={() => kick(manual.trim())}
            title={hint(spec, "kick")}
            disabledReason={lockReason(spec, "kick", spec?.kick, err)}
          >
            <ShieldX className="h-4 w-4" /> {t("kick.label")}
          </GhostButton>
          <Button
            tone="danger"
            disabled={!manual.trim()}
            onClick={() => ban(manual.trim())}
            title={hint(spec, "ban")}
            disabledReason={lockReason(spec, "ban", spec?.ban, err)}
          >
            <Ban className="h-4 w-4" /> {t("ban.label")}
          </Button>
          <GhostButton
            disabled={!manual.trim()}
            onClick={() => ban(manual.trim(), true)}
            title={hint(spec, "ban_ip")}
            disabledReason={lockReason(spec, "ban_ip", spec?.ban_ip, err)}
          >
            {t("ban.withIp")}
          </GhostButton>
          <GhostButton
            disabled={!manual.trim()}
            onClick={() =>
              confirmed(
                t("unban.title", { name: manual.trim() }),
                t("unban.body", { name: manual.trim() }),
                t("unban.label"),
                () => call("/moderation/unban", { username: manual.trim() }, t("sent.unban", { name: manual.trim() })),
              )
            }
            title={hint(spec, "unban")}
            disabledReason={lockReason(spec, "unban", spec?.unban, err)}
          >
            {t("unban.label")}
          </GhostButton>
          {/* The divider is deliberately unconditional: it separates two groups that both always
              exist — the second one possibly greyed out. */}
          <span className="mx-1 hidden h-5 w-px bg-line sm:block" />
          <Select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            aria-label={t("cap.accessLevels")}
            disabledReason={lockReason(spec, "access_levels", levels.length > 0, err)}
          >
            <option value="">{t("level.placeholder")}</option>
            {levels.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </Select>
          <GhostButton
            disabled={!manual.trim() || !level}
            onClick={() => access(manual.trim(), level)}
            disabledReason={lockReason(spec, "access_levels", levels.length > 0, err)}
          >
            {t("level.set")}
          </GhostButton>
          <span className="mx-1 hidden h-5 w-px bg-line sm:block" />
          <GhostButton
            disabled={!manual.trim()}
            onClick={() => call("/moderation/whitelist/add", { username: manual.trim() }, t("sent.whitelistAdd", { name: manual.trim() }))}
            disabledReason={lockReason(spec, "whitelist", spec?.whitelist, err)}
          >
            <UserPlus className="h-4 w-4" /> {t("whitelist.add")}
          </GhostButton>
          <GhostButton
            disabled={!manual.trim()}
            onClick={() =>
              confirmed(
                t("whitelist.removeTitle", { name: manual.trim() }),
                t("whitelist.removeBody", { name: manual.trim() }),
                t("whitelist.removeConfirm"),
                () => call("/moderation/whitelist/remove", { username: manual.trim() }, t("sent.whitelistRemove", { name: manual.trim() })),
              )
            }
            disabledReason={lockReason(spec, "whitelist", spec?.whitelist, err)}
          >
            <UserMinus className="h-4 w-4" /> {t("whitelist.remove")}
          </GhostButton>
        </div>
      </Card>

      {/* The table stays hidden rather than greyed out: an empty table with a lock explains
          nothing the greyed-out whitelist buttons above do not already say. */}
      {spec?.whitelist && (
        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
            {wl.available ? t("whitelist.titleCount", { n: wl.users.length }) : t("whitelist.title")}
          </div>
          {/* Whether the list has any EFFECT (Avorion: `accessListMode`; Valheim: an empty list
              lets everybody in). Without it the page looks like a closed server. */}
          {wl.available && !!wl.note && <Alert kind="warn">{noteText(wl.note)}</Alert>}
          {!wl.available ? (
            <Card>
              <EmptyState>{t("whitelist.unreadable")}</EmptyState>
            </Card>
          ) : (
            <DataTable<WlUser>
              rows={wl.users}
              rowKey={(_, i) => i}
              empty={
                <Card>
                  <EmptyState>{t("whitelist.empty")}</EmptyState>
                </Card>
              }
              columns={[
                { header: t("column.player"), cell: (u) => <span className="font-medium text-fg">{u.username}</span> },
                { header: t("column.level"), cell: (u) => <span className="text-fg-muted">{u.accesslevel || "—"}</span> },
                {
                  header: t("column.status"),
                  cell: (u) => (u.banned ? <Badge tone="blood">{t("status.banned")}</Badge> : <Badge tone="online">{t("status.ok")}</Badge>),
                },
                { header: t("column.id"), cell: (u) => <span className="font-mono text-xs text-fg-dim">{u.steamid || "—"}</span> },
              ]}
            />
          )}
        </div>
      )}
    </div>
  );
}
