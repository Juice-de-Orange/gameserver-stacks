import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, apiJson, errText, setTokens, clearTokens, getAccess } from "./api";
import i18n from "./i18n";

export type Role = "admin" | "trusted" | "member";

export interface Me {
  id: number;
  display_name: string;
  username: string;
  role: Role;
  status: string;
  must_change_pw: boolean;
  flags: Record<string, boolean>; // per-user overrides only (for the admin UI)
  effective_flags: Record<string, boolean>; // effective (admin/role/override) — for gating
  created_at?: string;
  last_login?: string | null;   // last sign-in
  last_seen?: string | null;    // last request of any kind — that is "last online"
}

interface AuthValue {
  me: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  reload: () => Promise<void>;
  can: (flag: string) => boolean;
}

const AuthCtx = createContext<AuthValue>(null as unknown as AuthValue);
export const useAuth = () => useContext(AuthCtx);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  async function reload(): Promise<void> {
    if (!getAccess()) {
      setMe(null);
      setLoading(false);
      return;
    }
    try {
      setMe(await apiJson<Me>("/auth/me"));
    } catch {
      clearTokens();
      setMe(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    reload();
  }, []);

  async function login(username: string, password: string): Promise<void> {
    const body = new URLSearchParams({ username, password });
    const res = await api("/auth/login", { method: "POST", body });
    if (!res.ok) {
      // `detail` may be a coded object (`{code, message, params}`); `errText` turns it into the
      // translated text instead of "[object Object]".
      throw new Error(await errText(res, i18n.t("auth:login.failed")));
    }
    const data = await res.json();
    setTokens(data.access_token, data.refresh_token);
    await reload();
  }

  function logout(): void {
    clearTokens();
    setMe(null);
  }

  const can = (flag: string) =>
    !!me && (me.role === "admin" || !!me.effective_flags?.[flag] || !!me.flags?.[flag]);

  return (
    <AuthCtx.Provider value={{ me, loading, login, logout, reload, can }}>
      {children}
    </AuthCtx.Provider>
  );
}
