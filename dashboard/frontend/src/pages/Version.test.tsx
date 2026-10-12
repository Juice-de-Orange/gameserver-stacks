/**
 * The version page names a recreate a recreate, and the downgrade dialog does not claim a backup
 * that is only taken after it is confirmed.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Version from "./Version";

const server = {
  key: "vs-1", game_type: "vintagestory", display_name: "Vintage Story", game_version: "1.22.6",
  server_type: "", version_source: "env", capabilities: ["versions"],
};
const confirmCalls: { title: string; body: string }[] = [];

vi.mock("../components/ServerContext", () => ({
  useActiveServer: () => server,
  useServers: () => ({ reload: async () => {} }),
}));
vi.mock("../components/feedback", () => ({
  useConfirm: () => async (o: { title: string; body: string }) => {
    confirmCalls.push(o);
    return true;
  },
  useToast: () => ({ success: () => {}, error: () => {} }),
}));
vi.mock("../components/Worlds", () => ({ Worlds: () => null }));

const OP = {
  step: "Server is starting — verifying the boot (may take a few minutes)…", current: 0, total: 0,
  percent: null, detail: "", result: {}, started_at: "2026-10-12T11:38:00", finished_at: null,
  status: "running", running: true,
};

function backend(ops: Record<string, unknown>) {
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const p = url.pathname;
      if (p === "/api/servers/vs-1/version/history") return json(200, []);
      if (p === "/api/servers/vs-1/version/available")
        return json(200, { supported: true, entries: [], current: "1.22.6", update_available: null, checked_at: null, note: "" });
      if (p === "/api/servers/vs-1/operations") return json(200, ops[url.searchParams.get("kind") || ""] ?? {});
      if (p === "/api/servers/vs-1/version/change" && init?.method === "POST") {
        if (JSON.parse(String(init.body)).confirm_downgrade) return json(200, { ...OP, id: 9, kind: "version.change" });
        return json(400, {
          detail: { code: "version.downgrade_needs_confirmation", message: "Downgrade needs confirmation (confirm_downgrade)",
                    params: {}, old: "1.22.6", new: "1.21.0" },
        });
      }
      return json(404, {});
    }),
  );
}

beforeEach(() => {
  confirmCalls.length = 0;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the version page", () => {
  it("titles a running recreate as a recreate", async () => {
    backend({ "version.recreate": { ...OP, id: 3, kind: "version.recreate" } });
    render(<Version />);
    expect(await screen.findByText("Recreating the container")).toBeTruthy();
    expect(screen.queryByText(/Version change/)).toBeNull();
  });

  it("titles a version change as one, without 'in progress' before its outcome", async () => {
    backend({ "version.change": { ...OP, id: 4, kind: "version.change" } });
    render(<Version />);
    expect(await screen.findByText("Version change")).toBeTruthy();
  });

  it("the downgrade dialog does not claim the backup is already there", async () => {
    backend({});
    render(<Version />);
    const input = await screen.findByPlaceholderText("1.22.6");
    fireEvent.change(input, { target: { value: "1.21.0" } });
    fireEvent.click(screen.getByRole("button", { name: "Switch version" }));
    await waitFor(() => expect(confirmCalls.length).toBe(2));
    const downgrade = confirmCalls[1].body;
    expect(downgrade).toContain("From 1.22.6 to 1.21.0");
    expect(downgrade).not.toMatch(/is in place/);
    expect(downgrade).toMatch(/taken before the switch/);
  });
});
