# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack
Python 3.12 + FastAPI + SQLAlchemy (panel and node agent), Docker Engine via docker-py on every node. Frontend: no build step — plain HTML/CSS/ES modules served by the panel.

## Users
An admin running game servers for themselves and a small group of friends (homelab scale). The admin adds machines (their own box, a rented VPS, a provider's dedicated server) and creates servers; invited friends sign in to start/stop, use the console, and tweak the servers they own.

## Product Purpose
Hearth is a self-hosted control panel for running many game servers at once across many machines. It takes the Pterodactyl model (panel + per-machine daemon, port allocations, templates) and adds first-class Minecraft modpack handling: browse CurseForge and Modrinth, pick any pack version or any server version, and let packs auto-update. Success: going from "I want to play pack X" to a running server with the right RAM and properties in one form.

## Positioning
One panel, many hosts: local and remote machines are the same kind of node (a small agent container), so a server on a home box and one on a cloud VPS are managed identically. Modpack versions are tracked continuously, not looked up once.

## Operating Context
Used from a desktop browser while setting things up, and from a phone/laptop mid-game to restart or run a console command. Sessions are short and task-driven: check status, open console, change RAM, bump version.

## Capabilities and Constraints
- Panel ↔ node agent over HTTP(S) + WebSocket with a per-node bearer token.
- Port allocations are per node; each server owns a primary allocation and optional extras.
- Templates (Minecraft Java, Minecraft Bedrock, Valheim, custom Docker image) define images, ports, variables.
- Minecraft Java uses the itzg/minecraft-server image; server types Vanilla, Paper, Purpur, Fabric, Quilt, Forge, NeoForge, Modrinth pack, CurseForge pack.
- CurseForge requires an API key supplied by the admin; Modrinth needs none.
- Undecided: backups, schedules, sub-user permissions, billing (not planned).

## Brand Commitments
- Name: Hearth.
- Visual reference pinned by the owner: look of skills.sh (black ground, Geist type, hairline tables, ASCII block wordmark, leaderboard rows) arranged in Pterodactyl's panel layout (top bar, server list, per-server sub-navigation, admin sidebar).

## Evidence on Hand
None. No real users, screenshots, or metrics; nothing may be fabricated as such.

## Product Principles
1. A server's state is always visible and always truthful — never show "running" from a stale guess.
2. Every change is reversible from the same screen it was made on.
3. Remote and local hosts are equals.
4. Defaults that just work (RAM, ports, Java version) with full control one click away.
