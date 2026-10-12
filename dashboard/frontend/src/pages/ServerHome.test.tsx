/**
 * The server page while its status is still on the way: no "Stopped" and no Start button for a
 * server that may well be running — the first overview answer takes a second or two (Docker
 * inspect, stats, an RCON round trip).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";

const SERVER = {
  id: 1, key: "mc-1", game_type: "minecraft", display_name: "Survival", sort_order: 0, enabled: true,
  management_mode: "dashboard", container_name: "mc-1", data_dir: "/data/mc-1", image_repo: "x",
  image_tag: "1", network: "net", console_kind: "source_rcon", rcon_host: "mc-1", rcon_port: 25575,
  rcon_password_env: "", game_ports: { game: "25565/tcp" }, game_version: "26.2", server_type: "",
  ram_mb: 0, cpu_cores: 0, heap_max_mb: null, heap_min_mb: null, idle_autostop_enabled: false,
  idle_timeout_minutes: null, bridge_in: false, bridge_out: false, presence_visible: false,
  announce_target: false, capabilities: ["console"],
};

const OVERVIEW = {
  container: { running: true, status: "running", exists: true }, uptime_seconds: 120, stats: { cpu_percent: 3 },
  players: { count: 0, names: [] }, rcon: { reachable: true, latency_ms: 4 }, max_players: 20, mods_count: null,
  last_backup: null, next_backup: null, next_restart: null, disk: null, pending_registrations: null,
  open_tickets: null, last_incident: null, mod_health: null, connect: null,
};

function fakeBackend(overview: Promise<Response>) {
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  return vi.fn((input: RequestInfo | URL) => {
    const p = new URL(String(input), "http://localhost").pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role: "admin", status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true, can_restart: true },
      });
    if (p === "/api/servers") return json(200, [SERVER]);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "minecraft", active: true }] });
    if (p === "/api/servers/mc-1/overview") return overview;
    return json(404, { detail: { code: "server.not_found", message: "not found", params: {} } });
  });
}

beforeEach(() => {
  localStorage.setItem("gss_access", "token");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the server page before its status is known", () => {
  it("shows neither Stopped nor Start until the overview has answered", async () => {
    let answer: (r: Response) => void = () => {};
    const overview = new Promise<Response>((resolve) => {
      answer = resolve;
    });
    vi.stubGlobal("fetch", fakeBackend(overview));
    render(
      <MemoryRouter initialEntries={["/s/mc-1"]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );

    const hero = (await screen.findByRole("heading", { name: "Survival" })).closest("div")!.parentElement!;
    expect(hero.textContent).toContain("Loading…");
    expect(hero.textContent).not.toContain("Stopped");
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();

    answer(new Response(JSON.stringify(OVERVIEW), { status: 200, headers: { "Content-Type": "application/json" } }));
    expect(await screen.findByRole("button", { name: "Stop" })).toBeTruthy();
    expect(hero.textContent).toContain("Running");
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });
});
