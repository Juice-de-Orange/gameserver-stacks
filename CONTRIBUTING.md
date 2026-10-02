# Contributing

Thanks for helping. The most useful contributions are corrections from people who run these
servers: a port that is wrong, a setting the image renamed, a gotcha that cost you an evening.

## Before you open a pull request

- Read [docs/CONVENTIONS.md](docs/CONVENTIONS.md). A block that follows it can be reviewed quickly.
- Run `./stack check` (needs Docker with Compose v2). CI runs the same check, `shellcheck` on the
  scripts and a gitleaks secret scan.
- **Test what you change.** For a block change: start the server with the new settings, connect
  with a client, run `./stack backup <game>` and restore the archive once. Say in the pull request
  which image tag you tested and what you checked.
- **Give a source for every fact** in a README — the image's documentation, the game's
  documentation, or a run you did yourself. "I think" is fine in an issue, not in the docs.
- Keep secrets, real hostnames, IPs, server names, Steam IDs and player names out of everything,
  including logs you paste. Use `203.0.113.10` and `example.com` in examples.

## Updating an image tag

Image tags are bumped by hand (Dependabot cannot read the `${*_TAG:-…}` defaults). In the pull
request, link the image release or the game patch notes, and say whether existing worlds need
anything (a migration, new mod versions, a client update).

## Adding a game

Open a "New game" issue first, with the image you want to use and its licence. A new block needs
an image that is maintained, documented, and licensed for redistribution of the compose file that
references it; the game itself must be downloadable at runtime from Steam or the vendor — nothing
of the game goes into this repository.

## Commits

One logical change per commit, imperative subject line (`valheim: document BepInEx variables`).
By contributing you agree that your contribution is licensed under the [MIT licence](LICENSE).
Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
