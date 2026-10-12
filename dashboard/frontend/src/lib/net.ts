// Network values typed into the server forms. The backend checks them again
// (`services/ports.py`); this only saves a round trip for an obvious typo.

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
// Loose on purpose: hex groups and colons, at most one `::`. The backend parses it exactly.
const IPV6 = /^(?=.*:)[0-9a-fA-F:.]{2,45}$/;

/** A bind address: empty (= 0.0.0.0), an IPv4 or an IPv6 address — never a host name. */
export function isBindAddress(value: string): boolean {
  const v = value.trim();
  if (!v) return true;
  if (IPV4.test(v)) return true;
  return IPV6.test(v) && (v.match(/::/g) ?? []).length <= 1;
}

/** The address to connect to as `host:port`, IPv6 in brackets; empty when either part is missing. */
export function connectAddress(host: string | null | undefined, port: number | null | undefined): string {
  if (!host || !port) return "";
  return host.includes(":") && !host.startsWith("[") ? `[${host}]:${port}` : `${host}:${port}`;
}
