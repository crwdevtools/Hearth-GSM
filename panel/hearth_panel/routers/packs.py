"""Modpack zips uploaded by hand (Modrinth .mrpack, CurseForge .zip), staged until a server is created."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..context import PanelContext
from ..deps import ctx, current_user
from ..models import User
from ..services import packzip

router = APIRouter(prefix="/api/modpacks/upload", tags=["modpacks"])


@router.put("")
async def upload_pack(request: Request, filename: str = Query(..., min_length=1, max_length=255),
                      user: User = Depends(current_user), context: PanelContext = Depends(ctx)):
    """Receive a modpack zip, read its manifest, and hold it for the create-server request."""
    if not filename.lower().endswith((".zip", ".mrpack")):
        raise HTTPException(400, "Choose a .mrpack (Modrinth) or .zip (CurseForge) modpack file.")
    declared = int(request.headers.get("content-length") or 0)
    if declared > packzip.MAX_PACK_BYTES:
        raise HTTPException(413, f"That file is over {packzip.MAX_PACK_BYTES // 1024**3} GB, which is too big for a modpack.")
    stage = await asyncio.to_thread(packzip.stage_dir, context.settings.data_dir)
    await asyncio.to_thread(packzip.sweep, stage, user.id)

    token = secrets.token_urlsafe(24)
    part = packzip.packed(stage, token).with_suffix(".part")
    size = 0
    try:
        with part.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > packzip.MAX_PACK_BYTES:
                    raise HTTPException(413, "That file is too big for a modpack.")
                await asyncio.to_thread(out.write, chunk)
        info = await asyncio.to_thread(packzip.inspect, part)
    except packzip.PackError as exc:
        part.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise HTTPException(507, "The panel couldn't store that upload (is its disk full?).") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    packzip.meta_of(stage, token).write_text(json.dumps({
        "user_id": user.id, "created": time.time(), "filename": filename[:255], "size": size, "info": info}))
    part.replace(packzip.packed(stage, token))
    return {"token": token, "filename": filename[:255], "size": size, **info}


@router.delete("/{token}", status_code=204)
async def discard_pack(token: str, user: User = Depends(current_user), context: PanelContext = Depends(ctx)) -> None:
    stage = packzip.stage_dir(context.settings.data_dir)
    try:
        packzip.load(stage, token, user.id)
    except packzip.PackError:
        return
    packzip.discard(stage, token)
