/**
 * The edit page: what changes, what needs a new container, switching off and removing.
 *
 * The form logic is tested as plain functions (`changesOf`, `needsRecreate`, `editProblems`);
 * the page runs in the whole app against a small fake backend.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import type { ServerInfo } from "../lib/game";
import { changesOf, editProblems, formFromServer, needsRecreate } from "./EditServer";
import type { Preset } from "./AddServer";

const MC_PRESET = {
  game_type: "minecraft",
  image_repo: "itzg/minecraft-server",
  image_tag: "t",
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
  values: [],
  acknowledgements: [],
} as Preset;

function row(over: Partial<ServerInfo> = {}): ServerInfo {
  return {
    id: 1, key: "mc-1", game_type: "minecraft", display_name: "Survival", sort_order: 0, enabled: true,
    management_mode: "dashboard", container_name: "mc-1", data_dir: "/data/games/mc-1",
    image_repo: "itzg/minecraft-server", image_tag: "t", network: "net", console_kind: "source_rcon",
    rcon_host: "mc-1", rcon_port: 25575, rcon_password_env: "", game_ports: { game: "24511/tcp" },
    game_version: "26.2", server_type: "PAPER", ram_mb: 5120, cpu_cores: 0, heap_max_mb: 4096, heap_min_mb: 4096,
    idle_autostop_enabled: true, idle_timeout_minutes: null, bridge_in: true, bridge_out: true,
    presence_visible: true, announce_target: true, capabilities: ["console", "announce"],
    extra: { env: { SERVER_PORT: "24511", MOTD: "hi" }, eula_accepted: true }, updated_at: "2026-10-12T10:00:00",
    ...over,
  };
}

describe("the form logic", () => {
  it("a rename alone needs no new container", () => {
    const s = row();
    const body = changesOf(s, { ...formFromServer(s), displayName: "Creative" }, MC_PRESET);
    expect(body).toEqual({ display_name: "Creative" });
    expect(needsRecreate(s, body)).toBe(false);
  });

  it("a port change moves the port and the variable the game listens on, and recreates", () => {
    const s = row();
    const f = formFromServer(s);
    const body = changesOf(s, { ...f, ports: { game: "24512" } }, MC_PRESET);
    expect(body.game_ports).toEqual({ game: "24512/tcp" });
    expect(body.extra).toEqual({ env: { SERVER_PORT: "24512", MOTD: "hi" }, eula_accepted: true });
    expect(needsRecreate(s, body)).toBe(true);
  });

  it("the public address is no container value", () => {
    const s = row();
    const body = changesOf(s, { ...formFromServer(s), publicHost: "play.example.com" }, MC_PRESET);
    expect(body).toEqual({ extra: { ...s.extra, public_host: "play.example.com" } });
    expect(needsRecreate(s, body)).toBe(false);
  });

  it("refuses a port outside 1..65535 and a host with a slash", () => {
    const s = row();
    expect(editProblems(s, { ...formFromServer(s), ports: { game: "70000" } })).toEqual(["problem.port"]);
    expect(editProblems(s, { ...formFromServer(s), publicHost: "http://x" })).toEqual(["problem.publicHost"]);
  });

  it("the bind address is a container value and must be an IP address", () => {
    const s = row();
    const f = formFromServer(s);
    expect(f.bindAddress).toBe("0.0.0.0");
    const body = changesOf(s, { ...f, bindAddress: "127.0.0.1" }, MC_PRESET);
    expect(body).toEqual({ bind_address: "127.0.0.1" });
    expect(needsRecreate(s, body)).toBe(true);
    expect(changesOf(row({ bind_address: "127.0.0.1" }), { ...f, bindAddress: "" }, MC_PRESET)).toEqual({ bind_address: "0.0.0.0" });
    expect(editProblems(s, { ...f, bindAddress: "localhost" })).toEqual(["problem.bindAddress"]);
  });
});

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function fakeBackend(server: ServerInfo, putError?: unknown) {
  const calls: Call[] = [];
  let current = server;
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
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true, can_restart: true },
      });
    if (p === "/api/servers" && method === "GET") return json(200, current ? [current] : []);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "minecraft", active: true }] });
    if (p === "/api/servers/presets") return json(200, { key_pattern: ".*", data_root: "/data/games", network: "net", presets: [MC_PRESET] });
    if (p === `/api/servers/${current.key}/setup`)
      return json(200, {
        key: current.key, enabled: current.enabled, management_mode: "dashboard", container_name: current.key,
        container: { exists: true, running: current.enabled, status: current.enabled ? "running" : "exited" },
        create_command: "x", secret: { source: "stored", env_name: "", env_set: false, stored: true, unreadable: false },
        ...(current.game_type === "pz"
          ? { secrets: { admin: { source: "none", env_name: "", env_set: false, stored: false, unreadable: false } } }
          : {}),
      });
    if (p === `/api/servers/${current.key}/secret`)
      return json(200, { source: "stored", env_name: "", env_set: false, stored: true, unreadable: false });
    if (p === "/api/servers/mc-1" && method === "PUT") {
      if (putError) return json(409, { detail: putError });
      current = { ...current, ...(body as Partial<ServerInfo>), updated_at: `${Date.now()}` };
      return json(200, current);
    }
    if (p === "/api/servers/mc-1/recreate")
      return json(200, { id: 3, kind: "version.change", status: "running", step: "preparing", current: 0, total: 0,
        percent: null, detail: "", result: {}, running: true, started_at: "x", finished_at: null });
    if (p === "/api/servers/mc-1/remove") {
      current = undefined as unknown as ServerInfo;
      return json(200, { key: "mc-1", data_deleted: false, container_removed: true });
    }
    return json(200, {});
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

beforeEach(() => localStorage.setItem("gss_access", "token"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the edit page", () => {
  it("renames and changes the port, says it recreates, and calls the recreate path", async () => {
    const backend = fakeBackend(row());
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1/edit");

    const name = await screen.findByDisplayValue("Survival");
    fireEvent.change(name, { target: { value: "Creative" } });
    fireEvent.change(screen.getByLabelText('Port "game" (TCP)'), { target: { value: "24512" } });
    expect(await screen.findByText(/These changes need a new container/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save and recreate" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and recreate" }));

    await waitFor(() =>
      expect(backend.calls.some((c) => c.method === "POST" && c.path === "/api/servers/mc-1/recreate")).toBe(true),
    );
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.body).toMatchObject({ display_name: "Creative", game_ports: { game: "24512/tcp" } });
  });

  it("shows a port conflict from the backend with the server that holds the port", async () => {
    const conflict = {
      code: "server.port_in_use_server",
      message: "x",
      params: { port: "0.0.0.0:24512/tcp", holder: "mc-2", theirs: "0.0.0.0:24512/tcp" },
      holder: "mc-2",
    };
    const backend = fakeBackend(row({ enabled: false }), conflict);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1/edit");

    fireEvent.change(await screen.findByLabelText('Port "game" (TCP)'), { target: { value: "24512" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and recreate" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Save and recreate" }));
    expect(await screen.findByText(/Port 0\.0\.0\.0:24512\/tcp is already used by server 'mc-2'/)).toBeTruthy();
    expect(backend.calls.some((c) => c.path === "/api/servers/mc-1/container")).toBe(false);
  });

  it("stores Project Zomboid's in-game admin password in its own slot", async () => {
    const backend = fakeBackend(row({ key: "pz-1", game_type: "pz", container_name: "pz-1", game_ports: { game: "16261/udp" } }));
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/pz-1/edit");

    expect((await screen.findByTestId("secret-status-admin")).textContent).toMatch(
      /uses the console password/,
    );
    fireEvent.change(screen.getByLabelText("New admin password"), { target: { value: "admin-pw" } });
    fireEvent.submit(screen.getByLabelText("New admin password").closest("form")!);
    await waitFor(() =>
      expect(backend.calls.find((c) => c.path === "/api/servers/pz-1/secret")?.body).toEqual({ value: "admin-pw", slot: "admin" }),
    );
  });

  it("switches off through PUT enabled=false after a confirmation", async () => {
    const backend = fakeBackend(row());
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1/edit");

    fireEvent.click(await screen.findByRole("button", { name: "Switch off" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch off" }));
    await waitFor(() => expect(backend.calls.find((c) => c.method === "PUT")?.body).toEqual({ enabled: false }));
  });

  it("removes a switched-off server only after its key is typed", async () => {
    const backend = fakeBackend(row({ enabled: false }));
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/s/mc-1/edit");

    const button = await screen.findByRole("button", { name: "Remove, keep data" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Type mc-1 to confirm"), { target: { value: "mc-1" } });
    fireEvent.click(screen.getByRole("radio", { name: /Delete the data/ }));
    fireEvent.click(screen.getByRole("button", { name: "Remove and delete data" }));
    await waitFor(() =>
      expect(backend.calls.find((c) => c.path === "/api/servers/mc-1/remove")?.body).toEqual({
        confirm_key: "mc-1",
        delete_data: true,
      }),
    );
  });
});
