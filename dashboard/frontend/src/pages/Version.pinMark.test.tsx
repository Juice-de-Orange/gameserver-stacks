/**
 * The version page of a game without a version list (Arma Reforger): the mark next to the image
 * and the note under it must say the same thing. The A9 check found "floating" next to the
 * project's own default tag and "pinned on purpose" in the note right below it.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";

const SERVER = {
  id: 1, key: "rf-1", game_type: "arma_reforger", display_name: "Reforger", sort_order: 0, enabled: true,
  management_mode: "dashboard", container_name: "rf-1", data_dir: "/data/rf-1",
  image_repo: "ghcr.io/acemod/arma-reforger", image_tag: "sha-d042142", network: "net",
  console_kind: "battleye_rcon", rcon_host: "rf-1", rcon_port: 19999, rcon_password_env: "",
  game_ports: { game: "2001/udp", a2s: "17777/udp" }, game_version: "", server_type: "", ram_mb: 0,
  cpu_cores: 0, heap_max_mb: null, heap_min_mb: null, idle_autostop_enabled: false,
  idle_timeout_minutes: null, bridge_in: false, bridge_out: false, presence_visible: false,
  announce_target: false, capabilities: ["versions"],
};

function fakeBackend(pin: { image_ref: string; image_pinned: boolean; image_floating: boolean }) {
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  return vi.fn((input: RequestInfo | URL) => {
    const p = new URL(String(input), "http://localhost").pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role: "admin", status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true, can_change_version: true },
      });
    if (p === "/api/servers") return json(200, [SERVER]);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "arma_reforger", active: true }] });
    if (p === "/api/servers/rf-1/version/history") return json(200, []);
    if (p === "/api/servers/rf-1/version/available")
      return json(200, {
        supported: false, entries: [], current: "1.8.0.13", update_available: null, checked_at: null,
        note: "This game has no selectable version.", ...pin,
      });
    return json(404, { detail: { code: "server.not_found", message: "not found", params: {} } });
  });
}

function renderVersion() {
  render(
    <MemoryRouter initialEntries={["/s/rf-1/version"]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.setItem("gss_access", "token");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the image mark on a game without a version list", () => {
  it("calls a build tag fixed, not floating", async () => {
    vi.stubGlobal("fetch", fakeBackend({
      image_ref: "ghcr.io/acemod/arma-reforger:sha-d042142", image_pinned: false, image_floating: false,
    }));
    renderVersion();
    expect(await screen.findByText(/fixed tag/)).toBeTruthy();
    expect(screen.queryByText(/floating/)).toBeNull();
  });

  it("calls a channel tag floating", async () => {
    vi.stubGlobal("fetch", fakeBackend({
      image_ref: "ghcr.io/acemod/arma-reforger:latest", image_pinned: false, image_floating: true,
    }));
    renderVersion();
    expect(await screen.findByText(/floating/)).toBeTruthy();
  });

  it("calls a digest pinned", async () => {
    vi.stubGlobal("fetch", fakeBackend({
      image_ref: "ghcr.io/acemod/arma-reforger@sha256:9503881c", image_pinned: true, image_floating: false,
    }));
    renderVersion();
    expect(await screen.findByText(/pinned —/)).toBeTruthy();
  });
});
