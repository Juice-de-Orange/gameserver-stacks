// Shows the active server and makes switching a deliberate step. ALWAYS shows prominently which
// server is active (themed emblem, name, state) and opens a selection dialog on tap.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, matchPath, useLocation, useNavigate } from "react-router-dom";
import { Check, ChevronsUpDown, Plus, Server } from "lucide-react";
import { useAuth } from "../auth";
import { useActiveServer, useServers } from "./ServerContext";
import { useOverview } from "./overview";
import { Modal } from "./feedback";
import { gameLabel, gameSuffix } from "../lib/game";
import { GameEmblem } from "./game/GameEmblem";
import { cx } from "./ui";

export function ServerChip({ variant = "chip" }: { variant?: "chip" | "block" }) {
  const { t } = useTranslation(["serverChip", "common"]);
  const active = useActiveServer();
  const { servers } = useServers();
  const { data } = useOverview();
  const { me } = useAuth();
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  if (!active) {
    // No enabled server (a fresh installation, or every server switched off): say so in the
    // place where the server would be, and give the admin the way to the first one.
    if (me?.role === "admin") {
      return (
        <Link
          to="/servers/new"
          className={cx(
            "inline-flex items-center gap-2 rounded-xl border border-dashed border-line px-3 text-sm text-fg-muted transition hover:border-primary/40 hover:text-fg",
            variant === "block" ? "w-full py-2.5" : "min-h-11 py-1.5 sm:min-h-9",
          )}
        >
          <Plus className="h-4 w-4 shrink-0" /> {t("add")}
        </Link>
      );
    }
    return (
      <span className="inline-flex items-center gap-2 px-1 py-1.5 text-sm text-fg-dim">
        <Server className="h-4 w-4 shrink-0" /> {t("none")}
      </span>
    );
  }

  const running = !!data?.container?.running;
  const enabled = servers.filter((s) => s.enabled).sort((a, b) => a.sort_order - b.sort_order);
  // Keep the current feature suffix when switching (e.g. /console → other server/console).
  const feat = matchPath("/s/:key/:feat", loc.pathname)?.params?.feat;

  // If the server is named like its game, "Factorio / FACTORIO" would be a repetition — then
  // this line shows the state, which would otherwise only be visible as a dot.
  const secondLine =
    gameSuffix(active) ||
    (!data?.container ? t("common:state.loading") : running ? t("common:state.running") : t("common:state.stopped"));

  const dot = (
    <span
      className={cx("inline-flex h-2 w-2 shrink-0 rounded-full", running ? "bg-success animate-live" : "bg-fg-dim")}
    />
  );

  return (
    <>
      <button
        type="button"
        data-game={active.game_type}
        onClick={() => setOpen(true)}
        title={t("switch")}
        className={cx(
          "group inline-flex items-center gap-2.5 rounded-xl border border-line bg-surface-card text-left transition hover:border-primary/40",
          variant === "block" ? "w-full px-3 py-2.5" : "min-h-11 max-w-full px-2.5 py-1.5 sm:min-h-9",
        )}
      >
        <span
          className={cx(
            "grid shrink-0 place-items-center border border-primary/30 bg-primary/[0.12] text-primary",
            variant === "block" ? "h-9 w-9" : "h-7 w-7",
          )}
          style={{ borderRadius: "var(--g-radius)" }}
        >
          <GameEmblem
            game={active.game_type}
            className={variant === "block" ? "h-5 w-5" : "h-4 w-4"}
            strokeWidth={2.2}
          />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-sm font-semibold leading-tight text-fg">{active.display_name || gameLabel(active)}</span>
            {variant === "chip" && dot}
          </span>
          <span className="g-label block truncate text-[10px] text-fg-dim">
            {variant === "block" ? (
              <span className="inline-flex items-center gap-1.5">
                {dot} {secondLine}
              </span>
            ) : (
              secondLine
            )}
          </span>
        </span>
        <ChevronsUpDown className="h-4 w-4 shrink-0 text-fg-dim transition group-hover:text-primary" />
      </button>

      {open && (
        <Modal onClose={() => setOpen(false)}>
          <h2 className="text-stencil text-lg text-fg">{t("choose")}</h2>
          <div className="space-y-1.5">
            {enabled.map((s) => {
              const on = s.key === active.key;
              return (
                <button
                  key={s.key}
                  type="button"
                  data-game={s.game_type}
                  onClick={() => {
                    setOpen(false);
                    nav(`/s/${s.key}${feat ? "/" + feat : ""}`);
                  }}
                  className={cx(
                    "flex w-full items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition",
                    on ? "border-primary/50 bg-primary-soft" : "border-line hover:bg-surface-sunken",
                  )}
                >
                  <span
                    className="grid h-9 w-9 shrink-0 place-items-center border border-primary/30 bg-primary/[0.12] text-primary"
                    style={{ borderRadius: "var(--g-radius)" }}
                  >
                    <GameEmblem game={s.game_type} className="h-5 w-5" strokeWidth={2} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-semibold text-fg">
                      {s.display_name || gameLabel(s)}
                    </span>
                    {gameSuffix(s) && (
                      <span className="block truncate text-xs text-fg-dim">{gameSuffix(s)}</span>
                    )}
                  </span>
                  {on && <Check className="h-4 w-4 shrink-0 text-primary" />}
                </button>
              );
            })}
          </div>
        </Modal>
      )}
    </>
  );
}
