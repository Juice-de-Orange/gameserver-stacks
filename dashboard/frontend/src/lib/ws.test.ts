// `wsUrl` builds every WebSocket address of the app. A wrong separator here means the server no
// longer sees the `?server=` parameter and closes the socket, so the separator is pinned down.
// It also pins what travels in the URL: a single-use ticket, never the access token.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { wsUrl } from "./ws";

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ api: apiMock, getAccess: () => "ACCESS-TOKEN" }));

function setLocation(protocol: string, host: string) {
  Object.defineProperty(window, "location", {
    value: { protocol, host },
    writable: true,
    configurable: true,
  });
}

function ticketResponse(ticket: string) {
  return { ok: true, json: async () => ({ ticket, expires_in: 30 }) };
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue(ticketResponse("TICKET123"));
});
afterEach(() => setLocation("http:", "localhost"));

describe("wsUrl", () => {
  it("picks the protocol that matches the page", async () => {
    setLocation("https:", "dashboard.example.com");
    expect(await wsUrl("/ws/logs")).toBe("wss://dashboard.example.com/ws/logs?ticket=TICKET123");
    setLocation("http:", "localhost:5173");
    expect(await wsUrl("/ws/logs")).toBe("ws://localhost:5173/ws/logs?ticket=TICKET123");
  });

  it("appends the ticket with & if the path already has a query", async () => {
    setLocation("https:", "h");
    // With "?" instead of "&" `server` would vanish from the path and the router would close.
    expect(await wsUrl("/ws/logs?server=mc")).toBe("wss://h/ws/logs?server=mc&ticket=TICKET123");
    expect(await wsUrl("/ws/console?server=pz-1")).toBe(
      "wss://h/ws/console?server=pz-1&ticket=TICKET123",
    );
  });

  it("requests a fresh ticket for every connection and never puts the access token in the URL", async () => {
    setLocation("https:", "h");
    apiMock.mockResolvedValueOnce(ticketResponse("FIRST")).mockResolvedValueOnce(ticketResponse("SECOND"));
    const first = await wsUrl("/ws/cross-chat");
    const second = await wsUrl("/ws/cross-chat");
    expect(first).toBe("wss://h/ws/cross-chat?ticket=FIRST");
    expect(second).toBe("wss://h/ws/cross-chat?ticket=SECOND");
    expect(apiMock).toHaveBeenCalledTimes(2);
    expect(apiMock).toHaveBeenCalledWith("/auth/ws-ticket", { method: "POST" });
    expect(first + second).not.toContain("ACCESS-TOKEN");
    expect(first + second).not.toContain("token=");
  });

  it("returns a URL without a ticket instead of throwing when none can be had", async () => {
    setLocation("https:", "h");
    apiMock.mockResolvedValueOnce({ ok: false, json: async () => ({}) });
    expect(await wsUrl("/ws/logs?server=mc")).toBe("wss://h/ws/logs?server=mc");
    apiMock.mockRejectedValueOnce(new Error("network down"));
    expect(await wsUrl("/ws/logs?server=mc")).toBe("wss://h/ws/logs?server=mc");
  });
});
