import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { KeyRound, LogOut } from "lucide-react";
import { api, errText, setTokens } from "../api";
import { useAuth } from "../auth";
import { useConfirm } from "../components/feedback";
import { Alert, Button, Card, Field, GhostButton, Input } from "../components/ui";

export default function ChangePassword() {
  const { t } = useTranslation("changePassword");
  const { reload, me, logout } = useAuth();
  const confirm = useConfirm();
  const nav = useNavigate();
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const res = await api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
      });
      if (res.ok) {
        // The change revokes every token issued before, including the one just used; the
        // response carries the pair this session continues with.
        const fresh = await res.json().catch(() => null);
        if (fresh?.access_token) setTokens(fresh.access_token, fresh.refresh_token);
        await reload();
        nav("/");
      } else {
        setErr(await errText(res, t("common:state.error")));
      }
    } catch {
      // Network error: without try/finally the button would stay locked for good.
      setErr(t("error.unreachable"));
    } finally {
      setBusy(false);
    }
  }

  /**
   * Sign out everywhere: the backend raises the token version, which revokes every token of this
   * account — the one of this session included. So the local sign-out follows only after the
   * call has succeeded; on a failure the session stays as it is and the error is shown.
   */
  async function logoutAll() {
    const ok = await confirm({
      title: t("logoutAll.title"),
      body: t("logoutAll.body"),
      danger: true,
      confirmLabel: t("logoutAll.label"),
      cancelLabel: t("common:action.cancel"),
    });
    if (!ok) return;
    setErr("");
    setBusy(true);
    try {
      const res = await api("/auth/logout-all", { method: "POST" });
      if (res.ok) logout();
      else setErr(await errText(res, t("common:state.error")));
    } catch {
      setErr(t("error.unreachable"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <Card className="w-full max-w-sm space-y-4">
        <div className="mb-1 flex flex-col items-center gap-1">
          <KeyRound className="h-9 w-9 text-danger-text" strokeWidth={2.2} />
          <div className="text-stencil text-xl text-fg">{t("title")}</div>
        </div>
        {me?.must_change_pw && <Alert kind="info">{t("mustChange")}</Alert>}
        {err && <Alert>{err}</Alert>}
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("field.current")}>
            <Input type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} />
          </Field>
          <Field label={t("field.new")}>
            <Input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} />
          </Field>
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "…" : t("common:action.save")}
          </Button>
        </form>
        <div className="space-y-2 border-t border-line pt-3">
          <p className="text-xs text-fg-dim">{t("logoutAll.hint")}</p>
          <GhostButton type="button" disabled={busy} onClick={logoutAll} className="w-full">
            <LogOut className="h-4 w-4" /> {t("logoutAll.label")}
          </GhostButton>
        </div>
      </Card>
    </div>
  );
}
