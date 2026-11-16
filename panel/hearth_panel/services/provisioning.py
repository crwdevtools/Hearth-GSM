"""Turn a Server row into the container spec the node agent runs, and assign ports."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Allocation, Node, Server
from ..templates import Template, TemplateError, defaults, get_template, parse_extra_env

# Template variables the panel consumes itself instead of passing to the container.
_PANEL_ONLY = {"IMAGE", "CONTAINER_PORT", "DATA_PATH", "EXTRA_ENV"}
_MODPACK_OVERRIDDEN = {"TYPE", "VERSION"}


class ProvisionError(ValueError):
    pass


def jvm_heap_mb(memory_mb: int) -> int:
    """Java heap for a container limited to `memory_mb`.

    The JVM needs a lot of memory outside the heap (metaspace, JIT code cache, thread stacks,
    direct buffers, world-gen and native libraries) and peaks while generating the first world.
    If heap + off-heap crosses the container limit, Docker OOM-kills the server mid-startup.
    Keep a quarter of the limit for that, at least 768 MB and at most 2 GB.
    """
    overhead = min(max(int(memory_mb * 0.25), 768), 2048)
    return max(512, memory_mb - overhead)


def minecraft_version(server: Server) -> str | None:
    if server.modpack_provider:
        return server.modpack_mc_version
    version = (server.variables or {}).get("VERSION")
    return str(version) if version else None


def _env_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return None  # unset: let the image keep its default / the file's current value
    return str(value)


def build_spec(server: Server, curseforge_key: str | None = None) -> dict[str, Any]:
    template = get_template(server.template)
    values: dict[str, Any] = {**defaults(template), **(server.variables or {})}
    primary = server.primary_allocation
    if primary is None:
        raise ProvisionError("Server has no primary port allocation.")

    env: dict[str, str] = dict(template.fixed_env)
    for var in template.variables:
        if var.key in _PANEL_ONLY:
            continue
        if server.modpack_provider and var.key in _MODPACK_OVERRIDDEN:
            continue
        rendered = _env_value(values.get(var.key))
        if rendered is not None:
            env[var.key] = rendered

    ports, port_by_name = _ports(server, template, primary, values)
    for env_key, port_name in template.env_from_port.items():
        env[env_key] = str(port_by_name[port_name])

    image = template.image
    data_mount = template.data_mount
    if "minecraft_java" in template.features:
        env["MEMORY"] = f"{jvm_heap_mb(server.memory_mb)}M"
        env.update(_modpack_env(server, curseforge_key))
        image = template.image_for({"_MC_VERSION": minecraft_version(server)}) if template.image_for else image
    if "custom_image" in template.features:
        image = str(values.get("IMAGE") or "")
        if not image:
            raise ProvisionError("Set a Docker image for this server.")
        data_mount = str(values.get("DATA_PATH") or "/data")
        try:
            env.update(parse_extra_env(str(values.get("EXTRA_ENV") or "")))
        except TemplateError as exc:
            raise ProvisionError(str(exc)) from exc

    return {
        "uuid": server.uuid,
        "image": image,
        "env": env,
        "ports": ports,
        "memory_mb": server.memory_mb,
        "swap_mb": server.swap_mb,
        "cpu_percent": server.cpu_percent,
        "data_mount": data_mount,
        "console": template.console,
        "console_exec": list(template.console_exec) or None,
        "stop_timeout": template.stop_timeout,
        "pre_backup": list(template.pre_backup),
        "post_backup": list(template.post_backup),
    }


def _ports(server: Server, template: Template, primary: Allocation, values: dict[str, Any]):
    ports: list[dict[str, Any]] = []
    port_by_name: dict[str, int] = {}
    owned = {(a.ip, a.port) for a in server.allocations}
    custom_container = int(values.get("CONTAINER_PORT") or 0) if "custom_image" in template.features else 0
    for tp in template.ports:
        host_port = primary.port + tp.offset
        if (primary.ip, host_port) not in owned:
            raise ProvisionError(f"Port {host_port} ({tp.name}) is not allocated to this server.")
        container_port = tp.container_port or custom_container or host_port
        port_by_name[tp.name] = host_port
        for proto in tp.protocols:
            ports.append({"host_ip": primary.ip, "host_port": host_port,
                          "container_port": container_port, "protocol": proto})
    for alloc in server.allocations:
        if alloc.is_primary or alloc.is_template:
            continue
        for proto in ("tcp", "udp"):
            ports.append({"host_ip": alloc.ip, "host_port": alloc.port,
                          "container_port": alloc.port, "protocol": proto})
    return ports, port_by_name


def _modpack_env(server: Server, curseforge_key: str | None) -> dict[str, str]:
    if server.modpack_provider == "modrinth":
        env = {"TYPE": "MODRINTH", "MODRINTH_MODPACK": server.modpack_project_id or ""}
        if server.modpack_version_id:
            env["MODRINTH_VERSION"] = server.modpack_version_id
        return env
    if server.modpack_provider == "curseforge":
        if not curseforge_key:
            raise ProvisionError("CurseForge modpacks need an API key. Add one in Admin → Settings.")
        env = {"TYPE": "AUTO_CURSEFORGE", "CF_API_KEY": curseforge_key, "CF_SLUG": server.modpack_slug or ""}
        if server.modpack_version_id:
            env["CF_FILE_ID"] = server.modpack_version_id
        return env
    return {}


# ----- allocations -------------------------------------------------------------------------


def choose_allocations(node: Node, template: Template, primary_id: int | None = None) -> list[tuple[Allocation, bool]]:
    """Pick a primary port (plus any template offset ports) from the node's free allocations.

    Returns [(allocation, is_primary)], primary first.
    """
    free: dict[str, dict[int, Allocation]] = defaultdict(dict)
    for a in node.allocations:
        if a.server_id is None:
            free[a.ip][a.port] = a
    offsets = [tp.offset for tp in template.ports if tp.offset]

    def fits(a: Allocation) -> list[Allocation] | None:
        extra = [free[a.ip].get(a.port + o) for o in offsets]
        return None if any(e is None for e in extra) else extra  # type: ignore[return-value]

    if primary_id is not None:
        chosen = next((a for pool in free.values() for a in pool.values() if a.id == primary_id), None)
        if chosen is None:
            raise ProvisionError("That port is not free on this node.")
        extra = fits(chosen)
        if extra is None:
            needs = ", ".join(str(chosen.port + o) for o in offsets)
            raise ProvisionError(f"{template.name} also needs port {needs} free on the same IP.")
        return [(chosen, True), *[(e, False) for e in extra]]

    # Prefer the game's conventional port and the ones just above it, then anything else.
    preferred = template.preferred_port
    candidates = sorted(
        (a for pool in free.values() for a in pool.values()),
        key=lambda a: (a.port < preferred, a.port if a.port >= preferred else -a.port, a.ip),
    )
    for alloc in candidates:
        extra = fits(alloc)
        if extra is not None:
            return [(alloc, True), *[(e, False) for e in extra]]
    hint = f" ({len(offsets) + 1} consecutive ports)" if offsets else ""
    raise ProvisionError(f"No free ports{hint} on node “{node.name}”. Add allocations in Admin → Nodes.")


def claim(db: Session, alloc: Allocation, server_id: int, *, is_primary: bool, is_template: bool) -> None:
    """Assign a port only if it is still free, so two concurrent requests can't share one."""
    result = db.execute(
        update(Allocation)
        .where(Allocation.id == alloc.id, Allocation.server_id.is_(None))
        .values(server_id=server_id, is_primary=is_primary, is_template=is_template)
    )
    if result.rowcount != 1:
        raise ProvisionError(f"Port {alloc.port} was just taken by another server. Try again.")


def check_capacity(db: Session, node: Node, memory_mb: int, disk_mb: int, exclude_server: int | None = None) -> None:
    query = select(func.coalesce(func.sum(Server.memory_mb), 0), func.coalesce(func.sum(Server.disk_mb), 0)).where(
        Server.node_id == node.id
    )
    if exclude_server is not None:
        query = query.where(Server.id != exclude_server)
    used_mem, used_disk = db.execute(query).one()
    if node.memory_mb and used_mem + memory_mb > node.memory_mb:
        raise ProvisionError(
            f"Node “{node.name}” has {node.memory_mb - used_mem} MB of memory left to assign; this needs {memory_mb} MB."
        )
    if node.disk_mb and disk_mb and used_disk + disk_mb > node.disk_mb:
        raise ProvisionError(
            f"Node “{node.name}” has {node.disk_mb - used_disk} MB of disk left to assign; this needs {disk_mb} MB."
        )
