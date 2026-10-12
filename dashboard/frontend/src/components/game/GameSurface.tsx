// The identity zone: the ONLY place that reads the `--g-*` art-style tokens from `index.css`.
//
// Why "only place": the same rule in several places is how copies drift apart. Pattern, vignette,
// grain, corners and edge shape therefore belong here — a page that applies a texture of its own
// "just quickly" has already started building the second copy.
import { ReactNode } from "react";
import { cx } from "../ui";

export interface GameSurfaceProps {
  /** `game_type` of the server. Sets `data-game` itself so that the surface also works where
   *  several games sit side by side (fleet table) — not only below the shell root. */
  game?: string | null;
  className?: string;
  children?: ReactNode;
  /** Corner ornament (rivets for Factorio, brackets for Avorion/Reforger). */
  corners?: boolean;
  /** Grain. On very small surfaces (chip, table row) it is just noise. */
  grain?: boolean;
  /** Pattern. Off when the surface is narrower than the pattern. */
  pattern?: boolean;
}

export function GameSurface({
  game,
  className,
  children,
  corners = true,
  grain = true,
  pattern = true,
}: GameSurfaceProps) {
  return (
    <div data-game={game || undefined} className={cx("g-surface", className)}>
      {pattern && <div className="g-layer g-pattern" />}
      <div className="g-layer g-vignette" />
      {grain && <div className="g-layer g-grain" />}
      {corners && <div className="g-layer g-corners" />}
      <div className="g-content">{children}</div>
    </div>
  );
}

/** The keyline of the game — hazard tape for Zomboid, conveyor belt for Factorio, notches for Valheim. */
export function GameRule({ game, className }: { game?: string | null; className?: string }) {
  return <div data-game={game || undefined} className={cx("g-rule", className)} aria-hidden="true" />;
}
