# steamcmd helper — downloads one Steam Workshop item with an anonymous login, then exits.
#
# Why this is a separate image: steamcmd is a 32-bit program that needs the i386 architecture and
# `lib32gcc-s1`. Only Project Zomboid workshop mods use it, so it is not part of the API image.
# The dashboard pulls this image the first time an active game needs a workshop item
# (`backend/app/services/steam_download.py`) and runs it as a short-lived container with the
# API's uid/gid and the cache `<DATA_ROOT>/.steamcmd` mounted at /steam.
#
# Published by CI as ghcr.io/juice-de-orange/stacks-console-steamcmd. To build it yourself:
#   docker build -t stacks-console/steamcmd:1 dashboard/images/steamcmd
# and set GSS_STEAMCMD_IMAGE=stacks-console/steamcmd:1 in the dashboard's .env.
#
# linux/amd64 only: steamcmd has no ARM build.
FROM debian:13.7-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# The bootstrap goes to /opt/steamcmd (read-only at run time). The entrypoint copies it into the
# cache on the first run, where steamcmd updates itself — so the self-update (about 200 MB on
# disk) is downloaded once per installation, not once per item.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl lib32gcc-s1 \
 && mkdir -p /opt/steamcmd \
 && curl -fsSL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" \
      | tar zxf - -C /opt/steamcmd \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/steamcmd-download
RUN chmod 0755 /usr/local/bin/steamcmd-download

# Never root. The dashboard overrides the ids with its own (`User` in the create payload).
ENV HOME=/steam
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/steamcmd-download"]
