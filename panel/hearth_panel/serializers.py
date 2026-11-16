"""Model → JSON shapes returned by the API."""

from __future__ import annotations

from typing import Any

from .models import Allocation, Node, Server, User
from .templates import get_template


def user_out(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "totp_enabled": user.totp_enabled,
        "max_servers": user.max_servers,
        "max_memory_mb": user.max_memory_mb,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def allocation_out(a: Allocation) -> dict[str, Any]:
    return {
        "id": a.id,
        "node_id": a.node_id,
        "ip": a.ip,
        "port": a.port,
        "server_id": a.server_id,
        "is_primary": a.is_primary,
        "is_template": a.is_template,
        "note": a.note,
    }


def node_out(node: Node, *, with_token: bool = False) -> dict[str, Any]:
    allocations = node.allocations
    data = {
        "id": node.id,
        "name": node.name,
        "location": node.location,
        "url": node.url,
        "public_host": node.public_host,
        "memory_mb": node.memory_mb,
        "disk_mb": node.disk_mb,
        "allocations_total": len(allocations),
        "allocations_free": sum(1 for a in allocations if a.server_id is None),
        "servers": len(node.servers),
        "memory_assigned_mb": sum(s.memory_mb for s in node.servers),
    }
    if with_token:
        data["token"] = node.token
    return data


def node_brief(node: Node) -> dict[str, Any]:
    return {"id": node.id, "name": node.name, "location": node.location, "public_host": node.public_host}


def server_out(server: Server, status: dict[str, Any] | None = None) -> dict[str, Any]:
    template = get_template(server.template)
    primary = server.primary_allocation
    secret_keys = {v.key for v in template.variables if v.secret}
    variables = {k: ("" if k in secret_keys and v else v) for k, v in (server.variables or {}).items()}
    return {
        "id": server.id,
        "uuid": server.uuid,
        "name": server.name,
        "description": server.description,
        "owner": {"id": server.owner.id, "username": server.owner.username},
        "node": node_brief(server.node),
        "template": {"key": template.key, "name": template.name, "game": template.game},
        "address": server.public_address or (f"{server.node.public_host}:{primary.port}" if primary else None),
        "direct_address": f"{server.node.public_host}:{primary.port}" if primary else None,
        "public_address": server.public_address,
        "discord_webhook_set": bool(server.discord_webhook),
        "auto_stop_minutes": server.auto_stop_minutes,
        "wake_on_join": server.wake_on_join,
        "suspended": server.suspended,
        "suspended_reason": server.suspended_reason,
        "memory_mb": server.memory_mb,
        "swap_mb": server.swap_mb,
        "cpu_percent": server.cpu_percent,
        "disk_mb": server.disk_mb,
        "variables": variables,
        "secrets_set": sorted(k for k in secret_keys if (server.variables or {}).get(k)),
        "modpack": (
            {
                "provider": server.modpack_provider,
                "project_id": server.modpack_project_id,
                "slug": server.modpack_slug,
                "name": server.modpack_name,
                "icon_url": server.modpack_icon,
                "version_id": server.modpack_version_id,
                "version_name": server.modpack_version_name,
                "mc_version": server.modpack_mc_version,
                "auto_update": server.auto_update,
                "update": (
                    {"id": server.update_version_id, "name": server.update_version_name}
                    if server.update_version_id else None
                ),
                "last_check": server.last_update_check.isoformat() if server.last_update_check else None,
            }
            if server.modpack_provider else None
        ),
        "allocations": [allocation_out(a) for a in server.allocations],
        "status": status or {"state": "unknown"},
        "created_at": server.created_at.isoformat() if server.created_at else None,
    }
