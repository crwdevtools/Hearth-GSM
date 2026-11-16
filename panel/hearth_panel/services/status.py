"""Live server status from the nodes, and console helpers built on it."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any

from ..models import Node, Server
from .node_client import NodeError

_PLAYERS_RE = re.compile(r"There are (\d+)\s*(?:of a max of|/)\s*(\d+) players online:?\s*(.*)", re.S)


async def live_statuses(ctx, servers: list[Server]) -> dict[str, dict[str, Any]]:
    by_node: dict[int, list[Server]] = defaultdict(list)
    nodes: dict[int, Node] = {}
    for s in servers:
        by_node[s.node_id].append(s)
        nodes[s.node_id] = s.node

    async def one(node_id: int) -> dict[str, dict[str, Any]]:
        node = nodes[node_id]
        try:
            reported = {st["uuid"]: st for st in await ctx.nodes.statuses(node)}
        except NodeError as exc:
            return {s.uuid: {"state": "node_offline", "error": str(exc)} for s in by_node[node_id]}
        return {s.uuid: reported.get(s.uuid, {"state": "missing"}) for s in by_node[node_id]}

    merged: dict[str, dict[str, Any]] = {}
    for part in await asyncio.gather(*(one(n) for n in by_node)):
        merged.update(part)
    return merged


def parse_player_list(output: str | None) -> tuple[int, int, list[str]] | None:
    """Parse Minecraft's `list` reply: 'There are 2 of a max of 20 players online: alex, sam'."""
    if not output:
        return None
    clean = re.sub(r"§.|\x1b\[[0-9;]*m", "", output)
    m = _PLAYERS_RE.search(clean)
    if not m:
        return None
    names = [n.strip() for n in m.group(3).strip().splitlines()[0].split(",") if n.strip()] if m.group(3).strip() else []
    return int(m.group(1)), int(m.group(2)), names
