# --- Build stage: build the Vite SPA ---
# The output is static files, so the stage runs on the build host's own platform: a multi-arch
# build then needs no emulation for npm and vite (the Caddy stage below has no RUN at all).
FROM --platform=$BUILDPLATFORM node:20-alpine AS build
WORKDIR /app
ENV NODE_OPTIONS=--max-old-space-size=1536
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# --- Serve stage: Caddy serves /srv and proxies /api and /ws ---
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
