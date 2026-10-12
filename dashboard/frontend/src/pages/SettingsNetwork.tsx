// Settings → Network (admin): the address players connect to.
//
// The value saved here wins over `PUBLIC_HOST` from the environment, which stays the starting
// value; clearing it falls back to that variable (`backend/app/services/install_settings.py`).
// A server's own address on its edit page wins over both. The fleet, each server's page and the
// join page show the result.
import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Globe, Save } from "lucide-react";
import { api, apiJson, errText } from "../api";
import { useToast } from "../components/feedback";
import { FirewallPanel } from "../components/Firewall";
import { Alert, Button, Card, Field, GhostButton, Input, SectionTitle, Skeleton } from "../components/ui";

export interface NetworkState {
  public_host: string;
  stored: string;
  env: string;
  source: "stored" | "env" | "none";
}

export default function SettingsNetwork() {
  const { t } = useTranslation(["settingsNetwork", "common"]);
  const toast = useToast();
  const [state, setState] = useState<NetworkState | null>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiJson<NetworkState>("/settings/network")
      .then((s) => {
        setState(s);
        setValue(s.stored);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  async function save(next: string) {
    setBusy(true);
    setError("");
    try {
      const res = await api("/settings/network", { method: "PUT", body: JSON.stringify({ public_host: next.trim() }) });
      if (!res.ok) {
        setError(await errText(res, t("saveFailed")));
        return;
      }
      const s = (await res.json()) as NetworkState;
      setState(s);
      setValue(s.stored);
      toast.success(t("saved"));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    void save(value);
  }

  const source =
    state?.source === "stored"
      ? t("source.stored")
      : state?.source === "env"
        ? t("source.env", { value: state.env })
        : t("source.none");

  return (
    <div className="space-y-5">
      <SectionTitle icon={Globe} sub={t("subtitle")}>
        {t("title")}
      </SectionTitle>
      {!state && !error && <Skeleton className="h-40" />}
      {state && (
        <Card className="space-y-4">
          <form onSubmit={submit} className="space-y-4" noValidate>
            <Field label={t("field.publicHost")} hint={t("hint.publicHost")}>
              <Input
                aria-label={t("field.publicHost")}
                value={value}
                placeholder={state.env || "play.example.com"}
                onChange={(e) => setValue(e.target.value)}
              />
            </Field>
            <div className="text-sm text-fg" data-testid="public-host-source">
              {source}
            </div>
            {state.source === "none" && <Alert kind="warn">{t("none")}</Alert>}
            {error && <Alert kind="error">{error}</Alert>}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={busy || value.trim() === state.stored}>
                <Save className="h-4 w-4" /> {t("common:action.save")}
              </Button>
              {state.stored && (
                <GhostButton type="button" disabled={busy} onClick={() => void save("")}>
                  {t("useEnv")}
                </GhostButton>
              )}
            </div>
          </form>
          <div className="text-xs text-fg-dim">{t("perServer")}</div>
        </Card>
      )}
      {!state && error && <Alert kind="error">{error}</Alert>}
      <FirewallPanel />
    </div>
  );
}
