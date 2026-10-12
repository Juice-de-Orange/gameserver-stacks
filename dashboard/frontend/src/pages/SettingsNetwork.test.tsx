/**
 * Settings → Network and the connect address: saved here over PUBLIC_HOST, shown on the fleet and
 * on a server's page. The whole app runs; only `fetch` is replaced by a small fake backend.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import { ConnectCard } from "../components/ConnectAddress";

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function fakeBackend(env = "env.example.com") {
  const calls: Call[] = [];
  let stored = "";
  const state = () => ({ public_host: stored || env, stored, env, source: stored ? "stored" : env ? "env" : "none" });
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
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "minecraft", active: true }] });
    if (p === "/api/settings/network" && method === "GET") return json(200, state());
    if (p === "/api/settings/network" && method === "PUT") {
      const value = String((body as { public_host: string }).public_host);
      if (value.includes("/"))
        return json(400, {
          detail: { code: "settings.public_host_invalid", message: "x", params: { value } },
        });
      stored = value.toLowerCase();
      return json(200, state());
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

describe("Settings → Network", () => {
  it("says the address comes from PUBLIC_HOST, saves one over it, and clears back to it", async () => {
    const backend = fakeBackend();
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/settings/network");

    expect((await screen.findByTestId("public-host-source")).textContent).toBe(
      "From PUBLIC_HOST in the environment: env.example.com",
    );
    fireEvent.change(screen.getByLabelText("Public address"), { target: { value: "Play.Example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByTestId("public-host-source").textContent).toBe("Saved here."));
    expect(backend.calls.find((c) => c.method === "PUT")?.body).toEqual({ public_host: "Play.Example.com" });

    fireEvent.click(screen.getByRole("button", { name: "Clear (use PUBLIC_HOST)" }));
    await waitFor(() => expect(screen.getByTestId("public-host-source").textContent).toMatch(/^From PUBLIC_HOST/));
  });

  it("shows the backend's reason for an address it refuses", async () => {
    vi.stubGlobal("fetch", fakeBackend().fetchMock);
    renderApp("/settings/network");

    fireEvent.change(await screen.findByLabelText("Public address"), {
      target: { value: "http://x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/'http:\/\/x' is not a host name or IP address/)).toBeTruthy();
  });
});

describe("the connect address", () => {
  it("shows host:port, or says it is missing instead of making one up", async () => {
    vi.stubGlobal("fetch", fakeBackend().fetchMock);
    const { rerender } = render(
      <MemoryRouter>
        <AuthProvider>
          <ConnectCard connect={{ host: "play.example.com", port: 25565, protocol: "tcp" }} />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("connect-address").textContent).toBe("play.example.com:25565");
    rerender(
      <MemoryRouter>
        <AuthProvider>
          <ConnectCard connect={{ host: "", port: 25565, protocol: "tcp" }} />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("connect-address")).toBeNull();
    expect(screen.getByText(/No public address is set/)).toBeTruthy();
  });
});
