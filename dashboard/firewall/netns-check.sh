#!/bin/sh
# End-to-end check of console-firewall against a real iptables, in a THROWAWAY network namespace.
#
#   sh dashboard/firewall/netns-check.sh
#
# Builds the helper image and runs tests/netns_check.py in a container with a network namespace of
# its own (`--network none`), every capability dropped except NET_ADMIN, and no-new-privileges.
# NET_ADMIN then covers that namespace only: the host's firewall is never read or written. The
# script refuses to run the check if Docker reports any other network mode or a privileged
# container. Kernel modules are not per namespace: the check uses only matches and targets the
# helper and Docker use themselves, so that it does not make the host load new ones.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
image=stacks-console-firewall:netns-check
name=console-firewall-netns-check-$$

docker build -q -t "$image" "$here" >/dev/null
docker run -d --name "$name" --network none --cap-drop ALL --cap-add NET_ADMIN \
  --security-opt no-new-privileges -v "$here:/src:ro" --entrypoint sleep "$image" 600 >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

mode=$(docker inspect -f '{{.HostConfig.NetworkMode}} privileged={{.HostConfig.Privileged}} caps={{.HostConfig.CapAdd}}' "$name")
echo "container: $mode"
case "$mode" in
  "none privileged=false caps=[CAP_NET_ADMIN]"|"none privileged=false caps=[NET_ADMIN]") ;;
  *) echo "refusing: the check must run in its own network namespace, unprivileged" >&2; exit 1 ;;
esac

docker exec -e PYTHONDONTWRITEBYTECODE=1 "$name" python3 /src/tests/netns_check.py
