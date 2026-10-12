/**
 * A fresh installation: no server in the registry. The admin must get from the empty fleet page
 * to a created server and its next steps without ever seeing a "not found":
 *
 *   empty fleet → "Add your first server" → choose the game → fill the form → create
 *   (`POST /api/servers`, switched off) → the server's page offers "Create container"
 *   → once the container exists: enable and start.
 *
 * The whole app runs; only `fetch` is replaced by a small fake backend.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import type { Preset, PresetsResponse } from "./AddServer";

const MC: Preset = {
  game_type: "minecraft",
  image_repo: "itzg/minecraft-server",
  image_tag: "2026.9.2-java25",
  image_help: "",
  ports: [{ name: "game", port: 25565, proto: "tcp", editable: true, env: "SERVER_PORT", help: "" }],
  console_kind: "source_rcon",
  rcon_port: 25575,
  secret_env: "MC_RCON_PW",
  secret_required: true,
  secret_help: "",
  game_version: "26.2",
  version_help: "",
  version_required: false,
  server_type: "PAPER",
  ram_mb: 5120,
  heap_max_mb: 4096,
  heap_min_mb: 4096,
  memory_help: "",
  values: [{ name: "MOTD", value: "A Paper Minecraft server", target: "env", secret: false, required: false, help: "" }],
  acknowledgements: [{ id: "eula", url: "https://www.minecraft.net/en-us/eula", help: "" }],
};

const PRESETS: PresetsResponse = {
  key_pattern: "^[a-z0-9][a-z0-9-]{1,31}$",
  data_root: "/data/games",
  network: "gameserver-net",
  presets: [MC],
};

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

const PROVISION_OK = {
  id: 7, kind: "server.provision", status: "ok", step: "Creating the container…", current: 0, total: 0,
  percent: null, detail: "The container was created.", result: { summary: "The container was created." },
  running: false, started_at: "2026-10-12T10:00:00", finished_at: "2026-10-12T10:01:00",
};

const CREATED = "Container mc-1 created. Switch the server on and start it below.";

interface Call {
  method: string;
  path: string;
  body: unknown;
  query: string;
  at: number;
}

/**
 * A fake backend: the registry starts empty, `POST /api/servers` adds a row to it.
 * `background`: "Create container" answers like the real API — a running operation — and the
 * next poll of `…/provision` reports it finished. `startError`: the start is refused with it.
 */
function fakeBackend(role: "admin" | "member" = "admin", failCreate = false, background = false, startError?: unknown) {
  const calls: Call[] = [];
  const registry: Record<string, unknown>[] = [];
  const state = { containerExists: false, enabled: false, provisioned: false };
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path: url.pathname, body, query: url.search, at: performance.now() });
    const p = url.pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role, status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true, can_restart: true },
      });
    if (p === "/api/servers" && method === "GET") return json(200, registry);
    if (p === "/api/servers" && method === "POST" && failCreate)
      return json(409, { detail: { code: "server.key_exists", message: "Server key already exists", params: {} } });
    if (p === "/api/servers" && method === "POST") {
      const row: Record<string, unknown> = { ...(body as Record<string, unknown>), id: 1, capabilities: ["console"], sort_order: 0 };
      delete row.rcon_password; // the API never returns the literal
      registry.push(row);
      return json(201, row);
    }
    // Minecraft is active, the other games are not (`GET /api/settings/games`).
    if (p === "/api/settings/games")
      return json(200, { games: [{ game_type: "pz", active: false }, { game_type: "minecraft", active: true }] });
    if (p === "/api/servers/fleet") return json(200, FLEET);
    if (p === "/api/servers/presets") return json(200, PRESETS);
    if (p === "/api/servers/mc-1/setup")
      return json(200, {
        key: "mc-1",
        enabled: state.enabled,
        management_mode: "dashboard",
        container_name: "mc-1",
        container: state.containerExists
          ? { exists: true, running: false, status: "created" }
          : { exists: false, running: false, status: "missing" },
        create_command: "docker compose exec console-api python -m app.cli create-container mc-1",
        secret: { source: "stored", env_name: "", env_set: false, stored: true, unreadable: false },
      });
    if (p === "/api/servers/mc-1/container" && method === "POST") {
      state.containerExists = true;
      if (background) {
        state.provisioned = true;
        return json(200, { ...PROVISION_OK, status: "running", detail: "", result: {}, running: true, finished_at: null });
      }
      return json(200, PROVISION_OK); // the fake operation finishes at once
    }
    if (p === "/api/servers/mc-1/provision") return json(200, state.provisioned ? PROVISION_OK : {});
    if (p === "/api/servers/mc-1" && method === "PUT") {
      state.enabled = (body as { enabled: boolean }).enabled;
      return json(200, { ...registry[0], enabled: state.enabled });
    }
    if (p === "/api/servers/mc-1/start") return startError ? json(409, { detail: startError }) : json(200, { ok: true });
    return json(404, { detail: { code: "server.not_found", message: "not found", params: {} } });
  });
  return { calls, state, registry, fetchMock };
}

function renderApp(path = "/") {
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

describe("first server", () => {
  it("goes from the empty fleet page to a created server and its next steps", async () => {
    const backend = fakeBackend();
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/");

    // The empty state asks for the active games first (one more request before the button shows).
    fireEvent.click(await screen.findByRole("button", { name: "Add your first server" }));
    fireEvent.click(await screen.findByRole("button", { name: "Minecraft" }));

    // The preset's values are in the form, editable.
    expect((screen.getByDisplayValue("itzg/minecraft-server") as HTMLInputElement).disabled).toBe(false);
    // The console password is typed here by default (stored encrypted); the env name stays one
    // click away with the preset's name in it.
    fireEvent.click(screen.getByRole("radio", { name: "Env variable in the .env" }));
    expect(screen.getByDisplayValue("MC_RCON_PW")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Enter it here" }));
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "s3cret-typed-here" } });

    fireEvent.change(screen.getByPlaceholderText("e.g. survival-1"), { target: { value: "mc-1" } });
    fireEvent.change(screen.getByDisplayValue("25565"), { target: { value: "24065" } });

    // Without accepting the EULA nothing is sent.
    fireEvent.click(screen.getByRole("button", { name: "Create server" }));
    expect(await screen.findByText("Accept the licence terms above.")).toBeTruthy();
    expect(backend.calls.some((c) => c.method === "POST")).toBe(false);

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create server" }));

    // The next-steps panel of the new server, with the exact command.
    expect((await screen.findByTestId("create-command")).textContent).toBe(
      "docker compose exec console-api python -m app.cli create-container mc-1",
    );
    const post = backend.calls.find((c) => c.method === "POST" && c.path === "/api/servers");
    expect(post?.body).toMatchObject({
      key: "mc-1",
      game_type: "minecraft",
      enabled: false,
      container_name: "mc-1",
      data_dir: "/data/games/mc-1",
      rcon_host: "mc-1",
      rcon_port: 25575,
      rcon_password_env: "",
      rcon_password: "s3cret-typed-here",
      game_ports: { game: "24065/tcp" },
      server_type: "PAPER",
      game_version: "26.2",
      extra: { env: { SERVER_PORT: "24065", MOTD: "A Paper Minecraft server" } },
      eula_accepted: true,
    });
    // Nothing asked for an overview of a server that has no container.
    expect(backend.calls.some((c) => c.path.endsWith("/overview"))).toBe(false);

    // "Create container" runs the provisioning operation; the page says it worked, even though
    // the panel that showed the operation goes away with the "Create" step.
    fireEvent.click(screen.getByRole("button", { name: "Create container" }));
    expect(await screen.findByText(CREATED)).toBeTruthy();
    expect(backend.calls.find((c) => c.method === "POST" && c.path === "/api/servers/mc-1/container")?.body).toEqual({
      eula_accepted: false, // recorded with the row already (`extra.eula_accepted`)
    });

    // The container now exists: the page offers "Enable and start" by itself.
    fireEvent.click(await screen.findByRole("button", { name: "Enable and start" }));
    await waitFor(() =>
      expect(backend.calls.some((c) => c.method === "POST" && c.path === "/api/servers/mc-1/start")).toBe(true),
    );
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put).toMatchObject({ path: "/api/servers/mc-1", body: { enabled: true } });
  });

  it("says so when the container was created in the background", async () => {
    // As with the real API: the POST answers with a running operation (the image was already
    // local, so it is done by the next poll), and the step's panel disappears with the step.
    const backend = fakeBackend("admin", false, true);
    backend.registry.push({
      id: 1, key: "mc-1", game_type: "minecraft", display_name: "", sort_order: 0, enabled: false,
      management_mode: "dashboard", container_name: "mc-1", console_kind: "source_rcon", capabilities: ["console"],
      game_ports: { game: "24065/tcp" }, extra: { eula_accepted: true },
    });
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1");

    fireEvent.click(await screen.findByRole("button", { name: "Create container" }));
    expect(await screen.findByText(CREATED)).toBeTruthy();
    // The page asks for the operation's progress right after starting it, not one poll interval
    // (2 s) later: a fast operation (the image is local) otherwise sits on "preparing".
    const post = backend.calls.find((c) => c.method === "POST" && c.path === "/api/servers/mc-1/container")!;
    const firstPoll = backend.calls.find((c) => c.path === "/api/servers/mc-1/provision" && c.query.startsWith("?id="))!;
    expect(firstPoll.at - post.at).toBeLessThan(1500);
    expect(await screen.findByRole("button", { name: "Enable and start" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create container" })).toBeNull();
  });

  it("switches the server back off when its start is refused, and says why", async () => {
    // A port another program holds is only found out by Docker at the start. "Enable and start"
    // is one step: if the start is refused, the server must not stay enabled but stopped.
    const refused = {
      code: "server.port_in_use_host",
      message: "x",
      params: { key: "mc-1", reason: "Bind for 0.0.0.0:24065 failed: port is already allocated" },
    };
    const backend = fakeBackend("admin", false, false, refused);
    backend.state.containerExists = true;
    backend.registry.push({
      id: 1, key: "mc-1", game_type: "minecraft", display_name: "", sort_order: 0, enabled: false,
      management_mode: "dashboard", container_name: "mc-1", console_kind: "source_rcon", capabilities: ["console"],
      game_ports: { game: "24065/tcp" }, extra: { eula_accepted: true },
    });
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1");

    fireEvent.click(await screen.findByRole("button", { name: "Enable and start" }));
    const alert = await screen.findByText(/could not be started, so it stays switched off/);
    expect(alert.textContent).toContain("A port of 'mc-1' is already in use on the host");
    await waitFor(() => expect(backend.state.enabled).toBe(false));
    const puts = backend.calls.filter((c) => c.method === "PUT").map((c) => c.body);
    expect(puts).toEqual([{ enabled: true }, { enabled: false }]);
    // Still the setup page: fix the port under "Edit server", then enable and start again.
    expect(screen.getByRole("button", { name: "Enable and start" })).toBeTruthy();
  });

  it("refuses an invalid key in the browser already", async () => {
    const backend = fakeBackend();
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/servers/new");

    fireEvent.click(await screen.findByRole("button", { name: "Minecraft" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. survival-1"), { target: { value: "-bad" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create server" }));
    expect(await screen.findByText("The key does not match the rule below its field.")).toBeTruthy();
    expect(backend.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("shows the backend's error text when creating fails", async () => {
    const backend = fakeBackend("admin", true);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/servers/new");

    fireEvent.click(await screen.findByRole("button", { name: "Minecraft" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. survival-1"), { target: { value: "mc-1" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create server" }));
    expect(await screen.findByText("Server key already exists")).toBeTruthy();
  });
});

describe("empty states", () => {
  it("the status page says there is no server instead of asking for one", async () => {
    const backend = fakeBackend("member");
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/status");

    expect(await screen.findByText("An admin has not set up a game server yet.")).toBeTruthy();
    // No "Add" button for a member, and no request with a placeholder key.
    expect(screen.queryByRole("button", { name: "Add your first server" })).toBeNull();
    expect(backend.calls.some((c) => /\/api\/servers\/[^/]+\/(overview|history|mods)/.test(c.path))).toBe(false);
  });

  it("the server switcher offers the first server to an admin", async () => {
    const backend = fakeBackend();
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/join");

    expect(await screen.findByText("No game server yet")).toBeTruthy();
    const sidebar = document.querySelector("aside") as HTMLElement;
    expect(within(sidebar).getByRole("link", { name: "Add server" }).getAttribute("href")).toBe("/servers/new");
    expect(backend.calls.some((c) => c.path.includes("/join-info"))).toBe(false);
  });
});
