# Hearth

A self-hosted control panel for running many game servers at once, across many machines —
in the spirit of Pterodactyl and Crafty Controller, built with Python and Docker.

- **One panel, many nodes.** Every machine (the one running the panel, a spare PC, a VPS from any
  provider) runs a small agent container. Local and remote hosts are managed the same way.
- **Port allocations per node.** Add ranges like `25565-25600`; each server gets a primary port
  (plus any extra ports for plugins), and each game prefers its usual port.
- **Minecraft done properly.** Vanilla, Paper, Purpur, Fabric, Quilt, Forge, NeoForge at any
  version, with the version list fetched live. Hearth picks the right Java image for the version.
- **Modpacks from Modrinth and CurseForge.** Browse a live leaderboard, deploy any pack at any
  version, and let Hearth check for new releases and restart servers onto them automatically.
- **Full server config.** Memory (with JVM heap sized for you), CPU, swap, disk accounting,
  every common `server.properties` setting, and a file editor for the rest.
- **Console, files, power** from the browser, with live stats and 24-hour CPU/memory/player graphs.
- **Backups**: manual, scheduled, and automatic before every modpack update or restore. The server
  is told to flush its world first (`save-all`, Bedrock `save hold`), so it stays online.
- **Schedules**: cron-style restarts, backups, start/stop and console commands, with in-game
  countdown warnings. Times use the panel timezone (Admin → Settings).
- **Players**: online list, whitelist, ops, bans and kicks (Minecraft Java).
- **Mods and plugins**: search Modrinth for single mods or plugins that match the server's loader
  and version; required dependencies install too.
- **Worlds**: download the world as a zip, upload one (Hearth finds `level.dat` in it), upload and
  unzip any archive in the file manager.
- **Make a copy** of a server (files included) as a test copy or a fresh start.
- **Sharing**: give friends access to one server with chosen permissions (console, power, files,
  backups, schedules, settings, players).
- **Quotas**: let users create their own servers up to a number of servers and an amount of memory.
- **Sleep when empty**: stop after N minutes with nobody online. With "wake on join", a tiny stand-in
  answers on the port, shows "starting up" in the server list, and boots the server when someone joins.
- **No port forwarding**: run the playit.gg agent on a node (Admin → Nodes → Tunnel), then paste the
  tunnel address into the server's Network tab. "Check if friends can join" tests from outside.
- **Notifications**: per-server Discord webhook (started, stopped, crashed, out of memory, updated,
  backups), plus signed webhooks to any URL for every event.
- **Security and hosting hooks**: two-factor sign-in (TOTP), API tokens for scripts, an activity log,
  and suspend/unsuspend for when you rent servers to others.
- Also included: Minecraft Bedrock, Valheim, and "any Docker image" templates.

```
             browser
                │  HTTPS (cookie session)
        ┌───────▼────────┐
        │  Hearth panel  │  FastAPI + SQLite + web UI
        └──┬──────────┬──┘
   HTTP+WS │          │ HTTPS+WSS (bearer token per node)
   ┌───────▼───┐  ┌───▼──────────┐
   │ agent     │  │ agent        │   one per machine
   │ (local)   │  │ (rented VPS) │
   └─────┬─────┘  └──────┬───────┘
     Docker            Docker  →  itzg/minecraft-server, valheim, ...
```

## Quick start (single machine)

Needs Docker with the compose plugin (Docker Desktop on Windows/macOS).

- **Linux / macOS:** `./start.sh`
- **Windows:** double-click `start.bat`

The script checks Docker (and starts Docker Desktop if it's closed), and on the first run asks
**which drive to install game servers on** (Windows lists your drives with free space; Linux/macOS
list your disks and external volumes, or take any folder) and which address players should use.
It writes `.env` with a random agent token, builds and starts everything, waits for the panel,
and opens it in your browser. Run it again any time;
`stop.sh` / `stop.bat` stop the panel (game servers keep running until you stop them in the panel).

Manual equivalent:

```sh
cp .env.example .env
# set HEARTH_AGENT_TOKEN to a random value, and HEARTH_PUBLIC_HOST to your domain or public IP
sed -i "s/^HEARTH_AGENT_TOKEN=.*/HEARTH_AGENT_TOKEN=$(openssl rand -hex 32)/" .env
docker compose up -d --build
```

`HEARTH_DATA_DIR` in `.env` is the folder that holds every server's files. To move it later:
stop Hearth and your servers, move the folder, update that line, and start again. On macOS pick a
folder under your home directory or on `/Volumes`, which Docker Desktop shares by default.

Open `http://<this machine>:8000`, create the admin account, and you'll find a node called
**local** already registered with ports 25565–25575, 19132 and 2456–2457. Click **New server**.

Open the game ports in your firewall/router (e.g. `25565-25575/tcp`). Game containers publish
their ports directly on the host; the agent itself is not exposed.

## Adding a remote machine (VPS, dedicated server, another PC)

1. In **Admin → Nodes → Add node**, enter the agent URL the panel will use
   (e.g. `https://vps.example.com:8443`) and the public address players connect to.
2. Hearth shows a `docker run …` command with the node's token. Build the agent image on that
   machine (`docker build -t hearth-agent:latest agent/`, or push it to your own registry and set
   `HEARTH_AGENT_IMAGE` on the panel so the command uses it) and run the command.
3. Add a port range on the node page and open those ports in the machine's firewall.

**Use TLS for agents on the internet.** The token controls every server on that machine. Either
put the agent behind a reverse proxy (Caddy: `reverse_proxy 127.0.0.1:8443`) or give it a
certificate directly with `-e HEARTH_TLS_CERT=/certs/fullchain.pem -e HEARTH_TLS_KEY=/certs/privkey.pem`
and a mounted cert directory. Restrict the agent port to the panel's IP if you can.

Server files can live anywhere on the node: change the left side of
`-v /var/lib/hearth:/var/lib/hearth` to any folder or drive. The agent reads its own mounts to
find where that folder is on the host (override with `HEARTH_HOST_DATA_DIR` if it can't).

## Modpacks

- **Modrinth** works out of the box.
- **CurseForge** needs a free API key from <https://console.curseforge.com/>. Paste it in
  **Admin → Settings** (or set `CURSEFORGE_API_KEY`).

Pick a pack under **Modpacks** (or in the New server form), choose *Latest release* or any
specific version, and optionally turn on **Update automatically**. Hearth checks every
`update_interval` minutes (default 6 h; Admin → Settings). When a newer *release* appears:

- with auto-update on, it announces the update in game, rebuilds the container on the new
  version, and restarts it if it was running;
- otherwise the server shows "update available" and you apply it from **Software**.

Hearth never "updates" a server onto an older release (e.g. if you pinned a newer beta).
Installing and upgrading pack files is done by the
[itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io/) image
(`TYPE=MODRINTH` / `TYPE=AUTO_CURSEFORGE`).

Hearth takes a backup before each automatic update and won't update if that backup fails.
Downgrades and loader changes can still corrupt worlds, so keep a backup before changing versions by hand.

## Users

The first account is the admin. Admins add nodes, create servers and assign an owner.

- **Owners** of a server get every server permission and can share it with others.
- **Shared users** see only the tabs their permissions allow.
- **Users with a quota** (Admin → Users) can create, resize and delete their own servers within it.
  Without a quota, resources, ports, deletion and custom images stay admin-only.
- **Suspended servers** can't be started by anyone but an admin.

### API and webhooks

Create a token under Account → API tokens and send it as `Authorization: Bearer hearth_…`. It acts
with your permissions. All endpoints are listed at `/api/docs`.

Webhooks (Admin → Settings) POST JSON for the events you pick. Each request carries
`X-Hearth-Event` and `X-Hearth-Signature: sha256=<hex>`, an HMAC of the body with the webhook's
secret. That's enough to hook up a billing system: create servers and users through the API, and
suspend or unsuspend them as payments land.

## Adding games

Templates live in `panel/hearth_panel/templates.py`. A template names the image, data path,
ports (with an optional offset for games that need consecutive ports), the console method
(`stdin` or an `exec` helper like `rcon-cli`) and the environment variables users can edit.
The form fields in the UI are generated from those variables.

## Configuration

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `HEARTH_AGENT_TOKEN` | panel + agent | — | Agent auth; with compose also registers node "local" |
| `HEARTH_PUBLIC_HOST` | panel | `localhost` | Address shown for the local node |
| `HEARTH_COOKIE_SECURE` | panel | `false` | Set `true` behind HTTPS |
| `HEARTH_SECRET_KEY` | panel | generated | Session signing key |
| `HEARTH_DATABASE_URL` | panel | SQLite in `/var/lib/hearth-panel` | Any SQLAlchemy URL |
| `CURSEFORGE_API_KEY` | panel | — | CurseForge access |
| `HEARTH_UPDATE_INTERVAL_MINUTES` | panel | `360` | Modpack update checks |
| `HEARTH_AGENT_IMAGE` | panel | `hearth-agent:latest` | Image in the node install command |
| `HEARTH_TRUSTED_PROXIES` | panel | `127.0.0.1` | IPs allowed to set X-Forwarded-For |
| `HEARTH_DATA_DIR` | compose `.env` | `/var/lib/hearth` | Folder on this computer for game server files (any drive) |
| `HEARTH_HOST_DATA_DIR` | agent | detected | Host path of the agent's data folder, if auto-detection fails |
| `HEARTH_TLS_CERT` / `HEARTH_TLS_KEY` | agent | — | Serve the agent over HTTPS |

## Development

```sh
pip install -r requirements-dev.txt
pytest                       # panel + agent tests; Docker and the internet are faked
python tools/demo.py         # full UI at http://127.0.0.1:8000 (admin / demo-password-123)
python tools/e2e.py          # with the demo running: clicks through every flow in Chromium
```

`tools/demo.py` runs the real panel and the real agent, with Docker simulated by
`tools/fake_docker.py` and a small **synthetic** modpack catalog, so the UI can be explored
without Docker or network access. Nothing in the demo is real data.

API docs are served at `/api/docs`.

## Upgrading

Replace the files and restart (`start.sh` / `start.bat`). Hearth adds new tables and columns to an
existing database on startup, so your servers, users and settings carry over.

## Not there yet

There's no SFTP, and no built-in payment provider (use the API and webhooks). Disk limits are
tracked and shown but Docker doesn't enforce them. Backups stay on the node that runs the server.
Startup upgrades only add tables and columns. Renaming or removing columns would need a
real migration tool such as Alembic.
