/**
 * An operation banner belongs to the visit that watched it. Entering a page attaches to a RUNNING
 * operation of its kind (a reload in the middle of a 15-minute version change must find it again),
 * never to one that finished earlier: a failure from an hour ago, or one started on another page,
 * stayed on top of every later visit.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useOperation } from "./Operation";

const OP = {
  id: 7, kind: "version.recreate", step: "", current: 0, total: 0, percent: null, detail: "",
  result: {}, started_at: "2026-10-12T11:38:00", finished_at: null,
};

function latest(body: unknown) {
  const fetchMock = vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useOperation on entering a page", () => {
  it("attaches to a running operation", async () => {
    latest({ ...OP, status: "running", running: true });
    const { result } = renderHook(() => useOperation("vs-1", "version.recreate"));
    await waitFor(() => expect(result.current.op?.id).toBe(7));
  });

  it("leaves a finished one alone", async () => {
    const fetchMock = latest({ ...OP, status: "failed", running: false, finished_at: "2026-10-12T11:53:31" });
    const { result } = renderHook(() => useOperation("vs-1", "version.recreate"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(result.current.op).toBeNull();
  });

  it("still shows the outcome of an operation this visit started", async () => {
    latest({ ...OP, status: "failed", running: false, finished_at: "2026-10-12T11:53:31" });
    const { result } = renderHook(() => useOperation("vs-1", "version.recreate"));
    result.current.track({ id: 7 });
    await waitFor(() => expect(result.current.op?.status).toBe("failed"));
  });
});
