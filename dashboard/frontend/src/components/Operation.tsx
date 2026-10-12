// Tracking long-running operations — applying mods, switching versions.
//
// The call starts the operation and returns an id at once; the progress is polled here. The
// display thereby survives a dropped connection (a reverse proxy may cut a request after about
// 100 s) and a page reload — instead of showing an error while the server keeps working
// correctly.
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Loader2, ShieldAlert } from "lucide-react";
import { apiJson, noteText } from "../api";
import { tb } from "../i18n";
import { cx } from "./ui";

export interface OperationState {
  id: number;
  kind: string;
  status: "running" | "ok" | "failed";
  step: string;
  current: number;
  total: number;
  percent: number | null;
  detail: string;
  result: Record<string, unknown>;
  running: boolean;
  started_at: string;
  finished_at: string | null;
}

/**
 * Tracks an operation. `kind` lets the page attach again after a reload: if no id is known, the
 * most recent operation of that kind is fetched, and taken over only while it is still RUNNING.
 * A finished one belongs to the visit that watched it; attached on entry, a failure from an hour
 * ago (or one started on another page) would sit on top of every later visit. The outcome stays
 * in each page's own record (version history, backup list, mod changes).
 */
export function useOperation(serverKey: string, kind: string) {
  const [op, setOp] = useState<OperationState | null>(null);
  const idRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const poll = useCallback(async () => {
    if (!serverKey) return null;
    try {
      const path = idRef.current
        ? `/servers/${serverKey}/operations/${idRef.current}`
        : `/servers/${serverKey}/operations?kind=${encodeURIComponent(kind)}`;
      // No running operation → the API answers with an empty object. `"id" in data` does NOT
      // narrow that for TypeScript (a record with an index signature stays in the union), so the
      // field itself is checked and the type narrowed explicitly once.
      const attaching = idRef.current === null;
      const data = await apiJson<Partial<OperationState>>(path);
      if (typeof data.id !== "number") return null;
      const current = data as OperationState;
      if (attaching && !current.running) return null;
      idRef.current = current.id;
      if (aliveRef.current) setOp(current);
      return current;
    } catch {
      return null; // a missed poll is not an error — the operation keeps running
    }
  }, [serverKey, kind]);

  // On entering the page, attach to an operation that may be running.
  useEffect(() => {
    idRef.current = null;
    setOp(null);
    void poll();
  }, [poll]);

  // While something is running, ask every 2 s.
  useEffect(() => {
    if (!op?.running) return;
    const timer = window.setInterval(() => void poll(), 2000);
    return () => window.clearInterval(timer);
  }, [op?.running, poll]);

  /** After starting a new operation: take over its id and keep tracking at once. */
  const track = useCallback(
    (started: { id: number }) => {
      idRef.current = started.id;
      void poll();
    },
    [poll],
  );

  return { op, track, refresh: poll };
}

/** Display of an operation: step, progress, result. */
export function OperationPanel({ op, title }: { op: OperationState | null; title: string }) {
  const { t } = useTranslation("operation");
  if (!op) return null;

  const tone =
    op.status === "failed"
      ? "border-danger-border bg-danger-soft text-danger-text"
      : op.status === "ok"
        ? "border-success/40 bg-success-soft text-success-text"
        : "border-line bg-surface-sunken text-fg-muted";

  const Icon = op.status === "failed" ? ShieldAlert : op.status === "ok" ? CheckCircle2 : Loader2;
  // A summary with a value in it comes coded in `result.summary` (`detail` holds its English text).
  const summary = op.result?.summary;
  const finalText = op.status === "ok" && summary && typeof summary === "object" ? noteText(summary) : tb(op.detail);
  const note = op.running ? "" : noteText(op.result?.note);

  return (
    <div className={cx("rounded-xl border p-3 text-sm", tone)}>
      <div className="flex items-start gap-2">
        <Icon className={cx("mt-0.5 h-4 w-4 shrink-0", op.running && "animate-spin")} />
        <div className="min-w-0 flex-1">
          <div className="font-medium">
            {op.status === "ok" ? t("done", { title }) : op.status === "failed" ? t("failed", { title }) : title}
          </div>
          {(op.step || op.detail) && (
            <div className="mt-0.5 break-words opacity-90">
              {op.status === "running" ? tb(op.step) : finalText || tb(op.step)}
            </div>
          )}
          {op.running && op.percent != null && (
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-surface-raised">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${Math.min(100, op.percent)}%` }}
              />
            </div>
          )}
          {op.running && (
            <div className="mt-1 text-xs opacity-70">
              {t("background")}
            </div>
          )}
          {/* What went wrong AFTER the work succeeded (history not written, result truncated):
              the backend puts it into `result.note`. */}
          {note && <div className="mt-1 break-words text-xs text-warning-text">{note}</div>}
        </div>
      </div>
    </div>
  );
}
