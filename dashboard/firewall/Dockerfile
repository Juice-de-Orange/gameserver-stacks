# console-firewall — the opt-in firewall helper of Stacks Console (compose profile `firewall`).
#
# Opens the published game ports of the enabled servers in Docker's DOCKER-USER chain, through a
# chain of its own (GSS-ALLOW) and nothing else. Runs in the host's network namespace with the
# capability NET_ADMIN only; no Docker socket, no network port (the API talks to it over a Unix
# socket in a volume the two share). Trust boundary: dashboard/docs/security.md.
#
# Python standard library only; both iptables variants (nf_tables and legacy), because the helper
# must write with the one Docker uses on the host (gss_firewall/iptables.py decides which).
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8

RUN apk add --no-cache python3 iptables ip6tables iptables-legacy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY gss_firewall ./gss_firewall

# Runs as root inside the container: Docker grants the added capability to root's processes.
# Compose drops every other capability and sets no-new-privileges.
ENTRYPOINT ["python3", "-m", "gss_firewall"]
CMD ["serve"]
