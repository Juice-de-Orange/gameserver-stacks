import {
  Archive,
  BarChart3,
  CalendarClock,
  ClipboardList,
  Cpu,
  DoorOpen,
  Gamepad2,
  Gauge,
  Gavel,
  Globe,
  LayoutGrid,
  Network,
  Package,
  ScrollText,
  SlidersHorizontal,
  Swords,
  Tag,
  Terminal,
  Ticket,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useAuth } from "../auth";
import { useActiveServer, useServers } from "./ServerContext";
import { ServerInfo } from "../lib/game";
import { Feature, featureAvailability } from "../lib/features";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  show: boolean;             // role/permission — false ⇒ the entry is removed entirely
  disabled?: boolean;        // game capability — visible but not clickable
  disabledReason?: string;   // tooltip text saying why it does not work
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

/** Role-dependent navigation — shared by the sidebar (desktop) and the BottomNav (mobile).
 *
 * The labels are translated here, inside the hook: `useTranslation` re-renders the callers when
 * the language changes. A label table at module level would freeze the language at import. */
export function useNav(): NavGroup[] {
  const { t } = useTranslation("nav");
  const { me, can } = useAuth();
  const active = useActiveServer();
  const { servers } = useServers();
  const admin = me?.role === "admin";
  const multiGame = servers.filter((s) => s.enabled).length >= 2;

  // One server tab entry: `show` = permission (removed), availability from
  // featureAvailability (greyed out with a reason instead of removed).
  const si = (
    server: ServerInfo,
    feature: Feature,
    path: string,
    label: string,
    icon: LucideIcon,
    show: boolean,
  ): NavItem => {
    const av = featureAvailability(server, feature);
    return { to: path, label, icon, show, disabled: !av.available, disabledReason: av.reason };
  };

  // Server-scoped navigation: ALL operations tabs of the active server in one place.
  // Unavailable tabs stay visible but greyed out — so it is always clear what works for
  // this server and what does not.
  const serverGroup: NavGroup | null = active
    ? {
        title: t("group.server"), // the active server itself is shown in the server head (ServerChip)
        items: [
          si(active, "overview", `/s/${active.key}`, t("item.overview"), Gauge, can("can_view_status")),
          si(active, "console", `/s/${active.key}/console`, t("item.console"), Terminal, can("can_console_safe") || can("can_console_full")),
          si(active, "actions", `/s/${active.key}/actions`, t("item.actions"), Swords, can("can_use_actions")),
          si(active, "moderation", `/s/${active.key}/moderation`, t("item.moderation"), Gavel, can("can_moderate")),
          si(active, "config", `/s/${active.key}/config`, t("item.config"), SlidersHorizontal, can("can_edit_sandbox")),
          si(active, "mods", `/s/${active.key}/mods`, t("item.mods"), Package, can("can_manage_mods")),
          si(active, "logs", `/s/${active.key}/logs`, t("item.logs"), ScrollText, can("can_view_logs")),
          si(active, "resources", `/s/${active.key}/resources`, t("item.resources"), Cpu, can("can_manage_resources")),
          si(active, "version", `/s/${active.key}/version`, t("item.version"), Tag, can("can_manage_versions")),
          si(active, "restarts", `/s/${active.key}/restarts`, t("item.restarts"), CalendarClock, can("can_restart")),
          si(active, "backups", `/s/${active.key}/backups`, t("item.backups"), Archive, can("can_manage_backups")),
        ],
      }
    : null;

  const groups: NavGroup[] = [
    {
      title: t("group.overview"),
      items: [
        // The fleet page is the start page: whoever opens the dashboard first wants to know how
        // the whole fleet is doing — not one server selected days ago. The single-server status
        // page sits next to it on `/status`.
        { to: "/", label: t("item.fleet"), icon: LayoutGrid, show: true },
        { to: "/status", label: t("item.status"), icon: Gauge, show: true },
        { to: "/cross", label: t("item.cross"), icon: Network, show: multiGame },
      ],
    },
    ...(serverGroup ? [serverGroup] : []),
    {
      title: t("group.community"),
      items: [
        { to: "/tickets", label: t("item.tickets"), icon: Ticket, show: true },
        { to: "/join", label: t("item.join"), icon: DoorOpen, show: true },
        // Own stats for players without the full stats permission (link the account there and
        // see one's own data).
        { to: "/my-stats", label: t("item.myStats"), icon: BarChart3, show: !!me && !can("can_view_player_stats") },
        { to: "/stats", label: t("item.stats"), icon: BarChart3, show: can("can_view_player_stats") },
        { to: "/users", label: t("item.users"), icon: Users, show: can("can_manage_users") },
      ],
    },
    {
      title: t("group.system"),
      items: [
        { to: "/settings/games", label: t("item.games"), icon: Gamepad2, show: admin },
        { to: "/settings/network", label: t("item.network"), icon: Globe, show: admin },
        { to: "/audit", label: t("item.audit"), icon: ClipboardList, show: admin },
      ],
    },
  ];
  return groups
    .map((g) => ({ ...g, items: g.items.filter((i) => i.show) }))
    .filter((g) => g.items.length > 0);
}
