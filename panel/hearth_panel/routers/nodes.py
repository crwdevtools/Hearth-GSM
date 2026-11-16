"""Admin: nodes (machines running the agent) and their port allocations."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import admin_user, ctx, current_user, db_session
from ..models import Allocation, Node, User
from ..security import new_node_token
from ..serializers import allocation_out, node_out
from ..services.netguard import host_is_local
from ..services.node_client import NodeError

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

MAX_ALLOCATIONS_PER_REQUEST = 1000


def _clean_url(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+(:\d+)?(/\S*)?$", v):
        raise ValueError("Agent URL must look like https://host:8443")
    return v


def _clean_host(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", v):
        raise ValueError("Public address must be a hostname or IP")
    return v


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=255)
    public_host: str | None = Field(default=None, max_length=255)
    memory_mb: int | None = Field(default=None, ge=0)
    disk_mb: int | None = Field(default=None, ge=0)

    allow_insecure: bool = False  # not stored: confirms a plain-HTTP agent URL on a public address

    _v_url = field_validator("url")(classmethod(lambda cls, v: _clean_url(v)))
    _v_host = field_validator("public_host")(classmethod(lambda cls, v: _clean_host(v)))


def check_agent_url(url: str | None, allow_insecure: bool) -> None:
    """The agent's token controls Docker on that machine; don't send it unencrypted across the
    internet unless the admin says that's intended."""
    if not url or not url.startswith("http://") or allow_insecure:
        return
    host = urlsplit(url).hostname or ""
    if not host_is_local(host):
        raise HTTPException(400, f"{host} looks like an internet address, so the agent's token would be sent "
                                 "unencrypted. Use an https:// URL (see the guide), or tick "
                                 "“Allow plain HTTP” if this machine is only reachable on a private network.")


class NodeBody(NodePatch):
    name: str = Field(min_length=1, max_length=64)
    location: str = Field(default="", max_length=64)
    url: str = Field(max_length=255)
    public_host: str = Field(min_length=1, max_length=255)
    memory_mb: int = Field(default=0, ge=0)
    disk_mb: int = Field(default=0, ge=0)


class AllocationBody(BaseModel):
    ip: str = "0.0.0.0"
    ports: str = Field(description="Comma-separated ports and ranges, e.g. 25565-25575, 8123", max_length=500)
    note: str = Field(default="", max_length=64)

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        try:
            return str(ipaddress.ip_address(v.strip()))
        except ValueError as exc:
            raise ValueError("Enter an IP address like 0.0.0.0") from exc


def parse_ports(spec: str) -> list[int]:
    ports: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        m = re.fullmatch(r"(\d{1,5})(?:\s*-\s*(\d{1,5}))?", part)
        if not m:
            raise HTTPException(400, f"“{part}” is not a port or range.")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1024 <= start <= end <= 65535):
            raise HTTPException(400, "Ports must be between 1024 and 65535, low to high.")
        ports.update(range(start, end + 1))
        if len(ports) > MAX_ALLOCATIONS_PER_REQUEST:
            raise HTTPException(400, f"Add at most {MAX_ALLOCATIONS_PER_REQUEST} ports at a time.")
    if not ports:
        raise HTTPException(400, "Enter at least one port.")
    return sorted(ports)


AGENT_IMAGE = os.environ.get("HEARTH_AGENT_IMAGE", "hearth-agent:latest")


def install_command(node: Node) -> str:
    """The `docker run` line an admin pastes on the machine that becomes this node."""
    port = re.search(r":(\d+)", node.url.split("//", 1)[-1])
    host_port = port.group(1) if port else ("443" if node.url.startswith("https") else "80")
    tls = ""
    domain = urlsplit(node.url).hostname or "your-domain"
    if node.url.startswith("https://"):
        # The panel will speak HTTPS, so the agent must too: point these at the certificate
        # for the node's name (e.g. from certbot), or put a reverse proxy in front instead.
        tls = ("  -v /etc/letsencrypt:/etc/letsencrypt:ro \\\n"
               f"  -e HEARTH_TLS_CERT=/etc/letsencrypt/live/{domain}/fullchain.pem \\\n"
               f"  -e HEARTH_TLS_KEY=/etc/letsencrypt/live/{domain}/privkey.pem \\\n")
    return (
        "docker run -d --name hearth-agent --restart unless-stopped \\\n"
        f"  -p {host_port}:8080 \\\n"
        "  -v /var/run/docker.sock:/var/run/docker.sock \\\n"
        "  -v /var/lib/hearth:/var/lib/hearth \\\n"
        f"{tls}"
        f"  -e HEARTH_AGENT_TOKEN={node.token} \\\n"
        f"  {AGENT_IMAGE}"
    )


def _get(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found.")
    return node


async def _probe(context: PanelContext, node: Node) -> dict[str, Any]:
    try:
        return {"online": True, "system": await context.nodes.system(node), "error": None}
    except NodeError as exc:
        return {"online": False, "system": None, "error": str(exc)}


@router.get("/choices")
async def node_choices(user: User = Depends(current_user), db: Session = Depends(db_session),
                       context: PanelContext = Depends(ctx)):
    """Where a user may create servers: names, free capacity and whether it's up (no URLs or tokens)."""
    if not user.is_admin and user.max_servers <= 0:
        raise HTTPException(403, "Your account doesn't have a server quota.")
    nodes = db.scalars(select(Node).order_by(Node.name)).all()

    async def up(node: Node) -> bool:
        try:
            await context.nodes.health(node)
            return True
        except NodeError:
            return False

    online = await asyncio.gather(*(up(n) for n in nodes))
    return [{"id": n.id, "name": n.name, "location": n.location, "public_host": n.public_host,
             "allocations_free": sum(1 for a in n.allocations if a.server_id is None),
             "memory_mb": n.memory_mb, "memory_assigned_mb": sum(x.memory_mb for x in n.servers), "online": ok}
            for n, ok in zip(nodes, online)]


@router.get("")
async def list_nodes(_: User = Depends(admin_user), db: Session = Depends(db_session),
                     context: PanelContext = Depends(ctx)):
    nodes = db.scalars(select(Node).order_by(Node.name)).all()
    probes = await asyncio.gather(*(_probe(context, n) for n in nodes))
    return [{**node_out(n), **p} for n, p in zip(nodes, probes)]


@router.post("", status_code=201)
def create_node(body: NodeBody, _: User = Depends(admin_user), db: Session = Depends(db_session)):
    check_agent_url(body.url, body.allow_insecure)
    node = Node(**body.model_dump(exclude={"allow_insecure"}), token=new_node_token())
    db.add(node)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A node with that name already exists.")
    return {**node_out(node, with_token=True), "install_command": install_command(node)}


@router.get("/{node_id}")
async def get_node(node_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session),
                   context: PanelContext = Depends(ctx)):
    node = _get(db, node_id)
    return {
        **node_out(node, with_token=True),
        **(await _probe(context, node)),
        "install_command": install_command(node),
        "allocations": [allocation_out(a) | {"server_name": a.server.name if a.server else None}
                        for a in node.allocations],
    }


@router.patch("/{node_id}")
def update_node(node_id: int, body: NodePatch, _: User = Depends(admin_user), db: Session = Depends(db_session)):
    node = _get(db, node_id)
    if body.url is not None and body.url != node.url:
        check_agent_url(body.url, body.allow_insecure)
    for key, value in body.model_dump(exclude_none=True, exclude={"allow_insecure"}).items():
        setattr(node, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A node with that name already exists.")
    return node_out(node)


@router.post("/{node_id}/token")
def rotate_token(node_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session)):
    node = _get(db, node_id)
    node.token = new_node_token()
    db.commit()
    return {"token": node.token, "install_command": install_command(node)}


@router.delete("/{node_id}", status_code=204)
def delete_node(node_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session)) -> None:
    node = _get(db, node_id)
    if node.servers:
        raise HTTPException(409, f"Move or delete the {len(node.servers)} server(s) on this node first.")
    db.delete(node)
    db.commit()


@router.post("/{node_id}/allocations", status_code=201)
def add_allocations(node_id: int, body: AllocationBody, _: User = Depends(admin_user),
                    db: Session = Depends(db_session)):
    node = _get(db, node_id)
    existing = {(a.ip, a.port) for a in node.allocations}
    ports = parse_ports(body.ports)
    added = 0
    for port in ports:
        if (body.ip, port) in existing:
            continue
        db.add(Allocation(node_id=node.id, ip=body.ip, port=port, note=body.note))
        added += 1
    db.commit()
    return {"added": added, "skipped": len(ports) - added}


@router.delete("/{node_id}/allocations/{allocation_id}", status_code=204)
def delete_allocation(node_id: int, allocation_id: int, _: User = Depends(admin_user),
                      db: Session = Depends(db_session)) -> None:
    alloc = db.get(Allocation, allocation_id)
    if alloc is None or alloc.node_id != node_id:
        raise HTTPException(404, "Allocation not found.")
    if alloc.server_id is not None:
        raise HTTPException(409, "That port is assigned to a server. Remove it from the server first.")
    db.delete(alloc)
    db.commit()


class TunnelBody(BaseModel):
    secret: str = Field(min_length=8, max_length=255)


@router.get("/{node_id}/tunnel")
async def tunnel_status(node_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session),
                        context: PanelContext = Depends(ctx)):
    node = _get(db, node_id)
    return {"saved": bool(node.playit_secret), **(await context.nodes.tunnel_status(node))}


@router.put("/{node_id}/tunnel")
async def tunnel_set(node_id: int, body: TunnelBody, _: User = Depends(admin_user), db: Session = Depends(db_session),
                     context: PanelContext = Depends(ctx)):
    node = _get(db, node_id)
    await context.nodes.tunnel_set(node, body.secret.strip())
    node.playit_secret = body.secret.strip()
    db.commit()
    return {"saved": True}


@router.delete("/{node_id}/tunnel", status_code=204)
async def tunnel_clear(node_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session),
                       context: PanelContext = Depends(ctx)) -> None:
    node = _get(db, node_id)
    await context.nodes.tunnel_clear(node)
    node.playit_secret = None
    db.commit()
