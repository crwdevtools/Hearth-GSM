"""One place where things that happen are recorded and announced.

emit() writes the activity log, then (in the background) posts to the server's Discord webhook
and to every outgoing webhook subscribed to the event. Delivery failures never break the action
that caused the event.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Server, User, Webhook
from . import netguard

log = logging.getLogger("hearth.events")

# event -> (Discord colour, emoji-free title template). Only these reach Discord and webhooks;
# everything else is activity-log only.
ANNOUNCED: dict[str, tuple[int, str]] = {
    "server.started": (0x45C27A, "{server} is online"),
    "server.stopped": (0x6E6E6E, "{server} stopped"),
    "server.crashed": (0xF2585C, "{server} crashed"),
    "server.out_of_memory": (0xF2585C, "{server} ran out of memory"),
    "server.sleeping": (0x52A8FF, "{server} is sleeping"),
    "server.woke": (0x52A8FF, "{server} is waking up"),
    "server.created": (0x45C27A, "{server} was created"),
    "server.deleted": (0x6E6E6E, "{server} was deleted"),
    "server.suspended": (0xE9A23B, "{server} was suspended"),
    "server.unsuspended": (0x45C27A, "{server} was unsuspended"),
    "modpack.updated": (0x52A8FF, "{server} updated its modpack"),
    "modpack.update_available": (0x52A8FF, "A modpack update is available for {server}"),
    "backup.completed": (0x45C27A, "Backup of {server} finished"),
    "backup.failed": (0xF2585C, "Backup of {server} failed"),
    "backup.restored": (0xE9A23B, "{server} was restored from a backup"),
    "schedule.failed": (0xF2585C, "A scheduled task on {server} failed"),
}
EVENT_NAMES = sorted(ANNOUNCED)

_background: set[asyncio.Task] = set()


def emit(ctx, db: Session, action: str, *, server: Server | None = None, user: User | None = None,
         detail: str = "", data: dict[str, Any] | None = None, commit: bool = True) -> None:
    db.add(AuditLog(action=action, detail=detail[:2000], server_id=server.id if server else None,
                    user_id=user.id if user else None))
    if commit:
        db.commit()
    if action not in ANNOUNCED:
        return
    payload = {
        "event": action,
        "at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
        "server": {"id": server.id, "uuid": server.uuid, "name": server.name} if server else None,
        "user": {"id": user.id, "username": user.username} if user else None,
        "data": data or {},
    }
    targets = [(w.url, w.secret) for w in db.scalars(select(Webhook).where(Webhook.enabled.is_(True)))
               if not w.events or action in w.events]
    discord = server.discord_webhook if server is not None else None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a worker thread (a plain `def` route): the activity log has it, but nothing
        # can be sent from here. Routes that emit announced events must be `async def`.
        if targets or discord:
            log.warning("%s not delivered: emitted outside the event loop", action)
        return
    task = loop.create_task(_deliver(ctx, payload, targets, discord))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _deliver(ctx, payload: dict[str, Any], targets: list[tuple[str, str]], discord: str | None) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    for url, secret in targets:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            if not ctx.settings.allow_private_webhooks:
                # A webhook must not be a way to reach the panel's own network (the node agents,
                # the cloud metadata service): check where the name points right before sending.
                reason = await netguard.public_destination(url, ctx.resolver)
                if reason:
                    log.warning("webhook %s not sent: %s (set HEARTH_ALLOW_PRIVATE_WEBHOOKS=true to allow)",
                                url, reason)
                    continue
            await ctx.http.client.post(url, content=body, timeout=8, follow_redirects=False, headers={
                "Content-Type": "application/json", "X-Hearth-Event": payload["event"],
                "X-Hearth-Signature": f"sha256={signature}"})
        except Exception as exc:
            log.warning("webhook %s failed: %s", url, exc)
    if discord:
        colour, title = ANNOUNCED[payload["event"]]
        name = (payload["server"] or {}).get("name", "Hearth")
        embed = {"title": title.format(server=name), "color": colour, "timestamp": payload["at"]}
        if payload["detail"]:
            embed["description"] = payload["detail"][:1500]
        try:
            await ctx.http.client.post(discord, json={"username": "Hearth", "embeds": [embed]}, timeout=8,
                                       follow_redirects=False)
        except Exception as exc:
            log.warning("discord webhook failed: %s", exc)


def is_discord_webhook(url: str) -> bool:
    return url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                           "https://ptb.discord.com/api/webhooks/", "https://canary.discord.com/api/webhooks/"))
