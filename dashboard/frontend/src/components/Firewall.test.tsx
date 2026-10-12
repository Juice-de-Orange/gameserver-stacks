/**
 * The firewall helper's panel (Settings → Network) and its line on a server's page: the preview
 * comes first, "Apply" sends back exactly the previewed plan's id, and a stale preview is
 * reloaded. The whole app runs; only `fetch` is replaced by a small fake backend.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { AuthProvider } from "../auth";
import { FirewallServerStatus, FirewallState } from "./Firewall";

const RULE = "-A GSS-ALLOW -p tcp -m conntrack --ctstate DNAT --ctorigdstport 25565 --ctdir ORIGINAL -j ACCEPT";
const ALLOW = { server: "mc-1", port: 25565, proto: "tcp", address: "" };

function ready(changes: number, planId = "plan-1"): FirewallState {
  const family = {
    status: "ok" as const,
    plan: changes
      ? { create_chain: true, hook: "insert" as const, add: [RULE], remove: [], changes: 3 }
      : { create_chain: false, hook: "ok" as const, add: [], remove: [], changes: 0 },
  };
  return {
    status: "ready",
    desired: { allows: [ALLOW], sources: [] },
    error: null,
    plan: {
      plan_id: planId,
      backend: "nft",
      families: { ipv4: family, ipv6: { status: "absent" } },
      allows: [{ ...ALLOW, state: changes ? "opens" : "open" }],
      closes: changes ? [{ server: "vh-1", port: 2456, proto: "udp", address: "" }] : [],
      changes,
      applied: changes ? undefined : 3,
    },
  };
}

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function fakeBackend(states: FirewallState[], applyStatus = 200, role = "admin") {
  const calls: Call[] = [];
  let current = 0;
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
        id: 1, display_name: "Admin", username: "admin", role, status: "active",
        must_change_pw: false, flags: {}, effective_flags: { can_view_status: true },
      });
    if (p === "/api/servers" && method === "GET") return json(200, []);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "minecraft", active: true }] });
    if (p === "/api/settings/network")
      return json(200, { public_host: "", stored: "", env: "", source: "none" });
    if (p === "/api/settings/firewall") return json(200, states[Math.min(current, states.length - 1)]);
    if (p === "/api/settings/firewall/apply") {
      if (applyStatus !== 200)
        return json(applyStatus, {
          detail: { code: "firewall.plan_changed", message: "changed", params: {} },
        });
      current += 1;
      return json(200, states[Math.min(current, states.length - 1)]);
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

describe("the firewall panel", () => {
  it("says it is off and how to switch it on, and still lists what it would open", async () => {
    const off: FirewallState = { status: "off", desired: { allows: [ALLOW], sources: [] }, plan: null, error: null };
    vi.stubGlobal("fetch", fakeBackend([off]).fetchMock);
    renderApp("/settings/network");

    expect(await screen.findByText(/Off\. To switch it on, set GSS_FIREWALL_TOKEN/)).toBeTruthy();
    expect(screen.getByTestId("firewall-ports").textContent).toContain("25565/tcp");
    expect(screen.queryByRole("button", { name: /^Apply/ })).toBeNull();
  });

  it("shows the rule diff first and applies exactly the previewed plan", async () => {
    const backend = fakeBackend([ready(3), ready(0, "plan-2")]);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/settings/network");

    const diff = await screen.findByTestId("firewall-diff-ipv4");
    expect(diff.textContent).toContain(`+ ${RULE}`);
    expect(diff.textContent).toContain("+ create the chain GSS-ALLOW");
    expect(screen.getByText("IPv6: no DOCKER-USER chain (Docker's IPv6 firewall is off) — left alone.")).toBeTruthy();
    expect(screen.getByTestId("firewall-closes").textContent).toBe("Closes on apply: vh-1 2456/udp");
    expect(screen.getByText("opens on apply")).toBeTruthy();
    expect(backend.calls.some((c) => c.path === "/api/settings/firewall/apply")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Apply 3 changes" }));
    await waitFor(() => expect(screen.getByText("The firewall matches the servers.")).toBeTruthy());
    expect(backend.calls.find((c) => c.path === "/api/settings/firewall/apply")?.body).toEqual({ plan_id: "plan-1" });
    expect(screen.getByText("open")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Apply 0 changes" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("reloads the preview when the plan changed in between", async () => {
    const backend = fakeBackend([ready(3)], 409);
    vi.stubGlobal("fetch", backend.fetchMock);
    renderApp("/settings/network");

    fireEvent.click(await screen.findByRole("button", { name: "Apply 3 changes" }));
    expect(await screen.findByText("The rules or the servers changed since the preview. Check the new preview and apply again.")).toBeTruthy();
    await waitFor(() => expect(backend.calls.filter((c) => c.path === "/api/settings/firewall").length).toBe(2));
  });

  it("shows the helper's reasons when it refuses the state", async () => {
    const refused: FirewallState = {
      status: "error",
      desired: { allows: [{ ...ALLOW, port: 80 }], sources: [] },
      plan: null,
      error: {
        code: "firewall.refused", message: "x", params: { reason: "port 80" },
        problems: ["allows[0]: port 80 is outside the allowed range 1024-65535 (GSS_FIREWALL_PORT_RANGE)"],
      },
    };
    vi.stubGlobal("fetch", fakeBackend([refused]).fetchMock);
    renderApp("/settings/network");

    expect(await screen.findByText(/The firewall helper refuses this state: port 80/)).toBeTruthy();
    expect(screen.getByText(/port 80 is outside the allowed range/)).toBeTruthy();
  });
});

describe("the firewall line on a server's page", () => {
  function renderStatus() {
    return render(
      <MemoryRouter>
        <AuthProvider>
          <FirewallServerStatus serverKey="mc-1" />
        </AuthProvider>
      </MemoryRouter>,
    );
  }

  it("tells an admin which ports still need an apply", async () => {
    vi.stubGlobal("fetch", fakeBackend([ready(3)]).fetchMock);
    renderStatus();
    expect((await screen.findByTestId("firewall-server-status")).textContent).toContain(
      "Firewall: 25565/tcp not open yet — needs an apply.",
    );
  });

  it("is not shown to other roles and asks nothing", async () => {
    const backend = fakeBackend([ready(3)], 200, "member");
    vi.stubGlobal("fetch", backend.fetchMock);
    renderStatus();
    await waitFor(() => expect(backend.calls.some((c) => c.path === "/api/auth/me")).toBe(true));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("firewall-server-status")).toBeNull();
    expect(backend.calls.some((c) => c.path === "/api/settings/firewall")).toBe(false);
  });
});
