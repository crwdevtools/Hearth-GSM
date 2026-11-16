"""Minecraft player management: who's online, whitelist, operators, bans."""

from __future__ import annotations

import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, server_with
from ..models import Server, User
from ..services import events
from ..services.node_client import NodeError
from ..services.status import parse_player_list

router = APIRouter(prefix="/api/servers/{server_id}/players", tags=["players"])
access = server_with("players")
NAME = r"^[A-Za-z0-9_]{1,16}$"

# action -> (console command, activity label)
ACTIONS = {
    "whitelist_add": ("whitelist add {name}", "added {name} to the whitelist"),
    "whitelist_remove": ("whitelist remove {name}", "removed {name} from the whitelist"),
    "op": ("op {name}", "made {name} an operator"),
    "deop": ("deop {name}", "removed operator from {name}"),
    "kick": ("kick {name} {reason}", "kicked {name}"),
    "ban": ("ban {name} {reason}", "banned {name}"),
    "pardon": ("pardon {name}", "unbanned {name}"),
}


class PlayerAction(BaseModel):
    action: Literal["whitelist_add", "whitelist_remove", "op", "deop", "kick", "ban", "pardon"]
    name: str = Field(pattern=NAME)
    reason: str = Field(default="", max_length=120)


def _require_minecraft(server: Server) -> None:
    if server.template != "minecraft-java":
        raise HTTPException(400, "Player management is available for Minecraft: Java servers.")


async def _json_list(context, server: Server, filename: str) -> list[dict]:
    try:
        raw = await context.nodes.read_file(server.node, server.uuid, f"/{filename}")
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except (NodeError, ValueError):
        return []


@router.get("")
async def players(server: Server = Depends(access), context: PanelContext = Depends(ctx)):
    _require_minecraft(server)
    online: list[str] | None = None
    max_players = None
    try:
        parsed = parse_player_list(await context.nodes.command(server.node, server.uuid, "list"))
        if parsed:
            _, max_players, online = parsed
    except NodeError:
        online = None  # server offline: lists below still come from the files
    whitelist = await _json_list(context, server, "whitelist.json")
    ops = await _json_list(context, server, "ops.json")
    banned = await _json_list(context, server, "banned-players.json")
    return {
        "running": online is not None,
        "online": online or [],
        "max": max_players,
        "whitelist": sorted(p.get("name", "") for p in whitelist if p.get("name")),
        "ops": sorted(p.get("name", "") for p in ops if p.get("name")),
        "banned": [{"name": p.get("name"), "reason": p.get("reason", ""), "created": p.get("created")}
                   for p in banned if p.get("name")],
        "whitelist_enabled": bool((server.variables or {}).get("ENABLE_WHITELIST")),
    }


@router.post("")
async def act(body: PlayerAction, server: Server = Depends(access), user: User = Depends(current_user),
              db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    _require_minecraft(server)
    template, label = ACTIONS[body.action]
    reason = re.sub(r"[\r\n]", " ", body.reason).strip()
    command = template.format(name=body.name, reason=reason).strip()
    try:
        output = await context.nodes.command(server.node, server.uuid, command)
    except NodeError as exc:
        raise HTTPException(409, f"The server must be running to do that ({exc}).") from exc
    events.emit(context, db, "players.changed", server=server, user=user, detail=label.format(name=body.name))
    return {"output": (output or "").strip()}
