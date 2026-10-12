# Vintage Story runner — fetches the game version named in VERSION at boot.
#
# Why this exists: `devidian/vintagestory` bakes the game in at BUILD time (`ARG vs_version`) and
# ignores any runtime env. The dashboard versions Vintage Story via the VERSION env
# (`VersionSource.env`), so with that image every version change, rollback and nightly patch
# update was a silent no-op (VERSION=1.22.7 set, game reported v1.21.6).
#
# Published by CI on every release as ghcr.io/juice-de-orange/stacks-console-vintagestory-runner
# (tag `1`), which new Vintage Story servers use by default (GSS_VS_RUNNER_IMAGE). The dashboard
# pulls it when it creates the container; it cannot build images (the socket proxy has BUILD=0).
# To build it yourself on the Docker host:
#   docker build -t stacks-console/vintagestory-runner:1 dashboard/images/vintagestory
# and set GSS_VS_RUNNER_IMAGE=stacks-console/vintagestory-runner:1 in the dashboard's .env.
#
# linux/amd64 only: the Vintage Story server ships x86_64 native libraries.
FROM mcr.microsoft.com/dotnet/runtime:10.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq \
 && rm -rf /var/lib/apt/lists/*

# 1.22.x targets .NET 10; 1.21.x targets .NET 8 and runs here via roll-forward.
ENV VS_DATA_PATH=/gamedata/vs \
    VS_BIN_CACHE=/gamedata/server-bin \
    VS_KEEP_VERSIONS=3 \
    DOTNET_ROLL_FORWARD=Major \
    PUID=1000 \
    PGID=1000 \
    TERM=xterm

COPY entrypoint.sh /usr/local/bin/vs-entrypoint.sh
RUN chmod 0755 /usr/local/bin/vs-entrypoint.sh

EXPOSE 42420
# Same as upstream: the server shuts down cleanly on SIGINT.
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/local/bin/vs-entrypoint.sh"]
