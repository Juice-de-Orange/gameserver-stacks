import { describe, expect, it } from "vitest";

import { memoryShare } from "./memory";

const GiB = 1024 ** 3;

describe("the RAM figure of a running server", () => {
  it("is measured against the container's memory limit", () => {
    expect(memoryShare({ mem_bytes: 1.6 * GiB, mem_container_limit: 2 * GiB, mem_max_heap: 1 * GiB })).toEqual({
      used: 1.6 * GiB,
      cap: 2 * GiB,
      share: 0.8,
    });
  });

  it("never against the Java heap: the container's memory is heap plus JVM plus native", () => {
    // The fleet used to show "1.6 GB / 1.1 GB · 145 %" for a Minecraft server with -Xmx1G.
    expect(memoryShare({ mem_bytes: 1.6 * GiB, mem_container_limit: null, mem_max_heap: 1 * GiB })).toEqual({
      used: 1.6 * GiB,
      cap: null,
      share: null,
    });
  });

  it("knows nothing without a figure", () => {
    expect(memoryShare(undefined)).toEqual({ used: 0, cap: null, share: null });
  });
});
