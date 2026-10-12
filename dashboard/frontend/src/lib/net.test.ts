import { describe, expect, it } from "vitest";
import { connectAddress, isBindAddress } from "./net";

describe("isBindAddress", () => {
  it.each(["", "0.0.0.0", "127.0.0.1", "192.0.2.10", "::", "::1", "2001:db8::10"])("accepts %s", (v) => {
    expect(isBindAddress(v)).toBe(true);
  });
  it.each(["localhost", "play.example.com", "256.1.1.1", "1.2.3", "1::2::3", "192.0.2.1/24"])("refuses %s", (v) => {
    expect(isBindAddress(v)).toBe(false);
  });
});

describe("connectAddress", () => {
  it("joins host and port, IPv6 in brackets", () => {
    expect(connectAddress("play.example.com", 25565)).toBe("play.example.com:25565");
    expect(connectAddress("2001:db8::10", 2456)).toBe("[2001:db8::10]:2456");
  });
  it("is empty without a host or a port", () => {
    expect(connectAddress("", 25565)).toBe("");
    expect(connectAddress("play.example.com", 0)).toBe("");
  });
});
