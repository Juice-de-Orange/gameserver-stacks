/**
 * Reconnecting with backoff — the one place that holds the rule.
 *
 * Every WebSocket in the app reconnects after a drop. The decisive question is not the backoff
 * itself but **when the counter may be reset**:
 *
 * - Resetting in `onopen` is wrong: the backend accepts the handshake **before** the auth check
 *   and may then close with 4401. With an expired token that produces a reconnect storm.
 * - Resetting on the **first message** is wrong as well: `docker logs?follow=1&tail=200` on a
 *   **stopped** container answers 200, delivers the last 200 lines and ends the stream at once.
 *   The message arrives BEFORE the close, so `retry` is back at 0 on every `onclose` and the
 *   next connection follows after exactly one second, forever. Per second: one WS handshake, one
 *   new HTTP client against the Docker socket proxy, one `GET /containers/<name>/logs` — against
 *   a backend with exactly ONE uvicorn worker, and precisely during version changes, mod applies
 *   and restores, when the container is down for minutes.
 *
 * Correct is: reset once the connection has **held**. `reconnect-guard.test.ts` makes sure
 * nobody builds a private copy of this rule.
 */

/** Upper bound of the wait. Waiting longer helps nobody. */
export const BACKOFF_MAX_MS = 15_000;

/** From this lifetime on a connection counts as having held — short enough to be back quickly
 *  after a real dropout, long enough not to count tail-and-end as a success. */
export const HELD_MS = 10_000;

export function backoffMs(retry: number): number {
  return Math.min(1000 * 2 ** retry, BACKOFF_MAX_MS);
}

export interface Reconnect {
  /** Call before every connection attempt (in `connect()`, not only the first time). */
  attemptStarted(): void;
  /** In `onopen`. Only remembers the time — the counter stays untouched. */
  opened(): void;
  /** In `onclose`. Returns the wait until the next attempt and counts up. */
  waitMs(): number;
}

export function newReconnect(now: () => number = Date.now): Reconnect {
  let retry = 0;
  let openedAt = 0;
  return {
    attemptStarted() {
      // MUST be reset per attempt: otherwise the timestamp of an earlier connection that held
      // carries over into all following failed ones. A tab left open for hours would then have
      // `retry = 0` permanently on every deploy.
      openedAt = 0;
    },
    opened() {
      openedAt = now();
    },
    waitMs() {
      if (openedAt && now() - openedAt > HELD_MS) retry = 0;
      return backoffMs(retry++);
    },
  };
}
