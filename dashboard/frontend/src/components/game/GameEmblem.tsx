// Game emblems — own drawings, no third-party brand material.
//
// Why not generic stock icons: they are interchangeable. An emblem is meant to DENOTE a game,
// and that takes a shape of its own, not the nearest symbol from a general set. And why no real
// logos: the logos are trademarks — the same decision `DeathMap.tsx` makes for map material.
//
// Everything draws in `currentColor` on a 32×32 field, so it scales with the font size and takes
// the server colour from the surrounding `[data-game]`.
import { GameType } from "../../lib/game";

export interface GameEmblemProps {
  game: GameType | string | null | undefined;
  className?: string;
  /** For very small sizes (< 20px) a stronger line pays off. */
  strokeWidth?: number;
}

/** Project Zomboid — biohazard sign with a crack running across it. */
function Zomboid({ w }: { w: number }) {
  return (
    <>
      {[0, 120, 240].map((a) => (
        <circle key={a} cx={16} cy={9.6} r={5.6} transform={`rotate(${a} 16 16)`} strokeWidth={w} />
      ))}
      <circle cx={16} cy={16} r={2.5} fill="currentColor" stroke="none" />
      {/* The crack: it runs over the sign, not inside it — the sheet metal is no longer intact. */}
      <path d="M7.5 2.5 14 12.2 10 14.8 18.5 29.5" strokeWidth={w * 0.85} strokeLinecap="round" />
    </>
  );
}

/** Factorio — gear wheel on a belt segment. */
function FactorioMark({ w }: { w: number }) {
  const teeth = [0, 45, 90, 135, 180, 225, 270, 315];
  return (
    <>
      <circle cx={16} cy={12} r={6} strokeWidth={w} />
      <circle cx={16} cy={12} r={2.2} strokeWidth={w} />
      {teeth.map((a) => (
        <path key={a} d="M16 5.4V2.6" transform={`rotate(${a} 16 12)`} strokeWidth={w} strokeLinecap="round" />
      ))}
      {/* Belt with its running direction */}
      <path d="M3.5 24.5h25" strokeWidth={w} strokeLinecap="round" />
      <path d="M6 29.2l2.6-3.1M12 29.2l2.6-3.1M18 29.2l2.6-3.1M24 29.2l2.6-3.1"
            strokeWidth={w * 0.85} strokeLinecap="round" />
    </>
  );
}

/** Minecraft — isometric block. */
function MinecraftMark({ w }: { w: number }) {
  return (
    <>
      <path d="M16 3.5 27.5 10 16 16.5 4.5 10Z" strokeWidth={w} strokeLinejoin="round" />
      <path d="M4.5 10v12L16 28.5V16.5Z" strokeWidth={w} strokeLinejoin="round" />
      <path d="M27.5 10v12L16 28.5V16.5Z" strokeWidth={w} strokeLinejoin="round" />
      {/* Tufts of grass on the top face — the block is a grass block. */}
      <path d="M12.4 9.2v-2M16 11.1V9M19.6 9.2v-2" strokeWidth={w * 0.8} strokeLinecap="round" />
    </>
  );
}

/** Vintage Story — knapped flint with flake scars. */
function VintageMark({ w }: { w: number }) {
  return (
    <>
      <path d="M16 2.6 26.6 11.2 23 24.4 10.6 28.4 4.2 15.6Z" strokeWidth={w} strokeLinejoin="round" />
      <path d="M16 2.6 13.6 14.8 23 24.4" strokeWidth={w * 0.8} strokeLinejoin="round" />
      <path d="M4.2 15.6 13.6 14.8 10.6 28.4" strokeWidth={w * 0.8} strokeLinejoin="round" />
    </>
  );
}

/** Arma Reforger — compass rose with bearing ticks. */
function ReforgerMark({ w }: { w: number }) {
  return (
    <>
      <circle cx={16} cy={16} r={11.4} strokeWidth={w} />
      {/* Four cardinal directions long, four intercardinal ones short */}
      {[0, 90, 180, 270].map((a) => (
        <path key={a} d="M16 4.6V8.4" transform={`rotate(${a} 16 16)`} strokeWidth={w} strokeLinecap="round" />
      ))}
      {[45, 135, 225, 315].map((a) => (
        <path key={a} d="M16 4.6V6.8" transform={`rotate(${a} 16 16)`} strokeWidth={w * 0.75} strokeLinecap="round" />
      ))}
      {/* North needle */}
      <path d="M16 8.8 19.4 19.6 16 17.4 12.6 19.6Z" strokeWidth={w} strokeLinejoin="round" />
    </>
  );
}

/** Valheim — valknut, three interlocked triangles. */
function ValheimMark({ w }: { w: number }) {
  const triangle = (cx: number, cy: number, r = 6.5) => {
    const h = r * 0.866;
    return `M${cx} ${cy - r} ${cx - h} ${cy + r / 2} ${cx + h} ${cy + r / 2}Z`;
  };
  return (
    <>
      <path d={triangle(16, 10.5)} strokeWidth={w} strokeLinejoin="round" />
      <path d={triangle(11, 20)} strokeWidth={w} strokeLinejoin="round" />
      <path d={triangle(21, 20)} strokeWidth={w} strokeLinejoin="round" />
    </>
  );
}

/** Avorion — ship silhouette in a crosshair. */
function AvorionMark({ w }: { w: number }) {
  return (
    <>
      <circle cx={16} cy={16} r={12} strokeWidth={w * 0.75} strokeDasharray="4 4.6" />
      {/* Bearing ticks */}
      <path d="M16 1.2v3.4M16 27.4v3.4M1.2 16h3.4M27.4 16h3.4" strokeWidth={w} strokeLinecap="round" />
      {/* Hull */}
      <path d="M16 7.6 20.6 21 16 18.4 11.4 21Z" strokeWidth={w} strokeLinejoin="round" />
      <path d="M11.4 14.2 8.2 17.4M20.6 14.2l3.2 3.2" strokeWidth={w * 0.85} strokeLinecap="round" />
    </>
  );
}

/** Fallback for a game that is not known yet — hexagonal placeholder. */
function UnknownMark({ w }: { w: number }) {
  return (
    <>
      <path d="M16 3 27.3 9.5v13L16 29 4.7 22.5v-13Z" strokeWidth={w} strokeLinejoin="round" />
      <circle cx={16} cy={16} r={3.4} strokeWidth={w} />
    </>
  );
}

const MARKS: Record<string, (p: { w: number }) => JSX.Element> = {
  pz: Zomboid,
  factorio: FactorioMark,
  minecraft: MinecraftMark,
  vintagestory: VintageMark,
  arma_reforger: ReforgerMark,
  valheim: ValheimMark,
  avorion: AvorionMark,
};

export function GameEmblem({ game, className, strokeWidth = 1.8 }: GameEmblemProps) {
  const Mark = (game && MARKS[game]) || UnknownMark;
  return (
    <svg
      viewBox="0 0 32 32"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <Mark w={strokeWidth} />
    </svg>
  );
}
