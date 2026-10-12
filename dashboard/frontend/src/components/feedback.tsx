import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { detailText } from "../api";
import i18n from "../i18n";
import { Button, cx, GhostButton } from "./ui";

// ---------------------------------------------------------------- Toasts
type ToastKind = "success" | "error" | "info";
interface Toast {
  id: number;
  kind: ToastKind;
  msg: string;
}
/**
 * The parameters are deliberately `unknown`, not `string`.
 *
 * `string` would be a promise the code cannot keep: the messages come from `res.json()`, which
 * is `any`, so the signature is simply waved through. At runtime an object would then land in
 * `{t.msg}`, React would throw, and because the throw happens in the OUTERMOST provider no
 * ErrorBoundary below it could catch it. `push` normalises instead (see below).
 */
interface ToastApi {
  success: (m: unknown) => void;
  error: (m: unknown) => void;
  info: (m: unknown) => void;
}
const ToastCtx = createContext<ToastApi>(null as unknown as ToastApi);
export const useToast = () => useContext(ToastCtx);

// ---------------------------------------------------------------- Confirm
interface ConfirmOpts {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}
type ConfirmFn = (o: ConfirmOpts) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(null as unknown as ConfirmFn);
export const useConfirm = () => useContext(ConfirmCtx);

const TOAST_ICON = { success: CheckCircle2, error: XCircle, info: Info };
const TOAST_STYLE = {
  success: "border-success-border bg-surface-card text-success-text",
  error: "border-danger-border bg-surface-card text-red-200",
  info: "border-line bg-surface-card text-fg-muted",
};

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const [pending, setPending] = useState<{ opts: ConfirmOpts; resolve: (v: boolean) => void } | null>(
    null,
  );

  // The ONE funnel every toast message has to pass. Normalising here makes every call site
  // structurally safe — whatever the backend sends today or tomorrow. The fallback is
  // translated at call time (`i18n.t`, not the hook's `t`): this callback is created once.
  const push = useCallback((kind: ToastKind, msg: unknown) => {
    const id = ++seq.current;
    const text = detailText(msg, i18n.t("feedback:toast.unknownError"));
    setToasts((list) => [...list, { id, kind, msg: text }]);
    setTimeout(() => setToasts((list) => list.filter((x) => x.id !== id)), 4800);
  }, []);

  const toastApi: ToastApi = {
    success: (m) => push("success", m),
    error: (m) => push("error", m),
    info: (m) => push("info", m),
  };

  const confirm = useCallback<ConfirmFn>(
    (opts) => new Promise<boolean>((resolve) => setPending({ opts, resolve })),
    [],
  );

  function settle(value: boolean) {
    pending?.resolve(value);
    setPending(null);
  }

  return (
    <ToastCtx.Provider value={toastApi}>
      <ConfirmCtx.Provider value={confirm}>
        {children}

        {/* Toast stack — above the tab bar on mobile */}
        <div className="pointer-events-none fixed bottom-40 right-4 z-[70] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2 lg:bottom-4">
          {toasts.map((toast) => {
            const Icon = TOAST_ICON[toast.kind];
            return (
              <div
                key={toast.id}
                className={cx(
                  "pointer-events-auto flex items-start gap-2 rounded-md border px-3 py-2 text-sm shadow-panel",
                  TOAST_STYLE[toast.kind],
                )}
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                <span role="alert" className="flex-1 break-words">
                  {toast.msg}
                </span>
                <button
                  onClick={() => setToasts((x) => x.filter((y) => y.id !== toast.id))}
                  aria-label={t("action.close")}
                  className="text-fg-dim hover:text-fg"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            );
          })}
        </div>

        {/* Confirm dialog */}
        {pending && (
          <Modal onClose={() => settle(false)} danger={pending.opts.danger}>
            <h2 className="text-stencil text-lg text-fg">{pending.opts.title}</h2>
            {pending.opts.body && (
              <div className="text-sm text-fg-muted">{pending.opts.body}</div>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <GhostButton onClick={() => settle(false)}>
                {pending.opts.cancelLabel || t("action.cancel")}
              </GhostButton>
              <Button
                autoFocus
                tone={pending.opts.danger ? "danger" : "primary"}
                onClick={() => settle(true)}
              >
                {pending.opts.confirmLabel || t("action.confirm")}
              </Button>
            </div>
          </Modal>
        )}
      </ConfirmCtx.Provider>
    </ToastCtx.Provider>
  );
}

/** Reusable modal overlay. A bottom sheet on mobile, centred from `sm` up. `danger` adds the
 *  warning-stripe header. */
export function Modal({
  children,
  onClose,
  danger,
  className = "",
}: {
  children: ReactNode;
  onClose?: () => void;
  danger?: boolean;
  className?: string;
}) {
  const { t } = useTranslation("feedback");
  useEffect(() => {
    if (!onClose) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-end justify-center bg-black/70 p-0 sm:items-center sm:p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        className={cx(
          "animate-sheet w-full max-w-md overflow-hidden rounded-t-2xl border border-line bg-surface-card pb-[env(safe-area-inset-bottom)] shadow-soft sm:animate-none sm:rounded-2xl sm:pb-0",
          className,
        )}
      >
        {danger && (
          <div className="warning-stripe flex items-center gap-2 px-4 py-1.5 text-xs font-semibold uppercase tracking-wide text-warning-text">
            <AlertTriangle className="h-4 w-4" /> {t("modal.warning")}
          </div>
        )}
        <div className="max-h-[80vh] space-y-3 overflow-y-auto p-5">{children}</div>
      </div>
    </div>
  );
}
