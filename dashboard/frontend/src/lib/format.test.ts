// The formatters are the last stop before the user's eye: whatever rounds wrongly here or lets a
// time zone slip is not seen as an error — it just looks like a different number. That is why the
// edge cases are pinned down, not the lucky ones.
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n";
import {
  fmtBytes,
  fmtCountdown,
  fmtDate,
  fmtDateTime,
  fmtDuration,
  fmtGiB,
  fmtHm,
  fmtMb,
  fmtPct,
  fmtRelative,
  fmtSpan,
} from "./format";

describe("empty values", () => {
  // Each of these functions is fed backend fields that MAY be `null` (never played, no backup,
  // no limit). "—" is the promise; a "NaN" or "undefined" in the UI would be the visible break.
  it("yields a dash throughout instead of NaN", () => {
    for (const f of [fmtDuration, fmtHm, fmtBytes, fmtPct, fmtMb, fmtGiB, fmtDateTime, fmtDate, fmtRelative, fmtSpan]) {
      expect((f as (v: unknown) => string)(null)).toBe("—");
      expect((f as (v: unknown) => string)(undefined)).toBe("—");
    }
  });

  it("treats 0 as a real value, not as empty", () => {
    expect(fmtDuration(0)).toBe("0m");
    expect(fmtPct(0)).toBe("0%");
    expect(fmtMb(0)).toBe("0 MB");
  });
});

describe("fmtDuration", () => {
  it("switches the unit at the boundaries", () => {
    expect(fmtDuration(59)).toBe("0m");
    expect(fmtDuration(60)).toBe("1m");
    expect(fmtDuration(3599)).toBe("59m");
    expect(fmtDuration(3600)).toBe("1h 0m");
    expect(fmtDuration(86399)).toBe("23h 59m");
    expect(fmtDuration(86400)).toBe("1d 0h");
    expect(fmtDuration(90061)).toBe("1d 1h");
  });

  it("discards negative durations instead of showing them", () => {
    // Happens when a session is closed with a log timestamp BEFORE the join.
    expect(fmtDuration(-1)).toBe("—");
  });
});

describe("fmtBytes / fmtMb / fmtGiB", () => {
  it("fmtBytes counts decimal (GB), fmtGiB binary (GiB) — on purpose", () => {
    expect(fmtBytes(1_000_000_000)).toBe("1.0 GB");
    expect(fmtGiB(1_073_741_824)).toBe("1.0 GiB");
    // The same number, two units: 1 GiB is ~1.07 GB.
    expect(fmtBytes(1_073_741_824)).toBe("1.1 GB");
  });

  it("switches at the boundaries", () => {
    expect(fmtBytes(999_999)).toBe("1000 kB");
    expect(fmtBytes(1_000_000)).toBe("1.0 MB");
    expect(fmtMb(1023)).toBe("1023 MB");
    expect(fmtMb(1024)).toBe("1.0 GB");
    expect(fmtMb(8192)).toBe("8.0 GB");
  });

  it("fmtGiB takes the number of decimals", () => {
    expect(fmtGiB(1_073_741_824, 2)).toBe("1.00 GiB");
  });
});

describe("times", () => {
  // The backend sends NAIVE UTC timestamps. `needsZ` appends the missing "Z"; without it the
  // browser reads them as local time and the display shifts by the zone offset. Exactly that
  // distinction is pinned down here.
  it("needsZ changes how a naive timestamp is read", () => {
    const naive = "2026-08-01T12:00:00";
    const asUtc = new Date(naive + "Z").getTime();
    const asLocal = new Date(naive).getTime();
    if (asUtc !== asLocal) {
      expect(fmtDateTime(naive, true)).not.toBe(fmtDateTime(naive, false));
    }
    // With "Z" the reading is unambiguous, whatever zone the test runs in.
    expect(fmtDateTime(naive, true)).toBe(new Date(naive + "Z").toLocaleString("en"));
  });

  it("catches unreadable timestamps", () => {
    expect(fmtDateTime("broken")).toBe("—");
    expect(fmtDate("broken")).toBe("—");
    expect(fmtRelative("broken")).toBe("—");
    expect(fmtDateTime("")).toBe("—");
  });

  it("fmtRelative steps by distance", () => {
    const ago = (sec: number) => new Date(Date.now() - sec * 1000).toISOString();
    expect(fmtRelative(ago(5))).toBe("just now");
    expect(fmtRelative(ago(120))).toBe("2 min ago");
    expect(fmtRelative(ago(7200))).toBe("2 h ago");
    expect(fmtRelative(ago(172800))).toBe("2 d ago");
  });
});

describe("fmtSpan", () => {
  // The idle timeout is stored in minutes (28800) and shown in days. If the conversion slips, the
  // page promises a different deadline than the one the server is actually stopped by — and
  // nobody notices until the server is down.
  it("calls the default by its proper name", () => {
    expect(fmtSpan(28800)).toBe("20 days");
  });

  it("does not round whole numbers away and does not invent decimals", () => {
    expect(fmtSpan(1440)).toBe("1 day");
    expect(fmtSpan(2160)).toBe("1.5 days");
    expect(fmtSpan(120)).toBe("2 hours");
    expect(fmtSpan(60)).toBe("1 hour");
    expect(fmtSpan(90)).toBe("1.5 hours");
    expect(fmtSpan(30)).toBe("30 minutes");
    expect(fmtSpan(1)).toBe("1 minute");
  });

  it("treats negative values like missing ones", () => {
    expect(fmtSpan(-1)).toBe("—");
  });
});

describe("fmtCountdown", () => {
  it("stays coarse where the job is coarse", () => {
    // The idle job runs hourly — minutes on a 20-day span would be a precision that does not
    // exist.
    expect(fmtCountdown(28800)).toBe("20 d");
    expect(fmtCountdown(28800 + 90)).toBe("20 d 2 h");
    expect(fmtCountdown(125)).toBe("2 h 5 min");
    expect(fmtCountdown(45)).toBe("45 min");
  });

  it("carries a rounded-up unit over instead of showing 24 h or 60 min", () => {
    // 28 799 min = 19 d 23 h 59 min: the hours round up to a full day.
    expect(fmtCountdown(28799)).toBe("20 d");
    expect(fmtCountdown(28800 - 20)).toBe("20 d");
    expect(fmtCountdown(28800 - 40)).toBe("19 d 23 h");
    expect(fmtCountdown(119.7)).toBe("2 h");
    expect(fmtCountdown(59.6)).toBe("1 h");
    expect(fmtCountdown(1439.8)).toBe("1 d");
  });

  it("says \"any moment\" instead of \"0 min\"", () => {
    expect(fmtCountdown(0)).toBe("any moment");
    expect(fmtCountdown(0.4)).toBe("any moment");
  });

  it("yields a dash instead of NaN", () => {
    expect(fmtCountdown(null)).toBe("—");
    expect(fmtCountdown(undefined)).toBe("—");
    expect(fmtCountdown(-5)).toBe("—");
  });
});

describe("language", () => {
  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  // The helpers translate at call time. A text cached at module level would keep the language the
  // module was imported in — the switch in the header would then change everything but these.
  it("follows a language switch without a reload", async () => {
    const ago = new Date(Date.now() - 120 * 1000).toISOString();
    expect(fmtRelative(ago)).toBe("2 min ago");
    await i18n.changeLanguage("de");
    expect(fmtRelative(ago)).toBe("vor 2 Min");
    expect(fmtDuration(90061)).toBe("1T 1Std");
    expect(fmtCountdown(0)).toBe("jeden Moment");
    expect(fmtCountdown(125)).toBe("2 Std 5 Min");
    expect(fmtSpan(28800)).toBe("20 Tage");
    expect(fmtSpan(1440)).toBe("1 Tag");
    expect(fmtSpan(2160)).toBe("1,5 Tage");
    const naive = "2026-08-01T12:00:00";
    expect(fmtDateTime(naive, true)).toBe(new Date(naive + "Z").toLocaleString("de"));
  });
});
