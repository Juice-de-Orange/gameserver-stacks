// Shared formatting helpers. Everything that is text or locale-dependent is resolved at call
// time, in the active language — nothing here is cached at module level.
import i18n, { currentLanguage } from "../i18n";

export function fmtDuration(sec?: number | null): string {
  if (sec == null || sec < 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return i18n.t("format:duration.dh", { d, h });
  if (h > 0) return i18n.t("format:duration.hm", { h, m });
  return i18n.t("format:duration.m", { m });
}

/** Compact "Nh Nm" for play time in tables. */
export function fmtHm(sec?: number | null): string {
  if (sec == null || sec < 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

export function fmtBytes(b?: number | null): string {
  if (b == null) return "—";
  if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  return `${(b / 1e3).toFixed(0)} kB`;
}

export function fmtPct(n?: number | null): string {
  return n == null ? "—" : `${Math.round(n)}%`;
}

/** Amount of RAM given in MB, compact (budget and reservation displays): GB from 1 GB up. */
export function fmtMb(mb?: number | null): string {
  if (mb == null) return "—";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

/** A whole number as it is, anything else with one decimal, in the active language. */
function oneDecimal(n: number): string {
  return Number.isInteger(n)
    ? String(n)
    : n.toLocaleString(currentLanguage(), { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

/** Minutes as a time span in plain words — "28800" tells nobody anything, "20 days" does. */
export function fmtSpan(min?: number | null): string {
  if (min == null || min < 0) return "—";
  if (min < 60) {
    const m = Math.round(min);
    return i18n.t("format:span.minutes", { count: m, n: m });
  }
  if (min < 1440) {
    const h = min / 60;
    return i18n.t("format:span.hours", { count: h, n: oneDecimal(h) });
  }
  const t = min / 1440;
  return i18n.t("format:span.days", { count: t, n: oneDecimal(t) });
}

/** Remaining time until a deadline — coarse on purpose, not to the minute.
 *
 * Used for the time left until an empty server is shut down. The job behind it runs hourly, so
 * minutes would claim a precision that does not exist. Below one hour minutes are shown anyway:
 * there the number matters.
 */
export function fmtCountdown(min?: number | null): string {
  if (min == null || min < 0) return "—";
  if (min < 1) return i18n.t("format:countdown.now");
  // Round the WHOLE span to the smallest unit shown, then split it: rounding only the remainder
  // produced "19 d 24 h" and "1 h 60 min".
  const total = Math.round(min);
  if (total < 60) return i18n.t("format:countdown.m", { m: total });
  if (total < 1440) {
    const h = Math.floor(total / 60);
    const m = total % 60;
    return m ? i18n.t("format:countdown.hm", { h, m }) : i18n.t("format:countdown.h", { h });
  }
  const hours = Math.round(min / 60);
  const d = Math.floor(hours / 24);
  const h = hours % 24;
  return h ? i18n.t("format:countdown.dh", { d, h }) : i18n.t("format:countdown.d", { d });
}

export function fmtGiB(bytes?: number | null, digits = 1): string {
  return bytes == null ? "—" : `${(bytes / 1073741824).toFixed(digits)} GiB`;
}

/** Show an ISO time in local time. The backend sends naive UTC; `needsZ = true` appends the "Z". */
export function fmtDateTime(iso?: string | null, needsZ = false): string {
  if (!iso) return "—";
  const d = new Date(needsZ ? iso + "Z" : iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString(currentLanguage());
}

/** The date only (no time of day) — for version and release dates. */
export function fmtDate(iso?: string | null, needsZ = false): string {
  if (!iso) return "—";
  const d = new Date(needsZ ? iso + "Z" : iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleDateString(currentLanguage());
}

export function fmtRelative(iso?: string | null, needsZ = false): string {
  if (!iso) return "—";
  const d = new Date(needsZ ? iso + "Z" : iso);
  if (isNaN(d.getTime())) return "—";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return i18n.t("format:relative.now");
  if (diff < 3600) return i18n.t("format:relative.minutes", { n: Math.floor(diff / 60) });
  if (diff < 86400) return i18n.t("format:relative.hours", { n: Math.floor(diff / 3600) });
  return i18n.t("format:relative.days", { n: Math.floor(diff / 86400) });
}
