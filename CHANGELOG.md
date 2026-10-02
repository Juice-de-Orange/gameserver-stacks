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
