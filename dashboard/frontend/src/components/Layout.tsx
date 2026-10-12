import { ReactNode, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, useLocation } from "react-router-dom";
import { Lock, LogOut, Moon, Sun } from "lucide-react";
import { BrandMark } from "./BrandMark";
import { ErrorBoundary } from "./ErrorBoundary";
import { useAuth } from "../auth";
import { Badge, cx } from "./ui";
import { StatusStrip } from "./StatusStrip";
import { BottomNav } from "./BottomNav";
import { DockToggle } from "./liveconsole";
import { LiveDock } from "./console/LiveDock";
import { ServerChip } from "./ServerChip";
import { useActiveServer } from "./ServerContext";
import { useNav } from "./nav";
import { APP_NAME, appSub } from "../lib/brand";
import { toggleTheme, useTheme } from "../lib/theme";
import { LANGUAGES, currentLanguage, setLanguage } from "../i18n";

function Brand({ compact = false }: { compact?: boolean }) {
  useTranslation(); // re-render the tagline when the language changes
  return (
    <Link to="/" className="flex items-center gap-2.5">
      <span
        className={cx(
          "grid shrink-0 place-items-center rounded bg-primary-soft text-primary",
          compact ? "h-8 w-8" : "h-9 w-9",
        )}
      >
        <BrandMark className="h-5 w-5" />
      </span>
      <div className="leading-none">
        <div className={cx("text-stencil text-fg", compact ? "text-base" : "text-lg")}>{APP_NAME}</div>
        {!compact && (
          <div className="text-[10px] uppercase tracking-[0.25em] text-fg-dim">{appSub()}</div>
        )}
      </div>
    </Link>
  );
}

/** Light/dark switch in the header. */
function ThemeToggle() {
  const { t } = useTranslation();
  const dark = useTheme() === "dark";
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={dark ? t("theme.switchToLight") : t("theme.switchToDark")}
      title={dark ? t("theme.light") : t("theme.dark")}
      className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-line text-fg-muted transition duration-200 hover:bg-surface-sunken hover:text-fg sm:h-9 sm:w-9"
    >
      {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  );
}

/** Language switch in the header: shows the language a click switches to. */
function LanguageToggle() {
  const { t } = useTranslation();
  const current = currentLanguage();
  const next = LANGUAGES[(LANGUAGES.indexOf(current) + 1) % LANGUAGES.length];
  const label = t("language.switch", { language: t(`language.${next}`) });
  return (
    <button
      type="button"
      onClick={() => setLanguage(next)}
      aria-label={label}
      title={label}
      lang={next}
      className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-line text-xs font-semibold uppercase text-fg-muted transition duration-200 hover:bg-surface-sunken hover:text-fg sm:h-9 sm:w-9"
    >
      {next}
    </button>
  );
}

function SidebarBody() {
  const { t } = useTranslation();
  const { me, logout } = useAuth();
  const groups = useNav();
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-line-soft px-4 py-4">
        <Brand />
      </div>
      {/* Server head: shows the active server (emblem, name, state) and opens the selection. */}
      <div className="border-b border-line-soft px-3 py-3">
        <ServerChip variant="block" />
      </div>
      <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
        {groups.map((g) => (
          <div key={g.title}>
            <div className="px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.2em] text-fg-dim">
              {g.title}
            </div>
            <div className="space-y-0.5">
              {g.items.map((i) =>
                i.disabled ? (
                  <span
                    key={i.to}
                    title={i.disabledReason}
                    aria-disabled="true"
                    className="flex cursor-not-allowed select-none items-center gap-3 rounded-xl border-l-2 border-transparent px-3 py-2 text-sm text-fg-dim/60"
                  >
                    <i.icon className="h-4 w-4 shrink-0 opacity-60" />
                    <span className="flex-1 truncate">{i.label}</span>
                    <Lock className="h-3 w-3 shrink-0 opacity-70" />
                  </span>
                ) : (
                  <NavLink
                    key={i.to}
                    to={i.to}
                    // `end` for the server group root `/s/:key` (overview) as well — otherwise the
                    // prefix also matches /s/:key/console etc. and two entries are active at once.
                    end={i.to === "/" || /^\/s\/[^/]+$/.test(i.to)}
                    className={({ isActive }) =>
                      cx(
                        "flex items-center gap-3 rounded-xl border-l-2 px-3 py-2 text-sm transition duration-200",
                        isActive
                          ? "border-primary bg-primary-soft font-semibold text-fg"
                          : "border-transparent text-fg-muted hover:bg-surface-sunken hover:text-fg",
                      )
                    }
                  >
                    <i.icon className="h-4 w-4 shrink-0" />
                    {i.label}
                  </NavLink>
                ),
              )}
            </div>
          </div>
        ))}
      </nav>
      <div className="border-t border-line-soft p-3">
        <div className="mb-2 flex items-center justify-between gap-2 px-1">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-fg">{me?.display_name}</div>
            <div className="text-xs text-fg-dim">@{me?.username}</div>
          </div>
          {me && <Badge tone={me.role === "admin" ? "blood" : "info"}>{me.role}</Badge>}
        </div>
        <button
          onClick={logout}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-fg-muted transition hover:bg-surface-card hover:text-fg"
        >
          <LogOut className="h-4 w-4" /> {t("action.signOut")}
        </button>
      </div>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const groups = useNav();
  const active = useActiveServer();
  const { i18n } = useTranslation();
  // The language is a dependency: the tab title has to follow the switch like every label.
  useEffect(() => {
    const item = groups.flatMap((g) => g.items).find((i) => i.to === loc.pathname);
    document.title = item ? `${item.label} · ${APP_NAME}` : `${APP_NAME} · ${appSub()}`;
  }, [loc.pathname, i18n.language]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    // data-game tints the whole app for the active server (accent variables in index.css).
    <div className="min-h-screen lg:flex" data-game={active?.game_type}>
      {/* Sidebar (Desktop) */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 border-r border-line bg-surface-raised lg:block">
        <SidebarBody />
      </aside>

      <div className="flex min-h-screen flex-1 flex-col">
        {/* Top bar: the active-server chip (switch) on mobile, the live status strip on desktop. */}
        <header className="sticky top-0 z-40 border-b border-line bg-surface-raised">
          <div className="flex items-center gap-2.5 px-3 py-2 sm:px-4 sm:py-2.5">
            {/* Mobile: active server + switch (desktop has the server head in the sidebar). */}
            <div className="min-w-0 flex-1 lg:hidden">
              <ServerChip />
            </div>
            {/* Desktop: live status of the active server. */}
            <div className="hidden min-w-0 flex-1 lg:block">
              <StatusStrip />
            </div>
            <LanguageToggle />
            <ThemeToggle />
            <DockToggle />
          </div>
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 pt-6 pb-24 lg:pb-8">
          <ErrorBoundary key={loc.pathname}>
            <div className="animate-fade-in">{children}</div>
          </ErrorBoundary>
        </main>
      </div>

      {/* Dockable live console: desktop = right column in the flow, mobile = fixed sheet */}
      <LiveDock />

      {/* Bottom tab bar (mobile) */}
      <BottomNav />
    </div>
  );
}
