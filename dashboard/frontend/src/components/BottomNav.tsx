import { useState } from "react";
import { useTranslation } from "react-i18next";
import { matchPath, NavLink, useLocation } from "react-router-dom";
import { LayoutGrid, Lock, LogOut, X, type LucideIcon } from "lucide-react";
import { useAuth } from "../auth";
import { useNav, type NavItem } from "./nav";
import { cx } from "./ui";

// Up to 4 direct tabs plus "More" — a lean bar, destinations one tap away.
const MAX_TABS = 4;

function Tab({ to, label, icon: Icon, end }: { to: string; label: string; icon: LucideIcon; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cx(
          "flex min-h-[3.7rem] flex-1 flex-col items-center justify-center gap-0.5 px-0.5 py-1.5 text-[10.5px] font-semibold transition",
          isActive ? "text-primary" : "text-fg-dim hover:text-fg",
        )
      }
    >
      {({ isActive }) => (
        <>
          <span
            className={cx(
              "flex h-7 w-11 items-center justify-center rounded-full transition",
              isActive ? "bg-primary-soft" : "bg-transparent",
            )}
          >
            <Icon
              className={cx("h-[1.15rem] w-[1.15rem]", isActive ? "text-primary" : "text-fg-muted")}
              strokeWidth={isActive ? 2.4 : 2}
            />
          </span>
          <span className="max-w-full truncate leading-none">{label}</span>
        </>
      )}
    </NavLink>
  );
}

/** Bottom tab bar (mobile only). CONTEXT-DEPENDENT: on a server its tabs directly, otherwise the
 * global destinations. The rest is in the "More" sheet (full navigation). */
export function BottomNav() {
  const { t } = useTranslation(["bottomNav", "common"]);
  const groups = useNav();
  const { me, logout } = useAuth();
  const loc = useLocation();
  const [more, setMore] = useState(false);

  const allItems: NavItem[] = groups.flatMap((g) => g.items);
  const onServer = !!matchPath("/s/:serverKey/*", loc.pathname);
  const serverItems = allItems.filter((i) => i.to.startsWith("/s/"));
  const globalItems = allItems.filter((i) => !i.to.startsWith("/s/"));
  // On a server → its tabs are primary; otherwise the global ones. Greyed-out entries appear in
  // the "More" sheet only.
  const source = onServer && serverItems.length ? serverItems : globalItems;
  const primary = source.filter((i) => !i.disabled).slice(0, MAX_TABS);

  return (
    <>
      <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t border-line bg-surface-raised pb-[env(safe-area-inset-bottom)] lg:hidden">
        {primary.map((i) => (
          <Tab key={i.to} to={i.to} label={i.label} icon={i.icon} end={i.to === "/" || /^\/s\/[^/]+$/.test(i.to)} />
        ))}
        <button
          onClick={() => setMore(true)}
          aria-label={t("more")}
          className="flex min-h-[3.7rem] flex-1 flex-col items-center justify-center gap-0.5 px-0.5 py-1.5 text-[10.5px] font-semibold text-fg-dim transition hover:text-fg"
        >
          <span className="flex h-7 w-11 items-center justify-center rounded-full">
            <LayoutGrid className="h-[1.15rem] w-[1.15rem] text-fg-muted" strokeWidth={2} />
          </span>
          <span className="leading-none">{t("more")}</span>
        </button>
      </nav>

      {more && (
        <div className="fixed inset-0 z-[80] flex items-end bg-black/70 lg:hidden" onClick={() => setMore(false)}>
          <div
            onClick={(e) => e.stopPropagation()}
            className="animate-sheet max-h-[85vh] w-full overflow-y-auto rounded-t-2xl border-t border-line bg-surface-raised pb-[env(safe-area-inset-bottom)] shadow-panel"
          >
            <div className="sticky top-0 flex items-center justify-between border-b border-line-soft bg-surface-raised px-4 py-3">
              <span className="text-stencil text-base text-fg">{t("title")}</span>
              <button
                onClick={() => setMore(false)}
                aria-label={t("common:action.close")}
                className="flex h-11 w-11 items-center justify-center text-fg-dim hover:text-fg"
              >
                <X className="h-7 w-7" />
              </button>
            </div>
            <div className="space-y-4 p-3">
              {groups.map((g) => (
                <div key={g.title}>
                  <div className="px-1 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.2em] text-fg-dim">
                    {g.title}
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {g.items.map((i) =>
                      i.disabled ? (
                        <div
                          key={i.to}
                          title={i.disabledReason}
                          aria-disabled="true"
                          className="flex min-h-[3.75rem] cursor-not-allowed select-none items-center gap-3 rounded-xl border border-line bg-surface-card/50 px-3 text-base text-fg-dim/60"
                        >
                          <i.icon className="h-6 w-6 shrink-0 opacity-60" />
                          <span className="flex-1 truncate">{i.label}</span>
                          <Lock className="h-4 w-4 shrink-0 opacity-70" />
                        </div>
                      ) : (
                        <NavLink
                          key={i.to}
                          to={i.to}
                          end={i.to === "/" || /^\/s\/[^/]+$/.test(i.to)}
                          onClick={() => setMore(false)}
                          className={({ isActive }) =>
                            cx(
                              "flex min-h-[3.75rem] items-center gap-3 rounded-xl border px-3 text-base transition",
                              isActive
                                ? "border-primary bg-primary-soft font-semibold text-fg"
                                : "border-line bg-surface-card/50 text-fg-muted hover:bg-surface-sunken hover:text-fg",
                            )
                          }
                        >
                          <i.icon className="h-6 w-6 shrink-0" />
                          <span className="truncate">{i.label}</span>
                        </NavLink>
                      ),
                    )}
                  </div>
                </div>
              ))}
              <div className="flex items-center justify-between gap-2 border-t border-line-soft px-1 pt-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-fg">{me?.display_name}</div>
                  <div className="text-xs text-fg-dim">@{me?.username}</div>
                </div>
                <button
                  onClick={() => {
                    setMore(false);
                    logout();
                  }}
                  className="flex min-h-[3rem] items-center gap-2 rounded-md border border-line px-4 text-sm text-fg-muted hover:bg-surface-card hover:text-fg"
                >
                  <LogOut className="h-5 w-5" /> {t("common:action.signOut")}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
