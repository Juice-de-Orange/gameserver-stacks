/**
 * The version page of a game without a version list still shows its worlds.
 *
 * Valheim has no selectable version (the image fetches the game on every start), so the page
 * takes its "no version list" branch. The worlds and the Recreate button sat only in the other
 * branch: a Valheim server's worlds could not be selected or removed anywhere in the dashboard,
 * and its settings, which all live in the container env, could not be applied from this page.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import type { ServerInfo } from "../lib/game";

const VALHEIM: ServerInfo = {
  id: 1, key: "val-1", game_type: "valheim", display_name: "Valheim", sort_order: 0, enabled: true,
  management_mode: "dashboard", container_name: "val-1", data_dir: "/data/games/val-1",
  image_repo: "ghcr.io/community-valheim-tools/valheim-server", image_tag: "1.4.0", network: "net",
  console_kind: "none", rcon_host: "val-1", rcon_port: 0, rcon_password_env: "",
  game_ports: { game: "2456/udp" }, game_version: "", server_type: "", ram_mb: 0, cpu_cores: 0,
  heap_max_mb: null, heap_min_mb: null, idle_autostop_enabled: true, idle_timeout_minutes: null,
  bridge_in: true, bridge_out: true, presence_visible: true, announce_target: true,
  capabilities: ["players", "config", "mods", "backup", "versions"], extra: {}, updated_at: "2026-10-12T10:00:00",
};

function fakeBackend() {
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  return vi.fn((input: RequestInfo | URL) => {
    const p = new URL(String(input), "http://localhost").pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role: "admin", status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true, can_manage_mods: true },
      });
    if (p === "/api/servers") return json(200, [VALHEIM]);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "valheim", active: true }] });
    if (p === "/api/servers/val-1/version/available")
      return json(200, {
        supported: false, entries: [], current: "1.0.17", update_available: null, checked_at: null,
        note: "", image_ref: "ghcr.io/community-valheim-tools/valheim-server:1.4.0", image_pinned: false,
      });
    if (p === "/api/servers/val-1/version/history") return json(200, []);
    if (p === "/api/servers/val-1/worlds")
      return json(200, {
        worlds: [{ name: "Dedicated", size: 145678, modified: "2026-10-12T12:58:00+00:00", files: ["Dedicated"],
          active: true, reason: "WORLD_NAME" }],
        switchable: true,
        removable: true,
      });
    return json(200, {});
  });
}

beforeEach(() => localStorage.setItem("gss_access", "token"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the version page without a version list", () => {
  it("lists the worlds of a Valheim server", async () => {
    vi.stubGlobal("fetch", fakeBackend());
    render(
      <MemoryRouter initialEntries={["/s/val-1/version"]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Dedicated")).toBeTruthy();
    expect(screen.getByText("Worlds")).toBeTruthy();
    // A world switch only takes effect with a recreate, so the button has to be here too.
    expect(screen.getByRole("button", { name: /Recreate/ })).toBeTruthy();
  });
});
