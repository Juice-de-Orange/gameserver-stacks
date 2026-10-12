import { Fragment, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Lock } from "lucide-react";
import { useAuth, Me } from "./auth";
import { FeedbackProvider } from "./components/feedback";
import { OverviewProvider } from "./components/overview";
import { LiveConsoleProvider } from "./components/liveconsole";
import {
  ServersProvider,
  ActiveServerProvider,
  useActiveServer,
  useActiveServerKey,
} from "./components/ServerContext";
import { Shell } from "./components/Layout";
import { EmptyState, Spinner } from "./components/ui";
import { Feature, featureAvailability } from "./lib/features";
import Fleet from "./pages/Fleet";
import CrossServer from "./pages/CrossServer";
import ServerHome from "./pages/ServerHome";
import ServerConsole from "./pages/ServerConsole";
import Resources from "./pages/Resources";
import Version from "./pages/Version";
import Login from "./pages/Login";
import Register from "./pages/Register";
import ChangePassword from "./pages/ChangePassword";
import Status from "./pages/Status";
import Actions from "./pages/Actions";
import Moderation from "./pages/Moderation";
import Restarts from "./pages/Restarts";
import Join from "./pages/Join";
import Logs from "./pages/Logs";
import Config from "./pages/Config";
import Mods from "./pages/Mods";
import Tickets from "./pages/Tickets";
import Stats from "./pages/Stats";
import MyStats from "./pages/MyStats";
import Backups from "./pages/Backups";
import Audit from "./pages/Audit";
import Users from "./pages/Users";
import AddServer from "./pages/AddServer";
import EditServer from "./pages/EditServer";
import SettingsGames from "./pages/SettingsGames";
import SettingsNetwork from "./pages/SettingsNetwork";

function Center({ children }: { children: ReactNode }) {
  return <div className="grid min-h-screen place-items-center text-fg-muted">{children}</div>;
}

function Protected({ children, check }: { children: ReactNode; check?: (me: Me) => boolean }) {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading)
    return (
      <Center>
        <Spinner className="h-6 w-6" />
      </Center>
    );
  if (!me) return <Navigate to="/login" replace />;
  if (me.must_change_pw && loc.pathname !== "/change-password")
    return <Navigate to="/change-password" replace />;
  if (check && !check(me)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

const has = (m: Me, f: string) => m.role === "admin" || !!m.effective_flags?.[f] || !!m.flags?.[f];
const isAdmin = (m: Me) => m.role === "admin";
const canConsole = (m: Me) => has(m, "can_console_safe") || has(m, "can_console_full");
const canActions = (m: Me) => has(m, "can_use_actions");
const canModerate = (m: Me) => has(m, "can_moderate");
const canRestart = (m: Me) => has(m, "can_restart");
const canLogs = (m: Me) => has(m, "can_view_logs");
const canConfig = (m: Me) => has(m, "can_edit_sandbox");
const canMods = (m: Me) => has(m, "can_manage_mods");
const canUsers = (m: Me) => has(m, "can_manage_users");
const canStats = (m: Me) => has(m, "can_view_player_stats");
const canBackups = (m: Me) => has(m, "can_manage_backups");
const canView = (m: Me) => has(m, "can_view_status");
const canResources = (m: Me) => has(m, "can_manage_resources");
const canVersions = (m: Me) => has(m, "can_manage_versions");

function page(node: ReactNode, check?: (me: Me) => boolean) {
  return (
    <Protected check={check}>
      <Shell>{node}</Shell>
    </Protected>
  );
}

/** A friendly "not available here" panel instead of bouncing the user back. */
function FeatureUnavailable({ reason }: { reason?: string }) {
  const { t } = useTranslation("app");
  return (
    <div className="mx-auto max-w-md py-12">
      <EmptyState icon={Lock}>
        <div className="space-y-1">
          <div className="font-semibold text-fg">{t("feature.unavailable")}</div>
          {reason && <div>{reason}</div>}
        </div>
      </EmptyState>
    </div>
  );
}

/** Checks whether the feature is available for the active server; otherwise the panel above. */
function FeatureGate({ feature, children }: { feature: Feature; children: ReactNode }) {
  const server = useActiveServer();
  const av = featureAvailability(server, feature);
  if (!av.available) return <FeatureUnavailable reason={av.reason} />;
  // The `key` is the actual point: React Router does NOT remount when switching from
  // `/s/pz-1/config` to `/s/mc/config` — same route, same element type, same place in the tree.
  // Without a forced remount every page carries its state over to the new server: the config
  // editor its changed fields, the mods page its file selection. A "save" before the reload has
  // finished would then write the values of ONE server into the OTHER. Here the protection
  // applies to all server-scoped routes at once.
  return <Fragment key={server?.key ?? "no-server"}>{children}</Fragment>;
}

/** Old path (`/actions` …) → server-scoped route of the ACTIVE (remembered) server. */
function LegacyRedirect({ feature }: { feature: string }) {
  const key = useActiveServerKey();
  // No server yet: the fleet page explains that, a `/s//console` would not.
  return <Navigate to={key ? `/s/${key}/${feature}` : "/"} replace />;
}

function gated(feature: Feature, node: ReactNode, check?: (me: Me) => boolean) {
  return page(<FeatureGate feature={feature}>{node}</FeatureGate>, check);
}

export default function App() {
  // Subscribing here re-renders the route elements when the language changes, so that pages
  // which build text with plain helpers (`fmtRelative`, `featureAvailability`, …) follow the
  // switch too — without a remount, which would throw away their state.
  useTranslation();
  return (
    <FeedbackProvider>
      <ServersProvider>
        <ActiveServerProvider>
          <OverviewProvider>
            <LiveConsoleProvider>
              <Routes>
                <Route path="/login" element={<Login />} />
                <Route path="/register" element={<Register />} />
                <Route path="/change-password" element={<Protected><ChangePassword /></Protected>} />
                <Route path="/" element={page(<Fleet />, canView)} />
                <Route path="/status" element={page(<Status />)} />
                <Route path="/cross" element={page(<CrossServer />, canView)} />
                <Route path="/servers/new" element={page(<AddServer />, isAdmin)} />

                {/* Server-scoped operations tabs (every operations feature per server) */}
                <Route path="/s/:serverKey" element={page(<ServerHome />, canView)} />
                <Route path="/s/:serverKey/overview" element={page(<ServerHome />, canView)} />
                {/* Not gated by a feature: a switched-off server must be editable (and removable). */}
                <Route path="/s/:serverKey/edit" element={page(<EditServer />, isAdmin)} />
                <Route path="/s/:serverKey/console" element={gated("console", <ServerConsole />, canConsole)} />
                <Route path="/s/:serverKey/actions" element={gated("actions", <Actions />, canActions)} />
                <Route path="/s/:serverKey/moderation" element={gated("moderation", <Moderation />, canModerate)} />
                <Route path="/s/:serverKey/config" element={gated("config", <Config />, canConfig)} />
                <Route path="/s/:serverKey/mods" element={gated("mods", <Mods />, canMods)} />
                <Route path="/s/:serverKey/logs" element={gated("logs", <Logs />, canLogs)} />
                <Route path="/s/:serverKey/resources" element={gated("resources", <Resources />, canResources)} />
                <Route path="/s/:serverKey/version" element={gated("version", <Version />, canVersions)} />
                <Route path="/s/:serverKey/restarts" element={gated("restarts", <Restarts />, canRestart)} />
                <Route path="/s/:serverKey/backups" element={gated("backups", <Backups />, canBackups)} />

                {/* Old paths → scoped route of the active server (bookmarks, compatibility) */}
                <Route path="/console" element={<Protected check={canConsole}><LegacyRedirect feature="console" /></Protected>} />
                <Route path="/actions" element={<Protected check={canActions}><LegacyRedirect feature="actions" /></Protected>} />
                <Route path="/moderation" element={<Protected check={canModerate}><LegacyRedirect feature="moderation" /></Protected>} />
                <Route path="/config" element={<Protected check={canConfig}><LegacyRedirect feature="config" /></Protected>} />
                <Route path="/mods" element={<Protected check={canMods}><LegacyRedirect feature="mods" /></Protected>} />
                <Route path="/logs" element={<Protected check={canLogs}><LegacyRedirect feature="logs" /></Protected>} />
                <Route path="/restarts" element={<Protected check={canRestart}><LegacyRedirect feature="restarts" /></Protected>} />
                <Route path="/backups" element={<Protected check={canBackups}><LegacyRedirect feature="backups" /></Protected>} />

                {/* Global pages (not tied to a server) */}
                <Route path="/join" element={page(<Join />)} />
                <Route path="/tickets" element={page(<Tickets />)} />
                <Route path="/stats" element={page(<Stats />, canStats)} />
                <Route path="/my-stats" element={page(<MyStats />)} />
                <Route path="/audit" element={page(<Audit />, isAdmin)} />
                <Route path="/settings/games" element={page(<SettingsGames />, isAdmin)} />
                <Route path="/settings/network" element={page(<SettingsNetwork />, isAdmin)} />
                <Route path="/users" element={page(<Users />, canUsers)} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </LiveConsoleProvider>
          </OverviewProvider>
        </ActiveServerProvider>
      </ServersProvider>
    </FeedbackProvider>
  );
}
