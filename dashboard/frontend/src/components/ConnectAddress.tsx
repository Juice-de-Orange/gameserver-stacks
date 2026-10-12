// Where players connect to a server: `host:port`, from the backend (`services/fleet.connect_of`,
// the driver's `connect_info` — the same source as the join page).
//
// An empty host means no public address is configured. Then the address is NOT made up from the
// dashboard's own URL: that is often a different host (a proxy, a tunnel) and a wrong address is
// worse than none. Admins get the way to set it.
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Copy, Globe } from "lucide-react";
import { useAuth } from "../auth";
import { connectAddress } from "../lib/net";
import { useToast } from "./feedback";
import { Card, GhostButton } from "./ui";

export interface Connect {
  host: string;
  port: number;
  protocol: string;
}

/** One line for a table cell: the address, or a dash. */
export function ConnectLine({ connect }: { connect: Connect | null | undefined }) {
  const { t } = useTranslation("connectAddress");
  const addr = connectAddress(connect?.host, connect?.port);
  return (
    <div
      data-testid="connect-address"
      className="truncate font-mono text-[11px] text-fg-muted"
      title={addr ? t("title") : t("missing")}
    >
      {addr || "—"}
    </div>
  );
}

/** The card on a server's page: address, protocol, copy — or how to set it. */
export function ConnectCard({ connect }: { connect: Connect | null | undefined }) {
  const { t } = useTranslation(["connectAddress", "common"]);
  const { me } = useAuth();
  const toast = useToast();
  if (!connect) return null;
  const addr = connectAddress(connect.host, connect.port);

  return (
    <Card className="space-y-2">
      <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-fg-muted">
        <Globe className="h-4 w-4" /> {t("title")}
      </div>
      {addr ? (
        <div className="flex flex-wrap items-center gap-2">
          <code data-testid="connect-address" className="rounded-md border border-line bg-surface-raised px-3 py-1.5 font-mono text-sm text-fg">
            {addr}
          </code>
          <span className="text-xs text-fg-dim">{connect.protocol.toUpperCase()}</span>
          <GhostButton
            onClick={() => {
              navigator.clipboard?.writeText(addr);
              toast.success(t("common:state.copied"));
            }}
          >
            <Copy className="h-4 w-4" /> {t("common:action.copy")}
          </GhostButton>
        </div>
      ) : (
        <div className="text-sm text-fg-muted">
          {t("missing")}{" "}
          {me?.role === "admin" && (
            <Link to="/settings/network" className="font-semibold text-primary-text underline-offset-2 hover:underline">
              {t("setIt")}
            </Link>
          )}
        </div>
      )}
    </Card>
  );
}
