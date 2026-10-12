import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LineChart } from "lucide-react";
import { apiJson } from "../api";
import { Card, cx } from "./ui";
import { Sparkline } from "./Sparkline";
import { useDefaultServerKey } from "./ServerContext";

interface Sample {
  t: string;
  players: number;
  cpu: number;
  mem_pct: number | null;
  running: boolean;
}

// `label` is a catalog key (namespace `historyCharts`), translated when rendering.
const RANGES = [
  { h: 24, label: "range.24h" },
  { h: 168, label: "range.7d" },
];

function Metric({
  title,
  values,
  unit,
  tone,
}: {
  title: string;
  values: number[];
  unit: string;
  /** Text colour class; the sparkline draws in `currentColor`. */
  tone: string;
}) {
  const cur = values.length ? values[values.length - 1] : null;
  const peak = values.length ? Math.max(...values) : null;
  return (
    <div className="rounded-md border border-line-soft bg-surface-card/40 p-3">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-fg-dim">{title}</span>
        <span className="font-mono text-sm text-fg">
          {cur != null ? `${cur}${unit}` : "—"}
          {peak != null && (
            <span className="ml-2 text-xs text-fg-dim">
              ↑{peak}
              {unit}
            </span>
          )}
        </span>
      </div>
      <div className={tone}>
        <Sparkline values={values} className="h-16" />
      </div>
    </div>
  );
}

/** History charts (players/RAM/CPU) — self-contained, polls /api/servers/<default server>/history. */
export function HistoryCharts() {
  const { t } = useTranslation(["historyCharts", "common"]);
  const [hours, setHours] = useState(24);
  const [rows, setRows] = useState<Sample[]>([]);
  const key = useDefaultServerKey();

  useEffect(() => {
    if (!key) {
      setRows([]);
      return;
    }
    let alive = true;
    const load = () =>
      apiJson<Sample[]>(`/servers/${key}/history?hours=${hours}`)
        .then((d) => alive && setRows(d))
        .catch(() => {});
    load();
    const t = window.setInterval(load, 60000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [hours, key]);

  const players = rows.map((r) => r.players);
  const mem = rows.map((r) => r.mem_pct ?? 0);
  const cpu = rows.map((r) => r.cpu);

  return (
    <Card className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
          <LineChart className="h-4 w-4" /> {t("title")}
        </div>
        <div className="flex gap-1 rounded-md border border-line-soft bg-surface-card/50 p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.h}
              onClick={() => setHours(r.h)}
              className={cx(
                "rounded px-2.5 py-1 text-xs font-medium transition",
                hours === r.h ? "bg-surface-sunken text-fg" : "text-fg-muted hover:text-fg",
              )}
            >
              {t(r.label)}
            </button>
          ))}
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Metric title={t("common:word.players")} values={players} unit="" tone="text-success" />
        <Metric title="RAM" values={mem} unit="%" tone="text-danger" />
        <Metric title="CPU" values={cpu} unit="%" tone="text-warning" />
      </div>
    </Card>
  );
}
