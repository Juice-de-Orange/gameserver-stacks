/**
 * A page that builds its text with plain helpers (`fmtCountdown`, `featureAvailability`, …) has
 * no translation hook of its own, so nothing would re-render it when the language changes. `App`
 * subscribes for all of them: a language change re-renders the route elements.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { AuthProvider } from "./auth";
import i18n, { setLanguage } from "./i18n";
import { fmtCountdown } from "./lib/format";

// Stands in for a page without `useTranslation`: its only text comes from a plain helper.
vi.mock("./pages/Login", () => ({
  default: () => <div data-testid="probe">{fmtCountdown(0)}</div>,
}));

afterEach(async () => {
  cleanup(); // unmount first: the language reset below must not update a mounted tree
  await i18n.changeLanguage("en");
  localStorage.removeItem("gss_lang");
});

describe("App", () => {
  it("re-renders the visible page when the language changes", async () => {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("any moment");
    act(() => setLanguage("de"));
    expect((await screen.findByText("jeden Moment")).textContent).toBe("jeden Moment");
  });
});
