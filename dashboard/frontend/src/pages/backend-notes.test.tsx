/**
 * Notes the backend sends and the UI has to show where the action happens: the whitelist that
 * has no effect, the server that is OFF after a failed restart, what went wrong after a
 * successful operation, a budget figure that is only a lower bound, a running version nobody
 * knows. Each of them used to arrive in a field no page rendered.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { detailText } from "../api";
import { AuthProvider } from "../auth";
import { OperationPanel, type OperationState } from "../components/Operation";
import i18n from "../i18n";

const SERVER = {
  id: 1, key: "av-1", game_type: "avorion", display_name: "Galaxy", sort_order: 0, enabled: true,
  management_mode: "dashboard", container_name: "av-1", data_dir: "/data/av-1", image_repo: "x",
  image_tag: "1", network: "net", console_kind: "source_rcon", rcon_host: "av-1", rcon_port: 27015,
  rcon_password_env: "", game_ports: { game: "27000/udp" }, game_version: "2.5", server_type: "",
  ram_mb: 0, cpu_cores: 0, heap_max_mb: null, heap_min_mb: null, idle_autostop_enabled: false,
  idle_timeout_minutes: null, bridge_in: false, bridge_out: false, presence_visible: false,
  announce_target: false, capabilities: ["console", "moderation", "versions"],
};

const WHITELIST_NOTE =
  "“accessListMode” is set to “Blacklist” — the whitelist currently has NO effect. It can be switched in the " +
  "configuration, which for Avorion can only be changed while the server is stopped.";

function fakeBackend() {
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  return vi.fn((input: RequestInfo | URL) => {
    const p = new URL(String(input), "http://localhost").pathname;
    if (p === "/api/auth/me")
      return json(200, {
        id: 1, display_name: "Admin", username: "admin", role: "admin", status: "active",
        must_change_pw: false, flags: {}, effective_flags: {},
      });
    if (p === "/api/servers") return json(200, [SERVER]);
    if (p === "/api/settings/games") return json(200, { games: [{ game_type: "avorion", active: true }] });
    if (p === "/api/servers/av-1/moderation/spec")
      return json(200, { kick: true, ban: true, unban: true, whitelist: true, access_levels: [] });
    if (p === "/api/servers/av-1/moderation/whitelist")
      // Coded (`moderation.avorion_whitelist_inactive`): the page fills in the mode itself.
      return json(200, {
        available: true,
        note: { code: "moderation.avorion_whitelist_inactive", message: "(English from the backend)", params: { mode: "Blacklist" } },
        users: [],
      });
    if (p === "/api/budget/usage")
      return json(200, {
        total_ram_mb: 8192, used_ram_mb: 2048, free_ram_mb: 6144, running: [],
        demand_unknown: ["av-1"],
      });
    if (p === "/api/budget") return json(200, { default_idle_timeout_minutes: 30, mem_headroom_pct: 25 });
    if (p === "/api/servers/av-1/resources")
      return json(200, {
        ram_mb: 0, cpu_cores: 0, heap_max_mb: null, heap_min_mb: null,
        idle_autostop_enabled: false, idle_timeout_minutes: null, live: {},
      });
    if (p === "/api/servers/av-1/version/available")
      return json(200, {
        supported: true, entries: [], current: "", update_available: null, source_ok: true,
        running_version_known: false, checked_at: null, note: "",
      });
    if (p === "/api/servers/av-1/version/history") return json(200, []);
    return json(404, { detail: { code: "server.not_found", message: "not found", params: {} } });
  });
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

describe("backend notes reach the page", () => {
  it("the whitelist says when it has no effect", async () => {
    vi.stubGlobal("fetch", fakeBackend());
    renderApp("/s/av-1/moderation");
    expect(await screen.findByText(WHITELIST_NOTE)).toBeTruthy();
  });

  it("the budget says when its figure is only a lower bound", async () => {
    vi.stubGlobal("fetch", fakeBackend());
    renderApp("/s/av-1/resources");
    expect(await screen.findByText(/The usage of av-1 cannot be measured right now/)).toBeTruthy();
  });

  it("the version page says when the running version is unknown", async () => {
    vi.stubGlobal("fetch", fakeBackend());
    renderApp("/s/av-1/version");
    expect(await screen.findByText(/The running version cannot be determined/)).toBeTruthy();
  });

  it("a failed restart after an error is part of the error text", () => {
    const warning = "WARNING: the server was running before and is now OFF — the restart failed (x).";
    expect(detailText({ error: "mandatory backup failed", restart_warning: warning })).toBe(
      `mandatory backup failed | ${warning}`,
    );
    // A coded detail carries the warning in its own message already — no second copy.
    expect(
      detailText({
        code: "x.unknown_code", message: `cause | ${warning}`, params: {}, restart_warning: warning,
      }),
    ).toBe(`cause | ${warning}`);
  });

  it("the operation panel shows what went wrong after a successful run", () => {
    const op: OperationState = {
      id: 1, kind: "mods.apply", status: "ok", step: "", current: 0, total: 0, percent: null,
      detail: "", running: false, started_at: "2026-10-11T12:00:00", finished_at: "2026-10-11T12:01:00",
      result: { note: "The change is applied and the server is running — only the history could not be written (db gone)." },
    };
    render(<OperationPanel op={op} title="Mod change" />);
    expect(screen.getByText(/only the history could not be written/)).toBeTruthy();
  });

  it("a summary and a note with values in them are translated by their code", async () => {
    const op: OperationState = {
      id: 2, kind: "backup.restore", status: "ok", step: "", current: 0, total: 0, percent: null,
      detail: "Backup b.tar.gz restored", running: false, started_at: "2026-10-11T12:00:00",
      finished_at: "2026-10-11T12:01:00",
      result: {
        summary: { code: "restore.done", message: "Backup b.tar.gz restored", params: { name: "b.tar.gz" } },
        note: {
          code: "version.completion_failed",
          message: "The new version is running — only the completion (cleaning up old images, writing the history) failed: db gone",
          params: { error: "db gone" },
        },
      },
    };
    await i18n.changeLanguage("de");
    try {
      render(<OperationPanel op={op} title="Einspielen" />);
      expect(screen.getByText("Backup b.tar.gz eingespielt")).toBeTruthy();
      expect(screen.getByText(/nur der Abschluss .* ist fehlgeschlagen: db gone/)).toBeTruthy();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("a restore names the old copy it could not delete, next to the other note", async () => {
    const left = "/data/games/pz-1/Saves.restore-old-20261012-101500";
    const op: OperationState = {
      id: 3, kind: "backup.restore", status: "ok", step: "", current: 0, total: 0, percent: null,
      detail: "The backup was restored.", running: false, started_at: "2026-10-12T10:00:00",
      finished_at: "2026-10-12T10:15:00",
      result: {
        started: false,
        note: [
          "The server was stopped before and stays stopped.",
          { code: "restore.old_copy_left", message: "(English from the backend)", params: { paths: left } },
        ],
      },
    };
    await i18n.changeLanguage("de");
    try {
      render(<OperationPanel op={op} title="Einspielen" />);
      const note = screen.getByText(/Der ersetzte Stand konnte nicht gelöscht werden/);
      expect(note.textContent).toBe(
        "Der Server war vorher gestoppt und bleibt gestoppt. " +
          `Der ersetzte Stand konnte nicht gelöscht werden und belegt weiter Platz: ${left}. Bitte von Hand entfernen.`,
      );
    } finally {
      await i18n.changeLanguage("en");
    }
  });
});
