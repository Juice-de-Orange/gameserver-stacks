import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, Link } from "react-router-dom";
import { BrandMark } from "../components/BrandMark";
import { useAuth } from "../auth";
import { Alert, Button, Card, Field, Input } from "../components/ui";
import { APP_NAME, appSub } from "../lib/brand";

function Brand() {
  return (
    <div className="mb-2 flex flex-col items-center gap-1">
      <BrandMark className="h-10 w-10 text-primary" />
      <div className="text-stencil text-2xl text-fg">{APP_NAME}</div>
      <div className="text-[10px] uppercase tracking-[0.35em] text-fg-dim">{appSub()}</div>
    </div>
  );
}

export default function Login() {
  const { t } = useTranslation("login");
  const { login } = useAuth();
  const nav = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      await login(username, password);
      nav("/");
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <Card className="w-full max-w-sm space-y-4">
        <Brand />
        {err && <Alert>{err}</Alert>}
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("field.username")}>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </Field>
          <Field label={t("field.password")}>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "…" : t("submit")}
          </Button>
        </form>
        <p className="text-center text-sm text-fg-muted">
          {t("noAccount")}{" "}
          <Link to="/register" className="font-medium text-primary-text hover:underline">
            {t("register")}
          </Link>
        </p>
      </Card>
    </div>
  );
}
