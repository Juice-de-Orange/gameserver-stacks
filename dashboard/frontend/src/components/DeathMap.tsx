/**
 * Map of death locations — a density grid over the world coordinates.
 *
 * The coordinates come from the game's user log (`user <name> died at (x,y,z) (non pvp)`), not
 * from the container log.
 *
 * Deliberately WITHOUT a map image: Project Zomboid ships no freely licensed map material, and a
 * grid over the real game cells says the same. One square = one PZ cell (300×300 tiles) — the
 * same division the mod conflict check uses.
 */
import { useTranslation } from "react-i18next";

const CELL = 300; // tile width of a PZ cell

export type Death = { username: string; at: string; x: number; y: number; z: number; pvp: boolean };

export function DeathMap({ deaths }: { deaths: Death[] }) {
  const { t } = useTranslation("deathMap");
  if (!deaths.length) {
    return <p className="text-sm text-fg-dim">{t("empty")}</p>;
  }

  // Show only the section that is actually played in (with one cell of margin) — the whole map
  // would be almost empty and the single dots in it invisible.
  const cells = deaths.map((d) => ({ cx: Math.floor(d.x / CELL), cy: Math.floor(d.y / CELL), d }));
  const minX = Math.min(...cells.map((c) => c.cx)) - 1;
  const maxX = Math.max(...cells.map((c) => c.cx)) + 1;
  const minY = Math.min(...cells.map((c) => c.cy)) - 1;
  const maxY = Math.max(...cells.map((c) => c.cy)) + 1;

  const counts = new Map<string, Death[]>();
  for (const c of cells) {
    const k = `${c.cx},${c.cy}`;
    counts.set(k, [...(counts.get(k) ?? []), c.d]);
  }
  const max = Math.max(...Array.from(counts.values(), (v) => v.length));

  const cols = maxX - minX + 1;
  const rowCount = maxY - minY + 1;

  return (
    <div className="space-y-2">
      <div className="-mx-1 overflow-x-auto px-1 pb-1">
        <div className="inline-flex min-w-max flex-col gap-0.5">
          {Array.from({ length: rowCount }).map((_, r) => (
            <div key={r} className="flex gap-0.5">
              {Array.from({ length: cols }).map((_, c) => {
                const cx = minX + c;
                const cy = minY + r;
                const here = counts.get(`${cx},${cy}`) ?? [];
                const a = here.length === 0 ? 0 : 0.2 + 0.8 * (here.length / max);
                const who = Array.from(new Set(here.map((d) => d.username))).slice(0, 4).join(", ");
                return (
                  <div
                    key={c}
                    className="h-3.5 w-3.5 rounded-[3px]"
                    style={{
                      background:
                        here.length === 0
                          ? "rgb(var(--ink-700) / 0.6)"
                          : `rgb(var(--danger) / ${a})`,
                    }}
                    title={
                      here.length === 0
                        ? t("cell.none", { x: cx, y: cy })
                        : t("cell.deaths", { x: cx, y: cy, count: here.length, names: who })
                    }
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <p className="text-xs text-fg-dim">{t("legend", { minX, maxX, minY, maxY, count: deaths.length })}</p>
    </div>
  );
}
