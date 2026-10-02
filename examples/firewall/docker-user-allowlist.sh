#!/bin/sh
# Example: allow only the listed game ports from the internet; drop everything else Docker publishes
# on the external interface. Docker's published ports bypass UFW/firewalld, but every forwarded
# packet passes the DOCKER-USER chain first, so the allow-list belongs there.
#
# In DOCKER-USER the destination is already translated to the container, so `--dport` would match
# the container port. `--ctorigdstport` matches the port the client actually connected to.
#
# Run as root after Docker has started (see docker-user-allowlist.service). Test from outside before
# you rely on it. IPv6: repeat with ip6tables if Docker publishes on IPv6.
set -eu

EXT_IF="${EXT_IF:-eth0}"            # your public interface: ip route show default
ALLOW_TCP="${ALLOW_TCP:-25565}"     # space-separated ports or ranges (a:b)
ALLOW_UDP="${ALLOW_UDP:-34197 2456:2458}"

iptables -F DOCKER-USER
iptables -A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
iptables -A DOCKER-USER ! -i "$EXT_IF" -j RETURN
for port in $ALLOW_TCP; do
  iptables -A DOCKER-USER -i "$EXT_IF" -p tcp -m conntrack --ctorigdstport "$port" -j RETURN
done
for port in $ALLOW_UDP; do
  iptables -A DOCKER-USER -i "$EXT_IF" -p udp -m conntrack --ctorigdstport "$port" -j RETURN
done
iptables -A DOCKER-USER -i "$EXT_IF" -j DROP
