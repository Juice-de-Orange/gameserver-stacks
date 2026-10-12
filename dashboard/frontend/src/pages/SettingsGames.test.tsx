/**
 * Which games an installation runs: the first-run step, the settings page and the places that
 * must hide inactive games. The whole app runs; only `fetch` is replaced by a small fake backend.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import { GAMES, activeGames } from "../lib/game";

const FLEET = {
  host: {
    cpu_count: 4,
    mem_total_bytes: 8 * 1024 ** 3,
    source: "docker_info",
    disk: null,
    budget: { total_ram_mb: 0, used_ram_mb: 0, total_cpu_cores: 0, used_cpu_cores: 0, free_ram_mb: null },
  },
  job: { idle_interval_min: 60 },
  generated_at: "2026-10-03T12:00:00Z",
  servers: [],
};

interface Call {
  method: string;
  path: string;
  body: unknown;
}

/** `active`: the games switched on. `blocked`: games whose switch-off the backend refuses (409). */
function fakeBackend(active: string[], blocked: Record<string, string[]> = {}) {
  const calls: Call[] = [];
  const state = new Set(active);
  const list = () => ({ games: GAMES.map((g) => ({ game_type: g, active: state.has(g) })) });
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path: url.pathname, body });
    const p = url.pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role: "admin", status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true },
      });
    if (p === "/api/servers" && method === "GET") return json(200, []);
    if (p === "/api/servers/fleet") return json(200, FLEET);
    if (p === "/api/servers/presets")
      return json(200, { key_pattern: "^[a-z0-9-]+$", data_root: "/data", network: "net", presets: [] });
    if (p === "/api/settings/games" && method === "GET") return json(200, list());
    if (p === "/api/settings/games" && method === "PUT") {
      const changes = (body as { games: Record<string, boolean> }).games;
      for (const [game, on] of Object.entries(changes)) {
        if (!on && blocked[game]) {
          const servers = blocked[game]!.join(", ");
          return json(409, {
            detail: {
              code: "games.has_servers",
              message: `Game '${game}' still has enabled servers: ${servers}. Disable them first.`,
              params: { game, servers },
              servers: blocked[game],
            },
          });
        }
      }
      for (const [game, on] of Object.entries(changes)) {
        if (on) state.add(game);
        else state.delete(game);
      }
      return json(200, list());
    }
    return json(404, { detail: { code: "server.not_found", message: "not found", params: {} } });
  });
  return { calls, fetchMock };
}

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
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

describe("first run", () => {
  it("asks for the games before it offers the first server", async () => {
    const backend = fakeBackend([]);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/");

    expect(await screen.findByText("Which games do you want to run?")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add your first server" })).toBeNull();
    const next = screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);                       // nothing picked yet

    fireEvent.click(screen.getByRole("button", { name: "Minecraft" }));
    fireEvent.click(next);

    expect(await screen.findByRole("button", { name: "Add your first server" })).toBeTruthy();
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put).toMatchObject({ path: "/api/settings/games", body: { games: { minecraft: true } } });
  });

  it("skips the step once a game is active", async () => {
    vi.stubGlobal("fetch", fakeBackend(["factorio"]).fetchMock);
    renderApp("/");
    expect(await screen.findByRole("button", { name: "Add your first server" })).toBeTruthy();
    expect(screen.queryByText("Which games do you want to run?")).toBeNull();
  });

  it("the add-server page points to the games when none is active", async () => {
    vi.stubGlobal("fetch", fakeBackend([]).fetchMock);
    renderApp("/servers/new");
    expect(await screen.findByText("No game is active on this installation yet.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Choose games" }).getAttribute("href")).toBe("/settings/games");
  });
});

describe("Settings → Games", () => {
  it("is in the admin navigation", async () => {
    vi.stubGlobal("fetch", fakeBackend(["minecraft"]).fetchMock);
    renderApp("/settings/games");
    const sidebar = (await screen.findAllByRole("link", { name: "Games" }))[0]!;
    expect(sidebar.getAttribute("href")).toBe("/settings/games");
  });

  it("switches a game on", async () => {
    const backend = fakeBackend(["minecraft"]);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/settings/games");

    const row = (await screen.findByText("Factorio")).closest("li") as HTMLElement;
    const toggle = within(row).getByRole("switch");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect(backend.calls.find((c) => c.method === "PUT")?.body).toEqual({ games: { factorio: true } });
  });

  it("shows the refusal on the game's row and keeps the switch on", async () => {
    vi.stubGlobal("fetch", fakeBackend(["minecraft"], { minecraft: ["mc-1"] }).fetchMock);
    renderApp("/settings/games");

    const row = (await screen.findByText("Minecraft")).closest("li") as HTMLElement;
    const toggle = within(row).getByRole("switch");
    fireEvent.click(toggle);
    expect(
      await within(row).findByText("Game 'minecraft' still has enabled servers: mc-1. Disable them first."),
    ).toBeTruthy();
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });
});

describe("activeGames", () => {
  it("keeps the backend's order and drops what it does not know", () => {
    expect(
      activeGames([
        { game_type: "factorio", active: true },
        { game_type: "pz", active: false },
        { game_type: "minecraft", active: true },
        { game_type: "tetris" as never, active: true },
      ]),
    ).toEqual(["minecraft", "factorio"]);
    expect(activeGames(null)).toEqual([]);
  });
});
