/**
 * The language switch in the header changes the visible labels in place — no reload, no remount.
 *
 * The navigation labels are the classic trap: a label table at module level keeps the language
 * it was imported in, and the switch would then change everything but the navigation.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../auth";
import i18n from "../i18n";
import { FeedbackProvider } from "./feedback";
import { Shell } from "./Layout";
import { LiveConsoleProvider } from "./liveconsole";
import { OverviewProvider } from "./overview";
import { ActiveServerProvider, ServersProvider } from "./ServerContext";

/** The shell with the providers of `App.tsx`. Signed out, so nothing is fetched. */
function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <AuthProvider>
        <FeedbackProvider>
          <ServersProvider>
            <ActiveServerProvider>
              <OverviewProvider>
                <LiveConsoleProvider>
                  <Shell>
                    <input aria-label="probe" defaultValue="" />
                  </Shell>
                </LiveConsoleProvider>
              </OverviewProvider>
            </ActiveServerProvider>
          </ServersProvider>
        </FeedbackProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

// Components below the shell may poll on their own; no request may leave the test.
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
});

afterEach(async () => {
  cleanup(); // unmount first: the language reset below must not update a mounted tree
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
  localStorage.removeItem("gss_lang");
});

describe("language switch", () => {
  it("re-renders the navigation in the other language without a reload", async () => {
    renderShell();
    expect(screen.getAllByText("Fleet").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Sign out").length).toBeGreaterThan(0);
    expect(document.title).toContain("Fleet");

    // State typed into the page must survive the switch: the page is re-rendered, not remounted.
    const probe = screen.getByLabelText("probe") as HTMLInputElement;
    fireEvent.change(probe, { target: { value: "typed before the switch" } });

    fireEvent.click(screen.getByRole("button", { name: "Switch language to Deutsch" }));

    expect((await screen.findAllByText("Leitstand")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Fleet")).toBeNull();
    expect(screen.getAllByText("Abmelden").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Leitstelle").length).toBeGreaterThan(0);
    expect(document.documentElement.lang).toBe("de");
    expect(document.title).toContain("Leitstand");
    expect(localStorage.getItem("gss_lang")).toBe("de");
    expect((screen.getByLabelText("probe") as HTMLInputElement).value).toBe("typed before the switch");

    // And back: the button now offers English.
    fireEvent.click(screen.getByRole("button", { name: "Sprache auf English umstellen" }));
    expect((await screen.findAllByText("Fleet")).length).toBeGreaterThan(0);
    expect(document.documentElement.lang).toBe("en");
  });
});
