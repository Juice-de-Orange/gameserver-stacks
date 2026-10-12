import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { BrandMark } from "../components/BrandMark";
import { api, errText } from "../api";
import { Alert, Button, Card, Field, Input } from "../components/ui";

export default function Register() {
  const { t } = useTranslation("register");
  const [form, setForm] = useState({ display_name: "", username: "", password: "" });
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr("");
    setMsg("");
    setBusy(true);
    try {
      const res = await api("/auth/register", { method: "POST", body: JSON.stringify(form) });
      if (res.ok) {
        setMsg(t("received"));
      } else {
        setErr(await errText(res, t("error.failed")));
      }
    } catch {
      // Network error (fetch rejected): without try/finally `busy` would stay true and the
      // button locked for good, with no feedback.
      setErr(t("error.unreachable"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <Card className="w-full max-w-sm space-y-4">
        <div className="mb-1 flex flex-col items-center gap-1">
          <BrandMark className="h-9 w-9 text-primary" />
          <div className="text-stencil text-xl text-fg">{t("title")}</div>
        </div>
        {msg && <Alert kind="success">{msg}</Alert>}
        {err && <Alert>{err}</Alert>}
        {!msg && (
          <form onSubmit={submit} className="space-y-3">
            <Field label={t("field.displayName")}>
              <Input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
            </Field>
            <Field label={t("field.username")}>
              <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            </Field>
            <Field label={t("field.password")}>
              <Input
                type="password"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
            </Field>
            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "…" : t("submit")}
            </Button>
          </form>
        )}
        <p className="text-center text-sm text-fg-muted">
          <Link to="/login" className="text-success-text hover:underline">
            {t("backToLogin")}
          </Link>
        </p>
      </Card>
    </div>
  );
}
