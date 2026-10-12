import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { KeyRound, ShieldCheck, Trash2, UserCheck, Users as UsersIcon } from "lucide-react";
import { api, apiJson } from "../api";
import { Me, useAuth } from "../auth";
import { useConfirm, useToast, Modal } from "../components/feedback";
import { fmtRelative } from "../lib/format";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  GhostButton,
  SectionTitle,
  Select,
  cx,
} from "../components/ui";

// Catalog key per permission flag, translated when rendering. The chips are built from THIS
// table, so it must list every flag of `PERMISSION_FLAGS` (`models/user.py`) — a missing one is
// neither visible nor assignable per user. `tests/test_rbac.py` compares both lists.
const FLAG_LABEL: Record<string, string> = {
  can_view_status: "flag.can_view_status",
  can_restart: "flag.can_restart",
  can_console_safe: "flag.can_console_safe",
  can_console_full: "flag.can_console_full",
  can_edit_sandbox: "flag.can_edit_sandbox",
  can_manage_mods: "flag.can_manage_mods",
  can_manage_users: "flag.can_manage_users",
  can_view_logs: "flag.can_view_logs",
  can_manage_backups: "flag.can_manage_backups",
  can_manage_tickets: "flag.can_manage_tickets",
  can_view_player_stats: "flag.can_view_player_stats",
  can_use_actions: "flag.can_use_actions",
  can_moderate: "flag.can_moderate",
  can_manage_resources: "flag.can_manage_resources",
  can_manage_versions: "flag.can_manage_versions",
  can_manage_cross_server: "flag.can_manage_cross_server",
};

interface MatrixData {
  roles: string[];
  flags: string[];
  matrix: Record<string, Record<string, boolean>>;
}

export default function Users() {
  const { t } = useTranslation("users");
  const flagLabel = (f: string) => (FLAG_LABEL[f] ? t(FLAG_LABEL[f]) : f);
  const toast = useToast();
  const confirm = useConfirm();
  // The route `/users` depends on `can_manage_users` — that is NOT full admin. A holder of that
  // right may approve, enable, disable, delete and reset passwords. Assigning roles and setting
  // flags is not allowed (`users._guard_role_and_flags`, 403), and `/roles/matrix` needs
  // `require_admin` both ways — so those controls are locked or hidden for a non-admin.
  const { me } = useAuth();
  const isAdmin = me?.role === "admin";
  const adminOnly = isAdmin ? undefined : t("lock.adminOnly");
  const [tab, setTab] = useState<"users" | "roles">("users");
  const [users, setUsers] = useState<Me[]>([]);
  const [matrix, setMatrix] = useState<MatrixData | null>(null);
  const [err, setErr] = useState("");
  // The password belongs to an ACCOUNT, it does not stand alone: the dialog names the account,
  // so that after two resets in quick succession it is still clear whose password is shown.
  const [reset, setReset] = useState<{ name: string; pw: string } | null>(null);
  const [resetBusy, setResetBusy] = useState<number | null>(null);

  async function loadUsers() {
    try {
      setUsers(await apiJson<Me[]>("/users"));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  async function loadMatrix() {
    try {
      setMatrix(await apiJson<MatrixData>("/roles/matrix"));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    loadUsers();
    // Not for non-admins: the call ends in a 403, and its text would sit as a red banner above
    // the working user list, explaining nothing.
    if (isAdmin) loadMatrix();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin]);

  async function approve(id: number) {
    // apiJson throws on HTTP errors — otherwise the success toast would show on a 403/500 too.
    try {
      await apiJson(`/users/${id}/approve`, { method: "POST", body: JSON.stringify({ role: "member" }) });
      toast.success(t("approve.done"));
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function setRole(id: number, role: string) {
    // `admin` brings the full console, version changes, restore and user management — and
    // `trusted` carries `can_console_safe` on EVERY game server in the default matrix. Neither
    // should be a single click in a select without a confirmation.
    const u = users.find((x) => x.id === id);
    if (role === "admin" || role === "trusted") {
      const ok = await confirm({
        title: t("role.title", { name: u?.display_name ?? id, role }),
        body: role === "admin" ? t("role.bodyAdmin") : t("role.bodyTrusted"),
        danger: role === "admin",
        confirmLabel: t("role.confirm"),
      });
      if (!ok) {
        loadUsers();   // otherwise the select keeps showing the value that was not set
        return;
      }
    }
    try {
      await apiJson(`/users/${id}`, { method: "PATCH", body: JSON.stringify({ role }) });
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function setStatus(id: number, action: "disable" | "enable") {
    try {
      await apiJson(`/users/${id}/${action}`, { method: "POST" });
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function toggleFlag(u: Me, flag: string) {
    try {
      await apiJson(`/users/${u.id}`, {
        method: "PATCH",
        body: JSON.stringify({ flags: { [flag]: !u.flags[flag] } }),
      });
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
    }
  }
  async function doReset(u: Me) {
    // Every call sets a new `pw_hash` and `must_change_pw` on the server AT ONCE. An accidental
    // second click locks the account out of its password again, and the first new one is then
    // shown nowhere any more.
    const ok = await confirm({
      title: t("reset.title", { name: u.display_name }),
      body: t("reset.body", { username: u.username }),
      danger: true,
      confirmLabel: t("reset.confirm"),
    });
    if (!ok) return;
    setResetBusy(u.id);   // nothing visible happens until the answer — without this one clicks again
    try {
      const r = await apiJson<{ temp_password: string }>(`/users/${u.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      setReset({ name: `${u.display_name} (@${u.username})`, pw: r.temp_password });
    } catch (e: any) {
      toast.error(e.message);  // without the catch an error would be an unhandled rejection (no feedback)
    } finally {
      setResetBusy(null);
    }
  }
  async function del(u: Me) {
    const ok = await confirm({
      title: t("delete.title", { name: u.display_name }),
      body: t("delete.body", { username: u.username }),
      danger: true,
      confirmLabel: t("common:action.delete"),
    });
    if (!ok) return;
    try {
      // `apiJson` throws on HTTP errors — `api` does not, and the success toast would show even
      // on a 400 (deleting oneself, the last active admin) or a 403.
      await apiJson(`/users/${u.id}`, { method: "DELETE" });
      toast.success(t("delete.done"));
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  function toggleMatrix(role: string, flag: string) {
    if (!matrix || role === "admin") return;
    setMatrix({
      ...matrix,
      matrix: {
        ...matrix.matrix,
        [role]: { ...matrix.matrix[role], [flag]: !matrix.matrix[role][flag] },
      },
    });
  }
  async function saveMatrix() {
    if (!matrix) return;
    try {
      // Same here: `apiJson`, so that a failure is not reported as saved.
      await apiJson("/roles/matrix", { method: "PUT", body: JSON.stringify({ matrix: matrix.matrix }) });
      toast.success(t("matrix.saved"));
      loadUsers();
    } catch (e: any) {
      toast.error(e.message);
      // Fetch the server state back. `toggleMatrix` changes ONLY the local state; without this
      // the ticks that were never saved would stay — and the next successful "Save" would write
      // them silently. This view is the only feedback the admin has about rights that apply
      // immediately.
      loadMatrix();
    }
  }

  const pending = users.filter((u) => u.status === "pending");
  const others = users.filter((u) => u.status !== "pending");

  const tabCls = (t: string) =>
    cx(
      "inline-flex min-h-11 items-center rounded-lg px-3.5 py-1.5 text-sm font-medium transition sm:min-h-9",
      tab === t
        ? "bg-surface-card text-primary-text shadow-sm ring-1 ring-primary/25"
        : "text-fg-muted hover:bg-surface-sunken hover:text-fg",
    );

  return (
    <div className="space-y-5">
      <SectionTitle icon={UsersIcon} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>

      {err && <Alert>{err}</Alert>}

      <div className="flex gap-1 rounded-md border border-line-soft bg-surface-card/50 p-1">
        <button className={tabCls("users")} onClick={() => setTab("users")}>
          {t("tab.users")}
        </button>
        {/* A role/right hides, a game capability greys out (convention from `lib/features.ts`).
            Here the RIGHT is missing — the tab would be empty throughout for a non-admin. */}
        {isAdmin && (
          <button className={tabCls("roles")} onClick={() => setTab("roles")}>
            {t("tab.roles")}
          </button>
        )}
      </div>

      {tab === "users" && (
        <div className="space-y-5">
          {pending.length > 0 && (
            <section className="space-y-2">
              <div className="text-xs font-semibold uppercase tracking-[0.2em] text-warning-text">
                {t("pending.title", { n: pending.length })}
              </div>
              {pending.map((u) => (
                <Card key={u.id} className="flex items-center justify-between !p-3">
                  <div>
                    <b className="text-fg">{u.display_name}</b>{" "}
                    <span className="text-fg-dim">@{u.username}</span>
                    <div className="text-xs text-fg-dim">
                      {t("seen.registered", { when: u.created_at ? fmtRelative(u.created_at) : "—" })}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button onClick={() => approve(u.id)}>
                      <UserCheck className="h-4 w-4" /> {t("approve.label")}
                    </Button>
                    <GhostButton onClick={() => del(u)} title={t("common:action.delete")} className="hover:border-danger-border hover:text-danger-text">
                      <Trash2 className="h-4 w-4" />
                    </GhostButton>
                  </div>
                </Card>
              ))}
            </section>
          )}

          <section className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
              {t("list.title", { n: others.length })}
            </div>
            {others.map((u) => {
              const admin = u.role === "admin";
              return (
                <Card key={u.id} className="space-y-3 !p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <b className="text-fg">{u.display_name}</b>
                      <span className="text-fg-dim">@{u.username}</span>
                      <Badge tone={u.status === "disabled" ? "blood" : "online"}>{u.status}</Badge>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Select
                        value={u.role}
                        onChange={(e) => setRole(u.id, e.target.value)}
                        disabledReason={adminOnly}
                      >
                        <option value="admin">admin</option>
                        <option value="trusted">trusted</option>
                        <option value="member">member</option>
                      </Select>
                      <GhostButton disabled={resetBusy === u.id} onClick={() => doReset(u)}>
                        <KeyRound className="h-4 w-4" />
                        {resetBusy === u.id ? t("reset.busy") : t("reset.label")}
                      </GhostButton>
                      {u.status === "disabled" ? (
                        <GhostButton onClick={() => setStatus(u.id, "enable")}>{t("status.enable")}</GhostButton>
                      ) : (
                        <GhostButton onClick={() => setStatus(u.id, "disable")}>{t("status.disable")}</GhostButton>
                      )}
                      <GhostButton onClick={() => del(u)} title={t("delete.label")} className="hover:border-danger-border hover:text-danger-text">
                        <Trash2 className="h-4 w-4" />
                      </GhostButton>
                    </div>
                  </div>

                  <div className="text-xs text-fg-dim">
                    {/* `last_seen`, NOT `last_login`: the latter is only written on sign-in, and with
                        a refresh token valid for days it would say "5 d ago" for someone who is
                        signed in right now. The timestamps carry their zone themselves
                        (the `UserOut` schema serialises them as UTC) — hence no `needsZ`. */}
                    {t("seen.lastOnline")} <span className="text-fg-muted">{u.last_seen ? fmtRelative(u.last_seen) : t("seen.never")}</span>
                    {" · "}{t("seen.signedIn", { when: u.last_login ? fmtRelative(u.last_login) : t("seen.never") })}
                    {" · "}{t("seen.registered", { when: u.created_at ? fmtRelative(u.created_at) : "—" })}
                  </div>

                  <div>
                    <div className="mb-1.5 text-xs text-fg-dim">
                      <Trans
                        t={t}
                        i18nKey={isAdmin ? "flags.legendAdmin" : "flags.legendOther"}
                        components={{ inh: <span className="text-success-text" /> }}
                      />
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {Object.keys(FLAG_LABEL).map((f) => {
                        const override = !!u.flags[f];
                        const effective = admin || !!u.effective_flags?.[f];
                        const inherited = effective && !override;
                        return (
                          <button
                            key={f}
                            onClick={() => isAdmin && !admin && !inherited && toggleFlag(u, f)}
                            disabled={!isAdmin || admin || inherited}
                            title={
                              !isAdmin
                                ? t("flags.adminOnly")
                                : admin
                                  ? t("flags.adminHasAll")
                                  : inherited
                                    ? t("flags.inherited")
                                    : flagLabel(f)
                            }
                            className={cx(
                              "rounded border px-2 py-0.5 text-xs transition",
                              effective
                                ? inherited
                                  ? "border-success-border bg-success-soft/30 text-success-text/80"
                                  : "border-success-border bg-success-soft/60 text-success-text"
                                : "border-line text-fg-dim hover:text-fg",
                              (!isAdmin || admin || inherited) && "cursor-default",
                            )}
                          >
                            {flagLabel(f)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </Card>
              );
            })}
            {others.length === 0 && (
              <Card>
                <EmptyState icon={UsersIcon}>{t("list.empty")}</EmptyState>
              </Card>
            )}
          </section>
        </div>
      )}

      {tab === "roles" && matrix && (
        <Card className="space-y-3 overflow-x-auto">
          <div className="flex items-center gap-2 text-sm text-fg-muted">
            <ShieldCheck className="h-4 w-4 text-success-text" />
            <Trans t={t} i18nKey="matrix.intro" components={{ b: <b className="text-fg" /> }} />
          </div>
          {/* Mobile: one card per right with the roles as large switches (instead of a narrow table). */}
          <div className="space-y-2 sm:hidden">
            {matrix.flags.map((f) => (
              <div key={f} className="rounded-xl border border-line bg-surface-sunken/40 p-3">
                <div className="mb-2 text-sm font-medium text-fg">{flagLabel(f)}</div>
                <div className="flex flex-wrap gap-2">
                  {matrix.roles.map((r) => {
                    const on = !!matrix.matrix[r]?.[f];
                    const locked = r === "admin";
                    return (
                      <button
                        key={r}
                        onClick={() => toggleMatrix(r, f)}
                        disabled={locked}
                        className={cx(
                          "inline-flex min-h-11 items-center gap-1.5 rounded-lg border px-3 text-sm font-medium uppercase tracking-wide transition",
                          on
                            ? "border-primary/50 bg-primary-soft text-primary-text"
                            : "border-line bg-surface-raised text-fg-muted",
                          locked && "cursor-default opacity-70",
                        )}
                        title={locked ? t("flags.adminHasAll") : on ? t("matrix.on") : t("matrix.off")}
                      >
                        <span
                          className={cx(
                            "inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px]",
                            on ? "bg-primary-btn text-primary-fg" : "bg-surface-sunken",
                          )}
                        >
                          {on ? "✓" : ""}
                        </span>
                        {r}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>

          <table className="hidden w-full text-sm sm:table">
            <thead>
              <tr className="text-left text-fg-dim">
                <th className="py-2 pr-4 font-medium">{t("matrix.right")}</th>
                {matrix.roles.map((r) => (
                  <th key={r} className="px-3 py-2 text-center font-medium uppercase tracking-wide">
                    {r}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.flags.map((f) => (
                <tr key={f} className="border-t border-surface-card">
                  <td className="py-2 pr-4 text-fg">{flagLabel(f)}</td>
                  {matrix.roles.map((r) => {
                    const on = !!matrix.matrix[r]?.[f];
                    const locked = r === "admin";
                    return (
                      <td key={r} className="px-3 py-2 text-center">
                        <button
                          onClick={() => toggleMatrix(r, f)}
                          disabled={locked}
                          className={cx(
                            "h-6 w-6 rounded border transition",
                            on
                              ? "border-primary bg-primary-btn text-primary-fg"
                              : "border-line-strong bg-surface-raised hover:border-fg-dim",
                            locked && "cursor-default opacity-70",
                          )}
                          title={locked ? t("flags.adminHasAll") : on ? t("matrix.on") : t("matrix.off")}
                        >
                          {on ? "✓" : ""}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <Button onClick={saveMatrix}>{t("matrix.save")}</Button>
          </div>
        </Card>
      )}

      {reset && (
        <Modal onClose={() => setReset(null)}>
          <h2 className="text-stencil text-lg text-fg">{t("temp.title")}</h2>
          <p className="text-sm text-fg-muted">
            <Trans
              t={t}
              i18nKey="temp.body"
              values={{ name: reset.name }}
              components={{ b: <b className="text-fg" /> }}
            />
          </p>
          <div className="select-all rounded-md border border-line bg-surface-raised px-3 py-2 text-center font-mono text-lg text-success-text">
            {reset.pw}
          </div>
          <div className="flex justify-end gap-2">
            <GhostButton
              onClick={() => {
                navigator.clipboard?.writeText(reset.pw);
                toast.success(t("temp.copied"));
              }}
            >
              {t("common:action.copy")}
            </GhostButton>
            <Button onClick={() => setReset(null)}>{t("common:action.close")}</Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
