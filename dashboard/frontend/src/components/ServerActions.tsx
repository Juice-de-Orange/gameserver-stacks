// Start / stop / restart — ONCE, for every page that shows these buttons.
//
// Several pages show the same buttons, and a second version of this chain would drift. What
// hangs on it concretely:
//
//   * **Two different 409s look alike.** `POST /start` answers 409 both for the RAM budget
//     (`detail.stop_candidates`) and for the mod boot gate (`detail.blockers`). Whoever does not
//     tell them apart offers "stop another server" while in truth the mod configuration is
//     broken — the admin then shuts servers down without it helping.
//   * **The chain does not survive navigation, but the stop does.** "Stop & start" stops
//     ANOTHER server and waits up to 40 s. If the dialog is closed, the stop is real anyway.
//     Every piece of feedback therefore also goes to the live console, which is app-wide and
//     survives navigation.
import { ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, apiJson, detailText } from "../api";
import { useAuth } from "../auth";
import { Modal, useConfirm, useToast } from "./feedback";
import { useLiveConsole } from "./liveconsole";
import { Button, GhostButton } from "./ui";
import { fmtMb } from "../lib/format";

export type ServerAction = "start" | "stop" | "restart";

export interface StopCandidate {
  key: string;
  display_name?: string;
  effective_ram_mb: number;
}

/** The structured 409 of the RAM budget (NOT the one of the mod boot gate — that has `blockers`). */
export interface BudgetConflict {
  message: string;
  deficit_mb: number;
  stop_candidates: StopCandidate[];
}

/**
 * How `runAction` ended: done (`ok`), waiting in the budget dialog (`conflict` — the chain goes
 * on there), not confirmed (`cancelled`), or refused with the reason the user was shown (`failed`).
 */
export type ActionOutcome =
  | { status: "ok" | "conflict" | "cancelled" }
  | { status: "failed"; reason: string };

export interface ServerActions {
  /** Which action is running ("" for none), for `disabled`. */
  busy: string;
  /** Which server it runs on — the fleet table needs that per row. */
  busyKey: string;
  runAction: (key: string, name: string, a: ServerAction) => Promise<ActionOutcome>;
  /** The conflict dialog. Always render it; it is `null` as long as there is none. */
  dialog: ReactNode;
}

/** The `detail` of a failed response, whatever shape it has. */
async function detailOf(res: Response): Promise<unknown> {
  return (await res.json().catch(() => ({}))).detail;
}

export function useServerActions(onDone?: () => void): ServerActions {
  const { t } = useTranslation(["serverActions", "common"]);
  const { me } = useAuth();
  const confirm = useConfirm();
  const toast = useToast();
  const { emit } = useLiveConsole();
  const [busy, setBusy] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const [conflict, setConflict] = useState<BudgetConflict | null>(null);
  const [targetKey, setTargetKey] = useState("");

  /**
   * Is the calling page still alive?
   *
   * `freeAndStart` waits up to 40 s for another server to stop. The dialog closes on
   * Escape or a click next to it without touching the running chain, and the page is unmounted
   * on every navigation. If the chain ran on afterwards, the stop was real (players kicked) but
   * every piece of feedback a no-op: `setConflict` on an unmounted component does nothing, and
   * nobody sees the toasts. The result: one server off, one's own not started, no message.
   */
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  function finish() {
    // The shared poller ticks every few seconds — after one's own action the result should show
    // earlier.
    setTimeout(() => onDone?.(), 2500);
  }

  async function runAction(key: string, name: string, a: ServerAction): Promise<ActionOutcome> {
    if (a !== "start") {
      const ok = await confirm({
        title: a === "stop" ? t("confirm.stop.title", { name }) : t("confirm.restart.title", { name }),
        body: t("confirm.body"),
        danger: true,
        confirmLabel: a === "stop" ? t("action.stop") : t("action.restart"),
      });
      if (!ok) return { status: "cancelled" };
    }
    const failed = (reason: string): ActionOutcome => {
      toast.error(reason);
      return { status: "failed", reason };
    };
    setBusy(a);
    setBusyKey(key);
    setTargetKey(key);
    try {
      const res = await api(`/servers/${key}/${a}`, { method: "POST" });
      if (res.ok) {
        toast.success(
          a === "start" ? t("triggered.start") : a === "stop" ? t("triggered.stop") : t("triggered.restart"),
        );
        finish();
        return { status: "ok" };
      } else if (res.status === 409 && a === "start") {
        // Both 409 shapes come through here. Only the one WITH `stop_candidates` is the budget
        // conflict; everything else (e.g. the mod boot gate, a port in use) carries its own
        // explanation and must keep it.
        const d = await detailOf(res);
        if (d && typeof d === "object" && Array.isArray((d as BudgetConflict).stop_candidates)) {
          setConflict(d as BudgetConflict);
          return { status: "conflict" };
        }
        return failed(detailText(d, t("error.budgetExceeded")));
      }
      return failed(detailText(await detailOf(res), t("error.actionFailed")));
    } catch (e) {
      return failed((e as Error).message);
    } finally {
      setBusy("");
      setBusyKey("");
    }
  }

  /** Waits until a server has really stopped. Bounded — a game may save its world on stop. */
  async function waitUntilStopped(k: string, timeoutMs = 40000): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && alive.current) {
      try {
        const st = await apiJson<{ container?: { running?: boolean } }>(`/servers/${k}/overview`);
        if (!st.container?.running) return true;
      } catch {
        /* temporary — keep asking */
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    return false;
  }

  async function startAfterFreeing() {
    const res = await api(`/servers/${targetKey}/start`, { method: "POST" });
    if (res.ok) {
      toast.success(t("toast.freedAndStarted"));
      emit(t("emit.startedAfterFree", { server: targetKey }));
      setConflict(null);
      finish();
      return;
    }
    if (res.status === 409) {
      const d = await detailOf(res);
      if (d && typeof d === "object" && Array.isArray((d as BudgetConflict).stop_candidates)) {
        emit(t("emit.stillNotEnough", { server: targetKey }));
        setConflict(d as BudgetConflict); // still not enough → new list
        toast.error(t("toast.stillNotEnoughNext"));
        return;
      }
      // The second kind of 409 (mod boot gate, `blockers` plus its own text). Without this
      // distinction the existing, correct message would be thrown away and replaced by "stop
      // another server".
      const reason409 = detailText(d, t("error.stillNotEnough"));
      emit(`✖ ${targetKey}: ${reason409}`);
      toast.error(reason409);
      return;
    }
    const reason = detailText(await detailOf(res), t("error.startFailed"));
    emit(t("emit.startAfterFreeFailed", { server: targetKey, reason }));
    toast.error(reason);
  }

  async function freeAndStart(c: StopCandidate) {
    setBusy("start");
    setBusyKey(targetKey);
    try {
      const stopRes = await api(`/servers/${c.key}/stop`, { method: "POST" });
      if (!stopRes.ok) {
        toast.error(detailText(await detailOf(stopRes), t("error.stopFailed")));
        return;
      }
      toast.info(t("toast.stopping", { name: c.display_name || c.key }));
      emit(t("emit.stoppingFor", { server: c.key, target: targetKey }));
      if (!(await waitUntilStopped(c.key))) {
        // Leaving the page aborts the wait and lands here too — then the other server is down,
        // and exactly that has to be written somewhere.
        emit(t("emit.notStoppedInTime", { server: c.key, target: targetKey }));
        if (alive.current) toast.error(t("toast.stillStopping"));
        return;
      }
      await startAfterFreeing();
    } finally {
      setBusy("");
      setBusyKey("");
    }
  }

  async function startAnyway() {
    setBusy("start");
    setBusyKey(targetKey);
    try {
      const res = await api(`/servers/${targetKey}/start?force=true`, { method: "POST" });
      if (res.ok) {
        toast.success(t("toast.forced"));
        setConflict(null);
        finish();
      } else {
        toast.error(detailText(await detailOf(res), t("error.startFailed")));
      }
    } finally {
      setBusy("");
      setBusyKey("");
    }
  }

  const dialog = conflict ? (
    <Modal onClose={() => setConflict(null)} danger>
      <h2 className="text-lg text-fg">{t("conflict.title")}</h2>
      <div className="text-sm text-fg-muted">
        {/* `detailText` translates the budget message by its code, if the backend sent one. */}
        {detailText(conflict)} {me?.role === "admin" ? t("conflict.hintAdmin") : t("conflict.hint")}
      </div>
      <div className="space-y-2">
        {conflict.stop_candidates.length === 0 && (
          <div className="text-sm text-fg-dim">{t("conflict.none")}</div>
        )}
        {conflict.stop_candidates.map((c) => (
          <div
            key={c.key}
            className="flex items-center justify-between gap-3 rounded-md border border-line bg-surface px-3 py-2"
          >
            <div className="min-w-0 text-sm">
              <div className="truncate text-fg">{c.display_name || c.key}</div>
              <div className="text-xs text-fg-dim">
                {t("conflict.frees", { amount: fmtMb(c.effective_ram_mb) })}
              </div>
            </div>
            <Button disabled={!!busy} onClick={() => freeAndStart(c)}>
              {t("conflict.stopAndStart")}
            </Button>
          </div>
        ))}
      </div>
      <div className="flex justify-end gap-2 pt-1">
        <GhostButton onClick={() => setConflict(null)}>{t("common:action.cancel")}</GhostButton>
        {me?.role === "admin" && (
          <Button tone="danger" disabled={!!busy} onClick={startAnyway}>
            {t("conflict.force")}
          </Button>
        )}
      </div>
    </Modal>
  ) : null;

  return { busy, busyKey, runAction, dialog };
}
