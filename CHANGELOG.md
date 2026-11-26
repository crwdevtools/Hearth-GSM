# Changelog

Hearth uses [semantic versioning](https://semver.org/). Each release on GitHub carries the section
below for its version. Update with `git pull`, then run `start.sh` or `start.bat`; nodes on other
machines need their agent updated too (Admin → Nodes says when one is behind).

## Unreleased

### Fixed
- A stray "null" printed on the Files, Backups and other tabs; tables no longer push the page
  wider than a phone screen (they scroll instead).
- Copy buttons now work when the panel is opened over plain http on the local network.
- The console stops adding a red "can't reach the node" line (and stops retrying every second)
  while a node is down.
- A server that stopped while the panel was down no longer keeps counting playtime.
- Names made only of spaces are refused for servers, nodes, schedules and API tokens; a file or
  folder name over 255 bytes is refused instead of causing a server error on the node.
- Statistics: network speeds under 1 byte/s no longer read "undefined", and switching the time
  range no longer shows the previous range's window or an out-of-order reply.

### Added
- Player counts now come from a Minecraft status ping, so an empty server no longer gets a `list`
  command (and a log line) every minute; `list` runs only when the count changes or every 5 minutes
  while players are on, and whenever the ping can't reach the server.
- Alerts (Discord webhooks and the admin alert channel): TPS under 15 for 5 minutes, and a disk
  that will reach its limit within 3 days at the last day's growth.
- Console statistics: 1 h / 24 h / 7 d range switch; players online with the day's peak, TPS and
  tick time (Paper `tps`, vanilla/Fabric/NeoForge `tick query`, Forge `forge tps`; a server that
  doesn't answer is left alone), network speed, disk growth with a "fills in about N days" estimate,
  restart and crash markers, and recent joins and leaves. History is kept for 8 days and charts
  break where the server was off. JVM heap/GC and loaded chunks are not included yet.
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
