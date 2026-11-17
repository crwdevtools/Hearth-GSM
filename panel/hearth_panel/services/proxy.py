"""Linking Paper servers behind a Velocity proxy, with Velocity's modern forwarding.

The proxy's velocity.toml gets a [servers] entry per backend and a `try` order; each backend gets
online-mode off (the proxy authenticates players) and Paper's velocity forwarding turned on with
the proxy's secret, so a player who connects to a backend directly is refused.
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy import select

from ..models import Server
from .node_client import NodeError

BACKEND_SOFTWARE = ("PAPER", "PURPUR")
DOCKER_HOST = "172.17.0.1"  # the Docker host as seen from a container on the default bridge


class ProxyError(ValueError):
    pass


def eligible(server: Server) -> bool:
    return (server.template == "minecraft-java" and not server.modpack_provider
            and str((server.variables or {}).get("TYPE", "")).upper() in BACKEND_SOFTWARE)


def backend_address(proxy: Server, backend: Server) -> str:
    port = backend.primary_allocation.port
    if backend.node_id == proxy.node_id:
        return f"{DOCKER_HOST}:{port}"
    return f"{backend.node.public_host or backend.node.name}:{port}"


def _key(name: str) -> str:
    key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "server"
    return key[:32]


def render_servers(proxy: Server, backends: list[Server]) -> str:
    names, lines = [], ["[servers]"]
    for b in backends:
        key = _key(b.name)
        while key in names:
            key = f"{key}-{b.id}"
        names.append(key)
        lines.append(f'{key} = "{backend_address(proxy, b)}"')
    lines.append("try = [" + ", ".join(f'"{n}"' for n in names) + "]")
    return "\n".join(lines) + "\n"


def rewrite_velocity_toml(text: str, servers_block: str) -> str:
    """Replace the [servers] table and switch forwarding to modern. Everything else is kept."""
    out, skipping, replaced = [], False, False
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if header:
            if header.group(1).strip() == "servers":
                out.append(servers_block + "\n")
                skipping, replaced = True, True
                continue
            skipping = False
        if skipping:
            continue
        out.append(line)
    text = "".join(out)
    if not replaced:
        text = text.rstrip() + "\n\n" + servers_block
    for key, value in (("player-info-forwarding-mode", '"modern"'), ("forwarding-secret-file", '"forwarding.secret"')):
        if re.search(rf"(?m)^{key}\s*=", text):
            text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
        else:
            text = f"{key} = {value}\n" + text
    return text


def set_paper_velocity(text: str, enabled: bool, secret: str) -> str:
    """Set proxies.velocity in Paper's config/paper-global.yml (a minimal file is fine: Paper fills
    in the rest on start)."""
    wanted = {"enabled": "true" if enabled else "false", "online-mode": "true",
              "secret": "'" + secret.replace("'", "''") + "'"}
    lines = text.splitlines()
    try:
        p = next(i for i, l in enumerate(lines) if re.match(r"^proxies:\s*$", l))
    except StopIteration:
        block = ["proxies:", "  velocity:"] + [f"    {k}: {v}" for k, v in wanted.items()]
        return ("\n".join(lines).rstrip() + "\n" if lines else "") + "\n".join(block) + "\n"
    end = next((i for i in range(p + 1, len(lines)) if lines[i] and not lines[i].startswith(" ")), len(lines))
    v = next((i for i in range(p + 1, end) if re.match(r"^  velocity:\s*$", lines[i])), None)
    if v is None:
        lines[end:end] = ["  velocity:"] + [f"    {k}: {val}" for k, val in wanted.items()]
        return "\n".join(lines) + "\n"
    v_end = next((i for i in range(v + 1, end) if not lines[i].startswith("    ")), end)
    seen = set()
    for i in range(v + 1, v_end):
        m = re.match(r"^    ([a-z-]+):", lines[i])
        if m and m.group(1) in wanted:
            lines[i] = f"    {m.group(1)}: {wanted[m.group(1)]}"
            seen.add(m.group(1))
    lines[v_end:v_end] = [f"    {k}: {val}" for k, val in wanted.items() if k not in seen]
    return "\n".join(lines) + "\n"


async def _read(ctx, server: Server, path: str) -> str | None:
    try:
        return await ctx.nodes.read_file(server.node, server.uuid, path)
    except NodeError as exc:
        if exc.status == 404:
            return None
        raise


async def link(ctx, db, proxy: Server, backends: list[Server]) -> list[Server]:
    """Make `backends` (in try order) the proxy's servers. Returns the servers whose config
    changed and need a restart."""
    from . import updater

    toml = await _read(ctx, proxy, "/velocity.toml")
    if toml is None:
        raise ProxyError("Start the proxy once first, so Velocity writes velocity.toml.")
    secret = (await _read(ctx, proxy, "/forwarding.secret") or "").strip()
    if not secret:
        secret = secrets.token_urlsafe(24)
        await ctx.nodes.write_file(proxy.node, proxy.uuid, "/forwarding.secret", secret)
    await ctx.nodes.write_file(proxy.node, proxy.uuid, "/velocity.toml",
                               rewrite_velocity_toml(toml, render_servers(proxy, backends)))

    changed = [proxy]
    previous = list(db.scalars(select(Server).where(Server.proxy_id == proxy.id)))
    for order, backend in enumerate(backends):
        backend.proxy_id, backend.proxy_order = proxy.id, order
        await _configure_backend(ctx, backend, True, secret)
        changed.append(backend)
    for old in previous:
        if old not in backends:
            old.proxy_id, old.proxy_order = None, 0
            await _configure_backend(ctx, old, False, "")
            changed.append(old)
    db.commit()
    for server in changed[1:]:
        await updater.push(ctx, db, server)  # online-mode changed: recreate (keeps it running)
    return changed


async def _configure_backend(ctx, backend: Server, linked: bool, secret: str) -> None:
    variables = dict(backend.variables or {})
    variables["ONLINE_MODE"] = not linked
    backend.variables = variables
    current = await _read(ctx, backend, "/config/paper-global.yml") or ""
    if current or linked:
        await ctx.nodes.write_file(backend.node, backend.uuid, "/config/paper-global.yml",
                                   set_paper_velocity(current, linked, secret if linked else ""))
