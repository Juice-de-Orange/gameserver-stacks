/**
 * A rolled-back recreate leaves the saved settings unapplied: the edit page must say so instead of
 * showing a RAM limit the running container does not have (measured: 4096 saved, 0 running).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { lastRecreateNotApplied, RecreateNotApplied } from "./RecreateNotApplied";

const row = (direction: string, status: string, id = 1) => ({ id, direction, status });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("lastRecreateNotApplied", () => {
  it("is true only when the newest entry is a recreate that did not complete", () => {
    expect(lastRecreateNotApplied([row("recreate", "rolled_back")])).toBe(true);
    expect(lastRecreateNotApplied([row("recreate", "rolled_back_partial")])).toBe(true);
    expect(lastRecreateNotApplied([row("recreate", "failed")])).toBe(true);
    expect(lastRecreateNotApplied([row("recreate", "ok")])).toBe(false);
    expect(lastRecreateNotApplied([row("recreate", "pending")])).toBe(false);
    // A later successful swap built the container from the saved values.
    expect(lastRecreateNotApplied([row("up", "ok", 2), row("recreate", "rolled_back", 1)])).toBe(false);
    expect(lastRecreateNotApplied([row("up", "rolled_back")])).toBe(false);
    expect(lastRecreateNotApplied([])).toBe(false);
  });
});

describe("RecreateNotApplied", () => {
  function history(rows: unknown[]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(rows), { status: 200, headers: { "Content-Type": "application/json" } }))),
    );
  }

  it("warns after a rolled-back recreate", async () => {
    history([row("recreate", "rolled_back")]);
    render(<RecreateNotApplied serverKey="vs-1" />);
    expect(await screen.findByText(/saved, but NOT applied/)).toBeTruthy();
  });

  it("stays silent after a successful one", async () => {
    history([row("recreate", "ok")]);
    render(<RecreateNotApplied serverKey="vs-1" />);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText(/NOT applied/)).toBeNull();
  });
});
