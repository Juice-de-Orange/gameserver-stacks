/**
 * The reachability panel: checklist, firewall commands, and the outside check that names what it
 * sends before it can run. `fetch` is replaced by a small fake backend.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ReachabilityPanel, type Reachability } from "./ReachabilityPanel";

function checklist(over: Partial<Reachability> = {}): Reachability {
  return {
    key: "mc-1",
    game_type: "minecraft",
    ports: [{ name: "game", port: 25565, proto: "tcp" }],
    bind: { address: "0.0.0.0", scope: "all" },
    public: { host: "play.example.com", source: "installation", kind: "name" },
    commands: {
      ufw: ["sudo ufw route allow proto tcp from any to any port 25565"],
      iptables: ["sudo iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport 25565 --ctdir ORIGINAL -j ACCEPT"],
      nftables: ["sudo nft insert rule inet filter forward tcp dport 25565 accept"],
    },
    outside: {
      enabled: true,
      cache_s: 60,
      services: [
        { id: "steam", name: "Steam Web API", url: "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/", sends: "ip", enabled: true },
        { id: "mcstatus", name: "mcstatus.io", url: "https://api.mcstatus.io/v2/status/java/", sends: "ip_port", enabled: true },
        { id: "portcheck", name: "portchecker.io", url: "https://portchecker.io/api/query", sends: "ip_ports", enabled: false },
      ],
      plan: [{ name: "game", port: 25565, proto: "tcp", service: "mcstatus", state: "on" }],
    },
    ...over,
  };
}

const INSIDE_OK = {
  ran: true,
  reason: null,
  published: [{ name: "game", port: 25565, proto: "tcp", ok: true, detail: null }],
  answers: [{ channel: "rcon", port: 25575, ok: true, latency_ms: 4.2, error: null }],
};

function backend(data: Reachability, outside: unknown = undefined, inside: unknown = INSIDE_OK) {
  const calls: string[] = [];
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://localhost").pathname;
    calls.push(`${(init?.method ?? "GET").toUpperCase()} ${path}`);
    if (path === "/api/servers/mc-1/reachability") return json(200, data);
    if (path === "/api/servers/mc-1/reachability/outside") return json(200, outside);
    if (path === "/api/servers/mc-1/reachability/inside") return json(200, inside);
    return json(404, {});
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function open() {
  render(
    <MemoryRouter>
      <ReachabilityPanel serverKey="mc-1" />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: /Reachability/ }));
}

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("ReachabilityPanel", () => {
  it("loads nothing until it is opened", () => {
    const calls = backend(checklist());
    render(
      <MemoryRouter>
        <ReachabilityPanel serverKey="mc-1" />
      </MemoryRouter>,
    );
    expect(calls).toEqual([]);
  });

  it("shows ports, the public address, and says Docker bypasses ufw", async () => {
    backend(checklist());
    open();
    expect((await screen.findByTestId("reachability-ports")).textContent).toContain("25565/TCP");
    expect(screen.getByTestId("reachability-public").textContent).toBe(
      "Players connect to play.example.com (the installation's public address).",
    );
    expect(screen.getByText(/Docker bypasses ufw/)).toBeTruthy();
    expect(screen.getByTestId("reachability-commands").textContent).toContain("DOCKER-USER");
    fireEvent.click(screen.getByRole("tab", { name: "ufw" }));
    expect(screen.getByTestId("reachability-commands").textContent).toBe(
      "sudo ufw route allow proto tcp from any to any port 25565",
    );
  });

  it("a loopback bind is not reachable from outside by design: no commands, no router hints", async () => {
    backend(
      checklist({
        bind: { address: "127.0.0.1", scope: "loopback" },
        commands: { ufw: [], iptables: [], nftables: [] },
      }),
    );
    open();
    expect((await screen.findByTestId("reachability-bind")).textContent).toMatch(/not reachable from outside by design/);
    expect(screen.queryByTestId("reachability-commands")).toBeNull();
    expect(screen.queryByText(/Router and cloud firewall/)).toBeNull();
  });

  it("says what goes to which service before the outside check, then shows the result", async () => {
    const calls = backend(checklist(), {
      ran: true,
      reason: null,
      address: "198.51.100.7",
      ports: [
        {
          name: "game", port: 25565, proto: "tcp", service: "mcstatus", status: "reachable",
          detail: {
            code: "reachability.minecraft_online",
            message: "x",
            params: { address: "198.51.100.7:25565" },
          },
        },
      ],
    });
    open();
    const disclosure = await screen.findByTestId("reachability-disclosure");
    expect(disclosure.textContent).toContain(
      "mcstatus.io (https://api.mcstatus.io/v2/status/java/) receives the public IPv4 address and the game port.",
    );
    expect(disclosure.textContent).toContain("Steam Web API (https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/) receives the public IPv4 address.");
    expect(disclosure.textContent).toContain("portchecker.io: switched off, receives nothing.");
    expect(calls).not.toContain("POST /api/servers/mc-1/reachability/outside");

    fireEvent.click(screen.getByRole("button", { name: "Check from outside" }));
    const result = await screen.findByTestId("reachability-outside");
    expect(result.textContent).toContain("reachable");
    expect(result.textContent).toContain("mcstatus.io reached the Minecraft server at 198.51.100.7:25565 from the internet.");
  });

  it("offers no outside check while it is switched off", async () => {
    backend(checklist({ outside: { ...checklist().outside, enabled: false } }));
    open();
    expect((await screen.findByTestId("reachability-outside-off")).textContent).toMatch(/GSS_REACHABILITY_PROBES=on/);
    expect(screen.queryByRole("button", { name: "Check from outside" })).toBeNull();
    expect(screen.queryByTestId("reachability-disclosure")).toBeNull();
  });

  it("checks from inside", async () => {
    backend(checklist());
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Check from inside" }));
    await waitFor(() =>
      expect(screen.getByTestId("reachability-inside").textContent).toContain("RCON on port 25575 answers (4.2 ms)."),
    );
  });

  it("says in the reader's language why the game does not answer", async () => {
    // The backend sends the reason coded; before, the raw English sentence reached the German UI.
    backend(checklist(), undefined, {
      ran: true,
      reason: null,
      published: [],
      answers: [
        {
          channel: "query",
          port: null,
          ok: false,
          latency_ms: null,
          error: {
            code: "server.valheim_query_not_open_yet",
            message: "Valheim has not opened its Steam query yet. It does so once the world is ready.",
            params: {},
          },
        },
      ],
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Check from inside" }));
    await waitFor(() =>
      expect(screen.getByTestId("reachability-inside").textContent).toContain(
        "Valheim has not opened its Steam query yet. It does so once the world is ready (log line",
      ),
    );
    await act(async () => {
      await i18n.changeLanguage("de");
    });
    expect(screen.getByTestId("reachability-inside").textContent).toContain(
      "Valheim hat seine Steam-Abfrage noch nicht geöffnet.",
    );
  });

  it("names a timeout with its number", async () => {
    backend(checklist(), undefined, {
      ran: true,
      reason: null,
      published: [],
      answers: [
        {
          channel: "rcon",
          port: 25575,
          ok: false,
          latency_ms: null,
          error: { code: "reachability.probe_timeout", message: "no answer within 5 s", params: { seconds: "5" } },
        },
      ],
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Check from inside" }));
    await waitFor(() =>
      expect(screen.getByTestId("reachability-inside").textContent).toContain(
        "RCON on port 25575 does not answer: no answer within 5 s",
      ),
    );
  });
});
