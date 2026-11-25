# Changelog

Hearth uses [semantic versioning](https://semver.org/). Each release on GitHub carries the section
below for its version. Update with `git pull`, then run `start.sh` or `start.bat`; nodes on other
machines need their agent updated too (Admin → Nodes says when one is behind).

## Unreleased

### Added
- Create a server from a modpack zip you downloaded yourself (Modrinth `.mrpack` or CurseForge
  `.zip`): Hearth reads the manifest, picks the right Java, and the image installs the loader, mods
  and config on first start. Stopping a server that is still starting no longer waits out the
  full stop timeout.

## 0.2.0

### Added
- New games: Terraria (TShock), Factorio, Palworld, Satisfactory and Rust, and a Velocity proxy
  that links Paper/Purpur servers with modern forwarding (Network tab of the proxy).
- Starter packs when creating a server: Paper + Geyser/Floodgate (with the Bedrock port), or
  Fabric with Lithium, FerriteCore, Krypton and ModernFix.
- Off-site copies of backups to any S3-compatible storage, fetching them back onto a node, and
  nightly backups of the panel database.
- Disk limits enforced on uploads, downloads and unpacking, with warnings at 90% and 100%.
- Admin alerts (Discord): crash loops (the server is stopped), full disks, failed backups,
  machines offline or low on disk.
- Maintenance pause, and moving a server to another node.
- Player playtime, sessions and peaks on the Players tab.
- Console search, command history that survives reloads, and one-click latest.log and crash
  reports.
- A first-run setup wizard, a light theme, and a French translation.
- Mods: required dependencies that are already installed aren't downloaded again, and mods you
  added yourself are put back after a modpack update.
- Automated checks on GitHub Actions: unit, API and browser tests on every push; a weekly and
  on-push run against real Docker with a real Paper server; `start.sh` and `start.bat` runs.
- Version shown in Admin → Overview, with a notice when a newer release is out, and a warning on
  nodes whose agent is older than the panel.

### Fixed
- Uploading a file through the panel failed: the request to the node was built with two sets
  of headers.

## 0.1.0

First release: multi-node panel and agent; Minecraft Java (any version, Modrinth and CurseForge
modpacks with auto-update), Bedrock, Valheim and custom images; backups, schedules, players, mods,
sharing, quotas, two-factor sign-in, API tokens, webhooks, Discord notifications, auto-sleep with
wake-on-join, playit.gg tunnels, world import and export, and a security review.
