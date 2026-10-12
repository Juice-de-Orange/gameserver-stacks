/**
 * The toast has to survive EVERY payload — including the ones the backend really sends.
 *
 * For `POST /api/servers/{key}/{start,restart}` the backend returns an OBJECT as `detail` in two
 * cases: the RAM budget (`deficit_mb`, `stop_candidates`) and the mod boot gate (`blockers`).
 * React cannot render an object as a child → "Objects are not valid as a React child".
 *
 * The throw happens while the `FeedbackProvider` ITSELF re-renders. It is the outermost
 * provider; the `ErrorBoundary` in `Layout.tsx` is its descendant and cannot catch its own
 * render error. The result was a white page — and nobody saw the actual message.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FeedbackProvider, useToast } from "./feedback";

/** Fires exactly one toast on the first render. `payload` is deliberately `any` — that is how
 *  it comes out of `res.json()`, and exactly why a TypeScript signature `msg: string` would not
 *  help. */
function Trigger({ payload }: { payload: any }) {
  const toast = useToast();
  let fired = false;
  if (!fired) {
    fired = true;
    queueMicrotask(() => toast.error(payload));
  }
  return <div>ready</div>;
}

async function show(payload: any) {
  render(
    <FeedbackProvider>
      <Trigger payload={payload} />
    </FeedbackProvider>,
  );
  return screen.findByRole("alert");
}

describe("toast payloads from the backend", () => {
  it("renders the RAM budget object instead of tearing the page down", async () => {
    const message = await show({
      message: "Starting exceeds the shared RAM budget by 6144 MB.",
      deficit_mb: 6144,
      stop_candidates: [{ key: "mc", ram_mb: 5120 }],
    });
    expect(message.textContent).toContain("RAM budget");
  });

  it("renders the mod boot gate object", async () => {
    const message = await show({
      message: "The configuration would damage the server.",
      blockers: [{ code: "MAP_WITHOUT_PROVIDER", subject: "Newtown" }],
    });
    expect(message.textContent).toContain("damage");
  });

  it("takes `error` instead of `message`, as the mod routes send it", async () => {
    const message = await show({ error: "Mod change failed", blockers: [] });
    expect(message.textContent).toContain("Mod change failed");
  });

  it("translates a coded error by its code and keeps the extra fields out of the text", async () => {
    const message = await show({
      code: "auth.token_invalid",
      message: "Invalid or expired token",
      params: {},
      blockers: [],
    });
    expect(message.textContent).toContain("Invalid or expired token");
  });

  it("leaves a plain string unchanged", async () => {
    const message = await show("Server restarted.");
    expect(message.textContent).toContain("Server restarted.");
  });

  it("invents nothing if there is no message in it at all", async () => {
    const message = await show({ deficit_mb: 6144 });
    // No "[object Object]", but no invented euphemism either: the raw data is ugly but true —
    // and better than a white page.
    expect(message.textContent).not.toContain("[object Object]");
    expect(message.textContent).toContain("6144");
  });

  it("falls back to a translated text for an empty payload", async () => {
    const message = await show("");
    expect(message.textContent).toContain("Unknown error.");
  });
});
