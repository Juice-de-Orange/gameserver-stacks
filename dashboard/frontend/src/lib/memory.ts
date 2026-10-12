// The RAM figure of a running server: the container's memory (`mem_bytes` — for a JVM game the
// heap plus the JVM itself plus native memory) against the container's memory limit.
//
// Not against the heap ceiling (-Xmx): that is a different quantity, and a Minecraft server with
// a 1 GiB heap uses ~1.6 GiB of container memory as a matter of course — the fleet showed it as
// "145 %" in red, permanently. Not against Docker's `mem_limit` either: without a limit that is
// the host's total RAM. Without a limit there is nothing to compare against, so no share.

export interface MemoryStats {
  mem_bytes?: number;
  mem_container_limit?: number | null;
  mem_max_heap?: number | null;
}

export function memoryShare(stats: MemoryStats | undefined): { used: number; cap: number | null; share: number | null } {
  const used = stats?.mem_bytes ?? 0;
  const cap = stats?.mem_container_limit || null;
  return { used, cap, share: cap ? used / cap : null };
}
