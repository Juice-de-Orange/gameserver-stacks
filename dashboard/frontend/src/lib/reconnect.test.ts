// The case behind this file: `docker logs?follow=1&tail=200` on a STOPPED container answers 200,
// delivers the last 200 lines and ends the stream at once. Whoever resets the backoff on the
// first message then reconnects once per second — forever, and precisely during version changes,
// mod applies and restores.
import { describe, expect, it } from "vitest";
import { HELD_MS, backoffMs, newReconnect } from "./reconnect";

/** A clock that only runs when the test advances it. */
function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

describe("reconnect", () => {
  it("doubles and caps at 15 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6].map(backoffMs)).toEqual([1000, 2000, 4000, 8000, 15000, 15000, 15000]);
  });

  it("tail-and-end does NOT count as a connection that held", () => {
    // The log case exactly: socket open, 200 lines, closed at once. Six rounds.
    const u = clock();
    const w = newReconnect(u.now);
    const waits: number[] = [];
    for (let i = 0; i < 6; i++) {
      w.attemptStarted();
      w.opened();
      u.advance(80);            // tail arrives, stream ends
      waits.push(w.waitMs());
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
  });

  it("a connection that held resets the counter", () => {
    const u = clock();
    const w = newReconnect(u.now);
    w.attemptStarted(); w.opened(); u.advance(50); w.waitMs();   // 1000
    w.attemptStarted(); w.opened(); u.advance(50); w.waitMs();   // 2000
    w.attemptStarted(); w.opened();
    u.advance(HELD_MS + 1);                                        // this time it held        
    expect(w.waitMs()).toBe(1000);
  });

  it("a rejected handshake resets nothing, not even after a long time", () => {
    // 4401 after an expired token: `onopen` fires, `onclose` follows at once. Resetting here
    // builds the storm the rule was written against.
    const u = clock();
    const w = newReconnect(u.now);
    const waits: number[] = [];
    for (let i = 0; i < 4; i++) {
      w.attemptStarted();
      w.opened();
      u.advance(5);
      waits.push(w.waitMs());
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000]);
  });

  it("a connection that never opened does not count as having held", () => {
    // Backend gone (1006 without `onopen`) — and the timestamp of an EARLIER attempt that held
    // must not carry over.
    const u = clock();
    const w = newReconnect(u.now);
    expect(
      (() => { w.attemptStarted(); w.opened(); u.advance(HELD_MS + 1); return w.waitMs(); })(),
    ).toBe(1000);   // held → reset, and this attempt uses up the 1000
    const waits: number[] = [];
    for (let i = 0; i < 3; i++) {
      w.attemptStarted();          // no `opened` — the handshake never completes
      u.advance(60_000);            // despite the long pause
      waits.push(w.waitMs());
    }
    expect(waits).toEqual([2000, 4000, 8000]);
  });
});
