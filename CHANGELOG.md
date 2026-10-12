# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Game blocks: Arma Reforger, Avorion, Factorio 2.0, Minecraft (Paper), Project Zomboid (Build 42),
  Valheim, Vintage Story — each with a pinned image, required secrets, internal-only consoles and a
  `backup.list`.
- `stack` helper: up/down/logs/rcon, consistent backups (stop → archive → start), restore with the
  previous data kept aside, `stop-all`/`start-recorded` for host reboots, `check` for CI.
- Docs: block conventions, operations guide (DOCKER-USER allow-list, clean shutdown, backups,
  updates); systemd and firewall examples.
- CI: compose check of every block (fail-closed secrets), shellcheck, gitleaks.
- **Stacks Console** (`dashboard/`), a web dashboard for the same seven games on one Docker host:
  - Install with Docker Compose; prebuilt API and web images on GHCR (amd64 + arm64) from the
    first release, `--build` builds them from the checkout. `GSS_VERSION` pins a release.
  - Game activation: the first start asks which games to run; **Settings → Games** changes it
    later (`GSS_GAMES` seeds it). Inactive games download nothing; the steamcmd helper image is
    pulled only when a workshop download needs it.
  - Full server lifecycle in the browser: add (values from the game blocks), create the
    container with pull progress, enable and start, edit (recreated with a backup and a boot
    check where needed), switch off (stops the container), remove keeping or deleting the data.
    `python -m app.cli create-container` does the create step without a browser.
  - Ports: bind address per server, conflict checks against other servers and running
    containers, the players' address under **Settings → Network**.
  - Reachability panel per server: checklist, firewall commands, a check from inside and an
    opt-in check from outside (`GSS_REACHABILITY_PROBES`).
  - Optional HTTPS front (compose profile `https`: automatic certificates, redirect, HSTS) and
    an optional firewall helper (profile `firewall`: ACCEPT rules for the published game ports
    in a chain of its own, previewed and applied from the UI).
  - Console with safe and full mode, mods, version changes with a boot check, scheduled
    restarts and announcements, weekly world backups, a restore that replaces the world (with a
    safety backup first and a boot check after), player statistics, tickets,
    a cross-server chat bridge, a watchdog, an audit log, roles and per-user permissions.
  - Game passwords entered in the UI are stored encrypted (`GSS_SECRET_KEY`); an environment
    variable still wins.
  - Nightly database dump sidecar (`DB_BACKUP_*`), graceful stop/start around host reboots with
    `examples/systemd/stacks-console.service`.
  - Configurable for more than one installation per host: no fixed container names
    (`COMPOSE_PROJECT_NAME`), `GAME_NETWORK`, `DASHBOARD_BIND`, `DB_DATA_DIR`; unknown settings
    with a project prefix are reported at start.
  - English and German interface; error messages carry codes and are translated in the browser.
  - Docs: `dashboard/README.md`, `dashboard/docs/operations.md` (HTTPS with Caddy, nginx,
    Traefik and Cloudflare Tunnel examples, firewall, backups and restore, updates, logs, host
    reboots), `dashboard/docs/security.md`, `dashboard/docs/reachability.md`.
- CI for the dashboard (backend lint and tests, frontend type checks, tests and build, compose
  check, image builds) and a release workflow that publishes the images to GHCR.
- Stacks Console `./setup`: writes `.env` (mode 600) with generated secrets, creates
  `DATA_ROOT` owned by the invoking user, handles a uid other than 1000 (`APP_UID`, `--build`),
  `--games`, `--port`, `--data-root`. No `sudo`, no network, safe to run again. The quick start
  is now `./setup` and `docker compose up -d`.
- Stacks Console: a healthcheck for `console-api` (answers once its start-up migrations ran and
  the database responds), so `docker compose up -d --wait` returns only when the CLI can be
  used. The systemd unit relies on it.
- Stacks Console CI: the firewall helper image is published with the release
  (`stacks-console-firewall`, amd64 + arm64); the firewall, steamcmd and Vintage Story runner
  images are built on every pull request (job `helper-images`), and the firewall helper's
  network-namespace check runs there (job `firewall-netns`). The compose check builds its env
  file with `./setup`, and shellcheck covers `dashboard/setup`.

### Changed

- Stacks Console: "Enable and start" is all or nothing. When the start is refused (a port held
  by a program on the host, the mod boot gate), the server is switched off again and the reason
  stays on the page.
- Stacks Console: one player list per server, at most 15 s old, is shared by the fleet page,
  the status strip, the join page, metrics and session tracking, so a game's log is no longer
  flooded with console connections. The watchdog, the idle auto-stop, the auto-update and the
  boot check still ask the game directly.
- Stacks Console: a server's memory is shown against its container's memory limit, not
  against the Java heap; without a limit no percentage is shown. The setting is called
  "Memory limit" everywhere.
- Stacks Console: recreating a container is an operation of its own ("Recreating the
  container"), with backups named `pre-recreate-<version>`. Older `pre-version-*` archives stay
  listed and restorable.
- Stacks Console: Project Zomboid backups include `db/` (accounts, access levels, bans,
  whitelist). "Restore the configuration only" still restores `Server/` alone; restoring an
  archive made before this change leaves the current `db/` untouched.
- Stacks Console: "Back up now" stops a Valheim server for the archive and starts it again, like
  the weekly backup (Valheim cannot be told to save).
- Stacks Console: Arma Reforger keeps the installed game and SteamCMD in its data directory
  (`install/`, `steamcmd/`), so a recreate no longer downloads the game again (measured: about
  3 minutes of downtime instead of about 16). Its boot check waits for an answer over BattlEye
  RCON. A Reforger server created before this change keeps the old layout until its container
  is created anew; see the upgrade note in `dashboard/docs/operations.md`.
- Stacks Console: the Vintage Story runner prepares its folders as root and then runs the
  download and the game as the API's uid, so it leaves no root-owned files.
- Stacks Console: Project Zomboid's two ports can be edited. The ports of Vintage Story and
  Avorion stay fixed, and the form says why; the dashboard README has a games table.
- Stacks Console: files a game wrote as root (Arma Reforger, older Vintage Story runners) are
  deleted through a short-lived helper container when a server is removed with its data, and
  after a restore. Removing a server or recreating its container also removes the container's
  anonymous volumes.

### Fixed

- Stacks Console: a Vintage Story server without the RCON mod is stopped for a backup (like
  Valheim) instead of archiving the running world without a save point; German texts say
  "Benutzername" throughout.
- Stacks Console: the watchdog no longer restarts a server whose first SteamCMD download takes
  longer than the 15-minute boot window: the window counts from the last download progress line
  (seen with Valheim). Project Zomboid's stop no longer logs a false "could not be shut down
  through the console" warning. A scheduled restart with a backup archives Valheim after its
  stop instead of copying the running server's last autosave. Reachability check errors are
  translated (they were shown in English in the German UI). The Vintage Story console card names
  the fields where the mod's password goes. The Reforger save-swap helper keeps only the
  capabilities it needs and is removed with its anonymous volumes.
- Stacks Console: Vintage Story without the optional "Vintage RCON" mod boots, restores,
  changes versions and recreates (its ready line in the log proves the boot) instead of rolling
  back after 15 minutes; the console page explains the mod, and the watchdog leaves such a
  server alone.
- Stacks Console: a Minecraft server added again on a kept data directory gets the new console
  password written into `server.properties`; before, console, backups and recreates failed.
- Stacks Console: a recreate and a version change write the port and console password into the
  game's own config file (Minecraft, Arma Reforger) before the new container is created, so a
  drifted file no longer makes the boot check fail.
- Stacks Console: Valheim worlds in the newer directory format are listed, selected and removed;
  the version page shows Valheim's worlds and a Recreate button. A Steam query that answers late
  on a new world's first start is reported as such.
- Stacks Console: a recreate that failed and was rolled back says that the saved settings are
  not applied, and the edit page keeps warning until a later recreate succeeds.
- Stacks Console: a failed pull of the Vintage Story runner image names the local build command.
- Stacks Console: a normal Factorio stop no longer logs a warning that the console shutdown
  failed.
- Stacks Console: the container name is validated on create and save (2–128 characters: letters,
  digits, `_`, `.`, `-`, starting with a letter or digit) instead of failing at "Create
  container".
- Stacks Console: "Create container" shows its real step at once and ends with a success
  message, also when the image was already on the host. A recreate after an edit says the
  container was recreated with the saved settings, not that a version change completed.
- Stacks Console: a restore names a replaced world copy that could not be deleted.
- Stacks Console: the server page shows "Loading…" instead of "Stopped" and a Start button
  until the server's state is known; countdowns no longer read "19 d 24 h".
- Stacks Console: result messages with values (restore and backup summaries, mod counts) and the
  console greeting are translated; the help of Project Zomboid's password settings names the
  dashboard's encrypted password store; German leftovers in the interface; the version page no
  longer calls a fixed image tag "floating".

### Security

- Stacks Console: `GET /api/servers/<key>/status` and `/overview` returned the container's full
  environment, including console and admin passwords, to every signed-in user with the
  `can_view_status` permission (every member by default). They now return a fixed list of
  state fields.

