/**
 * "Sign out everywhere" on the account page.
 *
 * `POST /api/auth/logout-all` revokes every token of the account, the one of this session
 * included. Three things must hold: nothing is sent without the confirmation, the local sign-out
 * happens only after the backend has confirmed, and a failure leaves the session alone and is
 * shown.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FeedbackProvider } from "../components/feedback";
import ChangePassword from "./ChangePassword";

const { api, logout } = vi.hoisted(() => ({ api: vi.fn(), logout: vi.fn() }));

vi.mock("../api", async (original) => ({ ...(await original<typeof import("../api")>()), api }));
vi.mock("../auth", () => ({
  useAuth: () => ({ me: { must_change_pw: false }, reload: vi.fn(), logout }),
}));

function response(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function show() {
  render(
    <MemoryRouter>
      <FeedbackProvider>
        <ChangePassword />
      </FeedbackProvider>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));
}

/** The dialog's confirm button carries the same label as the button that opened it. */
function confirmDialog() {
  const buttons = screen.getAllByRole("button", { name: "Sign out everywhere" });
  fireEvent.click(buttons[buttons.length - 1]!);
}

describe("sign out everywhere", () => {
  beforeEach(() => {
    api.mockReset();
    logout.mockReset();
  });

  it("asks first and sends nothing when the dialog is cancelled", async () => {
    show();
    expect(await screen.findByText("Sign out everywhere?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Sign out everywhere?")).toBeNull());
    expect(api).not.toHaveBeenCalled();
    expect(logout).not.toHaveBeenCalled();
  });

  it("calls the endpoint and then signs out locally", async () => {
    api.mockResolvedValue(response(200, { ok: true }));
    show();
    await screen.findByText("Sign out everywhere?");
    confirmDialog();
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    expect(api).toHaveBeenCalledWith("/auth/logout-all", { method: "POST" });
  });

  it("keeps the session and shows the error when the backend refuses", async () => {
    api.mockResolvedValue(
      response(401, { detail: { code: "auth.token_invalid", message: "Invalid or expired token", params: {} } }),
    );
    show();
    await screen.findByText("Sign out everywhere?");
    confirmDialog();
    expect(await screen.findByText("Invalid or expired token")).toBeTruthy();
    expect(logout).not.toHaveBeenCalled();
  });

  it("keeps the session when the server is not reachable", async () => {
    api.mockRejectedValue(new TypeError("Failed to fetch"));
    show();
    await screen.findByText("Sign out everywhere?");
    confirmDialog();
    expect(await screen.findByText("Server not reachable — please try again later.")).toBeTruthy();
    expect(logout).not.toHaveBeenCalled();
  });
});
