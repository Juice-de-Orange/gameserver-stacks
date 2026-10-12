// Replays the real sequence of `CrossServer.tsx`: the 15-second poll sends its requests, the user
// flips a switch, the PUT goes through — and ONLY THEN does the poll answer, with the state from
// before.
import { describe, expect, it } from "vitest";
import { newFreshnessGuard } from "./freshness";

describe("freshness guard", () => {
  it("discards a response that set off before the save", () => {
    const a = newFreshnessGuard();

    const poll = a.begin();      // poll starts: /cross/settings reads chat_bridge = false
    a.invalidate();                // user flips the switch, PUT goes out
    a.invalidate();                // PUT is through, its response is in the state

    expect(a.valid(poll)).toBe(false);   // the poll's response must not overwrite it
  });

  it("lets a response through when nothing happened in between", () => {
    const a = newFreshnessGuard();
    const poll = a.begin();
    expect(a.valid(poll)).toBe(true);
  });

  it("a request started DURING the save counts as overtaken too", () => {
    // This is what the second `invalidate()` after the PUT is for: this request may have read
    // the old value because it reached the server before the PUT was committed.
    const a = newFreshnessGuard();
    a.invalidate();                // PUT out
    const during = a.begin();
    a.invalidate();                // PUT through
    expect(a.valid(during)).toBe(false);
  });

  it("requests started after the save are valid again", () => {
    const a = newFreshnessGuard();
    a.invalidate();
    a.invalidate();
    const after = a.begin();
    expect(a.valid(after)).toBe(true);
  });
});
