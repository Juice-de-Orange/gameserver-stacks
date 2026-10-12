// A sparkline, used in two places: in the history charts and in every row of the fleet page.
//
// **The colour comes from `currentColor`, not from a list in this file.** Hard-wired hex values
// are not class names, so no theme change and no guard would ever catch them. To colour the
// line, set a text colour on the surrounding element — it then follows light/dark and the
// server accent by itself.
import { useTranslation } from "react-i18next";
import { cx } from "./ui";

export function Sparkline({
  values,
  className,
  /** Upper bound of the scale. If omitted: the maximum of the values (relative display). */
  max,
  /** Text shown while there are fewer than two values. If omitted: the default hint. */
  emptyText,
}: {
  values: number[];
  className?: string;
  max?: number;
  emptyText?: string;
}) {
  const { t } = useTranslation("sparkline");
  const W = 300;
  const H = 64;
  const pad = 4;
  if (values.length < 2) {
    return (
      <div className={cx("flex items-center justify-center text-xs text-fg-dim", className)}>{emptyText ?? t("empty")}</div>
    );
  }
  const ceiling = Math.max(1, max ?? Math.max(...values));
  const stepX = (W - pad * 2) / (values.length - 1);
  const y = (v: number) => H - pad - (Math.min(v, ceiling) / ceiling) * (H - pad * 2);
  const pts = values.map((v, i) => [pad + i * stepX, y(v)] as const);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L ${pts[pts.length - 1][0].toFixed(1)} ${H - pad} L ${pts[0][0].toFixed(1)} ${H - pad} Z`;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className={cx("w-full", className)}
      aria-hidden="true"
    >
      <path d={area} fill="currentColor" fillOpacity={0.14} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
