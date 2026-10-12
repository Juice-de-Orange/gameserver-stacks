import {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { useTranslation } from "react-i18next";
import { Lock, type LucideIcon } from "lucide-react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Panel/card — a soft, matt surface with a subtle border and shadow. */
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cx(
        "rounded-2xl border border-line bg-surface-card p-5 shadow-panel",
        className,
      )}
    >
      {children}
    </div>
  );
}

type BtnTone = "primary" | "danger" | "toxic";

/**
 * The reason a control does not work — greyed out INSTEAD of hidden.
 *
 * Principle (see `lib/features.ts`): role hides, game capability greys out. A hidden button for
 * a subset of an existing capability reads wrong: "unban" is missing and looks like "does not
 * exist".
 *
 * The detour via a `<span>` is necessary, not pretty: a `<button disabled>` receives no mouse
 * events in most browsers, so its `title` never shows. The navigation (`Layout.tsx`,
 * `BottomNav.tsx`) solves it the same way.
 */
function LockedShell({
  reason,
  className = "",
  children,
}: {
  reason: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={reason}
      aria-disabled="true"
      // No `role="button"`/`tabIndex`, same as in the navigation: a focusable element that reacts
      // to nothing is a dead end for the keyboard — and the native `title` does not show on focus
      // anyway.
      className={cx(
        "inline-flex cursor-not-allowed select-none items-center justify-center gap-2 rounded-xl",
        "border border-line px-3 py-1.5 text-sm text-fg-dim/60 opacity-70",
        "min-h-11 sm:min-h-9",
        className,
      )}
    >
      <Lock className="h-3 w-3 shrink-0" />
      {children}
    </span>
  );
}

export function Button({
  className = "",
  tone = "primary",
  disabledReason,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: BtnTone; disabledReason?: string }) {
  const tones: Record<BtnTone, string> = {
    primary: "bg-primary-btn text-primary-fg border-transparent shadow-sm hover:brightness-[1.06] hover:shadow active:translate-y-px",
    danger: "bg-danger text-white border-transparent shadow-sm hover:brightness-[1.06] active:translate-y-px",
    toxic: "border-success/40 bg-success/10 text-success-text hover:bg-success/20 active:translate-y-px",
  };
  if (disabledReason) {
    return <LockedShell reason={disabledReason} className={className}>{children}</LockedShell>;
  }
  return (
    <button
      {...props}
      className={cx(
        "inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border px-4 py-2 text-sm font-semibold",
        "transition duration-200 disabled:opacity-50 disabled:active:translate-y-0 sm:min-h-9",
        tones[tone],
        className,
      )}
    >
      {children}
    </button>
  );
}

export function GhostButton({
  className = "",
  disabledReason,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { disabledReason?: string }) {
  if (disabledReason) {
    return <LockedShell reason={disabledReason} className={className}>{children}</LockedShell>;
  }
  return (
    <button
      {...props}
      className={cx(
        "inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-line px-3 py-1.5 text-sm text-fg-muted sm:min-h-9",
        "transition duration-200 hover:border-fg-dim hover:bg-surface-sunken hover:text-fg disabled:opacity-50",
        className,
      )}
    >
      {children}
    </button>
  );
}

/** Switch (on/off) styled as a button with track and knob. Large touch target. */
export function Toggle({
  checked,
  onChange,
  label,
  icon: Icon,
  title,
  className = "",
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  icon?: LucideIcon;
  title?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={title || label}
      title={title}
      onClick={() => onChange(!checked)}
      className={cx(
        "inline-flex min-h-11 shrink-0 items-center gap-2 rounded-xl border px-3 text-sm font-medium transition duration-200 sm:min-h-9",
        checked
          ? "border-primary/40 bg-primary-soft text-primary-text"
          : "border-line text-fg-muted hover:border-fg-dim hover:bg-surface-sunken hover:text-fg",
        className,
      )}
    >
      {Icon && <Icon className="h-4 w-4" />}
      {label && <span className="hidden sm:inline">{label}</span>}
      <span
        className={cx(
          "inline-flex h-4 w-7 items-center rounded-full border px-0.5 transition",
          checked ? "justify-end border-primary/40 bg-primary/30" : "justify-start border-line-strong bg-surface-sunken",
        )}
      >
        <span className={cx("h-3 w-3 rounded-full transition", checked ? "bg-primary" : "bg-fg-dim")} />
      </span>
    </button>
  );
}

const fieldBase =
  "w-full rounded-xl border border-line bg-surface-raised px-3 py-2 text-sm text-fg outline-none " +
  "placeholder:text-fg-dim transition focus:border-primary focus:ring-2 focus:ring-primary/25";

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx("min-h-11 sm:min-h-9", fieldBase, className)} />;
}

export function Textarea({ className = "", ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(fieldBase, className)} />;
}

export function Select({
  className = "",
  children,
  disabledReason,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement> & { disabledReason?: string }) {
  const { t } = useTranslation("ui");
  if (disabledReason) {
    return (
      <LockedShell reason={disabledReason} className={cx("px-2.5 py-2", className)}>
        {props["aria-label"] || t("select.unavailable")}
      </LockedShell>
    );
  }
  return (
    <select
      {...props}
      className={cx(
        "min-h-11 rounded-xl border border-line bg-surface-raised px-2.5 py-2 text-sm text-fg outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/25 sm:min-h-9",
        className,
      )}
    >
      {children}
    </select>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium uppercase tracking-wide text-fg-muted">{label}</span>
      {children}
      {hint && <span className="block text-xs text-fg-dim">{hint}</span>}
    </label>
  );
}

export function Alert({
  kind = "error",
  children,
}: {
  kind?: "error" | "info" | "success" | "warn";
  children: ReactNode;
}) {
  const styles = {
    error: "border-danger-border bg-danger-soft text-danger-text",
    success: "border-success/40 bg-success-soft text-success-text",
    warn: "border-warning/40 bg-warning/10 text-warning-text",
    info: "border-line bg-surface-sunken text-fg-muted",
  }[kind];
  return <div className={cx("rounded-xl border px-3 py-2 text-sm", styles)}>{children}</div>;
}

/** Page/section heading — a title above a continuous edge.
 *
 * The continuous line makes the structure of the page visible, and the short strong piece at its
 * start shows where a section begins.
 */
export function SectionTitle({
  icon: Icon,
  children,
  right,
  sub,
}: {
  icon?: LucideIcon;
  children: ReactNode;
  right?: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-center gap-2.5">
          {Icon && <Icon className="h-5 w-5 shrink-0 text-primary" strokeWidth={2} />}
          <div>
            <h1 className="text-stencil text-[1.6rem] leading-none text-fg">{children}</h1>
            {sub && <div className="mt-1.5 text-sm text-fg-muted">{sub}</div>}
          </div>
        </div>
        {right && <div className="flex flex-wrap items-center gap-2">{right}</div>}
      </div>
      <div className="mt-3 h-px w-full bg-line">
        <div className="h-px w-10 bg-primary" />
      </div>
    </div>
  );
}

type Tone = "online" | "offline" | "warn" | "info" | "toxic" | "blood";

export function Badge({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  const styles: Record<Tone, string> = {
    online: "border-success/40 bg-success-soft text-success-text",
    toxic: "border-success/40 bg-success-soft text-success-text",
    offline: "border-line bg-surface-sunken text-fg-dim",
    warn: "border-warning/40 bg-warning/10 text-warning-text",
    blood: "border-danger-border bg-danger-soft text-danger-text",
    info: "border-line bg-surface-sunken text-fg-muted",
  };
  return (
    <span
      className={cx(
        // Angular instead of a pill: the tool should look like a display, not like a label.
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-semibold",
        "uppercase tracking-[0.08em]",
        styles[tone],
      )}
    >
      {children}
    </span>
  );
}

/** Key figure tile for status grids. */
export function StatTile({
  label,
  value,
  sub,
  icon: Icon,
  tone = "bone",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: LucideIcon;
  tone?: "bone" | "toxic" | "blood" | "rust";
}) {
  const valTone = {
    bone: "text-fg",
    toxic: "text-success-text",
    blood: "text-danger-text",
    rust: "text-warning-text",
  }[tone];
  return (
    <Card className="!p-4 transition duration-200 hover:border-primary/30">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-fg-muted">{label}</div>
        {Icon && <Icon className="h-4 w-4 text-primary/70" />}
      </div>
      <div className={cx("mt-1.5 font-mono text-2xl font-semibold leading-tight tabular-nums", valTone)}>
        {value}
      </div>
      {sub && <div className="mt-0.5 truncate text-xs text-fg-dim">{sub}</div>}
    </Card>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={cx(
        "inline-block h-4 w-4 animate-spin rounded-full border-2 border-fg-dim border-t-primary",
        className,
      )}
    />
  );
}

export function EmptyState({ icon: Icon, children }: { icon?: LucideIcon; children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center text-sm text-fg-dim">
      {Icon && <Icon className="h-8 w-8 opacity-50" />}
      <div>{children}</div>
    </div>
  );
}

/** Loading placeholder (pulse) — instead of a "Loading…" text. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-xl bg-surface-sunken/70", className)} />;
}

// ---------------------------------------------------------------- DataTable
export interface Column<T> {
  header: string;
  cell: (row: T) => ReactNode;
  /** Extra classes for the desktop cell/header (e.g. text-right). */
  className?: string;
  /** Hide in the mobile card (e.g. redundant or long fields). */
  mobileHidden?: boolean;
}

/**
 * Responsive table: a real `<table>` from `sm` up, a list of cards (label/value pairs) below —
 * no horizontal scrolling on a phone. One column definition, both layouts. `actions` renders
 * buttons per row.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  actions,
  empty,
  onRowClick,
  rowAttrs,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string | number;
  actions?: (row: T) => ReactNode;
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  /** Extra attributes per row — e.g. `data-game`, so that a row takes on the colour of its
   *  game. Applies to the table row AND the mobile card, so that both layouts look the same. */
  rowAttrs?: (row: T) => Record<string, string | undefined>;
}) {
  if (rows.length === 0) return <>{empty ?? null}</>;
  return (
    <>
      {/* Desktop / Tablet */}
      <div className="hidden overflow-x-auto rounded-2xl border border-line sm:block">
        <table className="w-full text-sm">
          <thead className="bg-surface-sunken/60 text-left text-fg-dim">
            <tr>
              {columns.map((c) => (
                <th key={c.header} className={cx("p-2.5 font-medium", c.className)}>
                  {c.header}
                </th>
              ))}
              {actions && <th className="p-2.5" />}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr
                key={rowKey(r, i)}
                {...(rowAttrs?.(r) ?? {})}
                onClick={onRowClick ? () => onRowClick(r) : undefined}
                className={cx("border-t border-line", onRowClick && "cursor-pointer hover:bg-surface-sunken/50")}
              >
                {columns.map((c) => (
                  <td key={c.header} className={cx("p-2.5 align-middle", c.className)}>
                    {c.cell(r)}
                  </td>
                ))}
                {actions && (
                  <td className="p-2.5" onClick={(e) => e.stopPropagation()}>
                    <div className="flex justify-end gap-1.5">{actions(r)}</div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile: cards */}
      <div className="space-y-2 sm:hidden">
        {rows.map((r, i) => (
          <div
            key={rowKey(r, i)}
            {...(rowAttrs?.(r) ?? {})}
            onClick={onRowClick ? () => onRowClick(r) : undefined}
            className={cx(
              "rounded-2xl border border-line bg-surface-card p-3",
              onRowClick && "cursor-pointer active:bg-surface-sunken",
            )}
          >
            {columns
              .filter((c) => !c.mobileHidden)
              .map((c) => (
                <div key={c.header} className="flex items-baseline justify-between gap-3 py-0.5">
                  <span className="shrink-0 text-xs uppercase tracking-wide text-fg-dim">{c.header}</span>
                  <span className="min-w-0 break-words text-right text-sm text-fg">{c.cell(r)}</span>
                </div>
              ))}
            {actions && (
              <div
                className="mt-2 flex flex-wrap justify-end gap-2 border-t border-line pt-2"
                onClick={(e) => e.stopPropagation()}
              >
                {actions(r)}
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

// --- Multi-game primitives ---

export interface TabItem {
  key: string;
  label: ReactNode;
  badge?: ReactNode;
}

/** Segmented tab bar (config tabs, …). */
export function Tabs({
  items,
  active,
  onSelect,
  className = "",
}: {
  items: TabItem[];
  active: string;
  onSelect: (key: string) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cx("flex flex-wrap gap-1 rounded-xl border border-line bg-surface-sunken/60 p-1", className)}
    >
      {items.map((it) => {
        const on = it.key === active;
        return (
          <button
            key={it.key}
            role="tab"
            aria-selected={on}
            onClick={() => onSelect(it.key)}
            className={cx(
              "inline-flex min-h-11 items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-sm font-semibold transition duration-200 sm:min-h-9",
              on
                ? "bg-surface-card text-primary-text shadow-sm ring-1 ring-primary/25"
                : "text-fg-muted hover:bg-surface-card/60 hover:text-fg",
            )}
          >
            {it.label}
            {it.badge}
          </button>
        );
      })}
    </div>
  );
}

/** Range slider (RAM/CPU) with a label and the current value. */
export function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  unit = "",
  hint,
  disabled = false,
}: {
  label?: ReactNode;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  unit?: string;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        {label ? <span className="text-sm font-medium text-fg-muted">{label}</span> : <span />}
        <span className="font-mono text-sm text-fg">
          {value}
          {unit}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-2 w-full cursor-pointer appearance-none rounded-full bg-surface-sunken accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
      {hint && <div className="mt-0.5 text-xs text-fg-dim">{hint}</div>}
    </div>
  );
}

/** Usage/progress bar (shared cap, RAM usage). */
export function ResourceBar({
  used,
  total,
  label,
  unit = "MB",
  warnPct = 85,
}: {
  used: number;
  total: number;
  label?: ReactNode;
  unit?: string;
  warnPct?: number;
}) {
  const { t } = useTranslation("ui");
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const tone =
    total <= 0 ? "bg-line-strong" : pct >= 100 ? "bg-danger" : pct >= warnPct ? "bg-warning" : "bg-primary";
  return (
    <div>
      {label && (
        <div className="mb-1 flex items-baseline justify-between text-sm">
          <span className="text-fg-muted">{label}</span>
          <span className="font-mono text-fg-dim">
            {used}
            {total > 0 ? ` / ${total}` : ""} {unit}
            {total > 0 ? ` (${pct}%)` : ` (${t("resourceBar.noCap")})`}
          </span>
        </div>
      )}
      <div className="h-2.5 w-full overflow-hidden rounded-full border border-line bg-surface-sunken">
        <div
          className={cx("h-full transition-all duration-300", tone)}
          style={{ width: total > 0 ? `${pct}%` : "0%" }}
        />
      </div>
    </div>
  );
}
