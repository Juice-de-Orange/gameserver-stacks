/**
 * Vintage Story without its optional RCON mod: the console page says why it cannot work and how
 * to set the mod up, instead of a bare "RCON unreachable" after the first command.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import ServerConsole from "./ServerConsole";

const server = { key: "vs-1", game_type: "vintagestory", display_name: "Vintage Story" };

vi.mock("../components/ServerContext", () => ({ useActiveServer: () => server }));
vi.mock("../lib/ws", () => ({ wsUrl: async () => "ws://test/ws/console" }));

/** A socket that delivers the given messages once it is open. */
function fakeSocket(messages: unknown[]) {
  return class {
    static OPEN = 1;
    readyState = 1;
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    constructor() {
      setTimeout(() => {
        this.onopen?.();
        for (const m of messages) this.onmessage?.({ data: JSON.stringify(m) });
      }, 0);
    }
    send() {}
    close() {}
  };
}

beforeEach(() => {
  // jsdom has no layout: the page scrolls its log box after every line.
  Element.prototype.scrollTo = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the console of a Vintage Story server without the RCON mod", () => {
  it("explains the missing mod and links the set-up guide", async () => {
    const reason = {
      code: "console.vintagestory_rcon_mod_missing",
      message: "Vintage Story has no remote console of its own.",
      params: {},
    };
    vi.stubGlobal("WebSocket", fakeSocket([
      { type: "error", message: reason },
      { type: "info", message: "Connected as admin (full mode)" },
    ]));
    render(<ServerConsole />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(screen.getByText("The console needs a server mod")).toBeTruthy();
    expect(screen.getByText(/third-party server mod “Vintage RCON”/)).toBeTruthy();
    const guide = screen.getByRole("link", { name: "Set-up guide" }) as HTMLAnchorElement;
    expect(guide.href).toContain("games/vintage-story/README.md#optional-remote-console-with-the-vintage-rcon-mod");
    const mod = screen.getByRole("link", { name: /mod database/ }) as HTMLAnchorElement;
    expect(mod.href).toBe("https://mods.vintagestory.at/vintagercon");
    // The reason is the card, not one more ✖ line in the log.
    expect(screen.queryByText(/✖/)).toBeNull();
  });

  it("shows no card when the server has its console", async () => {
    vi.stubGlobal("WebSocket", fakeSocket([{ type: "info", message: "Connected as admin (full mode)" }]));
    render(<ServerConsole />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(screen.queryByText("The console needs a server mod")).toBeNull();
  });

  it("asks for a new password in both places, and names the field on the edit page", async () => {
    // The stored console password is never shown, so "set the mod's password to the console
    // password" could not be followed. The card names the edit page's own labels; reading them
    // from that page's catalog keeps the two from drifting apart in either language.
    for (const lng of ["en", "de"]) {
      const t = i18n.getFixedT(lng);
      const howTo = t("serverConsole:vsMod.howTo");
      expect(howTo).toContain(t("serverSetup:secret.title"));
      expect(howTo).toContain(t("serverSetup:secret.label"));
      expect(howTo).toContain("vsrcon.json");
    }
    expect(i18n.getFixedT("en")("serverConsole:vsMod.howTo")).not.toMatch(/set its password to the console password/);
  });
});
