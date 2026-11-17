"""Is there a newer Hearth? Checks the project's GitHub releases (cached; quiet when unreachable)."""

from __future__ import annotations

import os
import re
from typing import Any

from .. import VERSION

CACHE_SECONDS = 6 * 3600


def parse(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3]) or (0,)


def update_repo() -> str:
    """owner/repo to check, from HEARTH_UPDATE_REPO; empty turns the check off."""
    return os.environ.get("HEARTH_UPDATE_REPO", "crwdevtools/Hearth-GSM").strip()


async def latest(ctx) -> dict[str, Any] | None:
    """The newest published release if it's newer than this panel, else None."""
    repo = update_repo()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", repo):
        return None

    async def load() -> dict[str, Any] | None:
        try:
            data = await ctx.http.get_json("GitHub", f"https://api.github.com/repos/{repo}/releases/latest",
                                           headers={"Accept": "application/vnd.github+json"})
        except Exception:  # private repo, offline, rate limited: just don't show a notice
            return None
        tag = str(data.get("tag_name") or "")
        if not tag or parse(tag) <= parse(VERSION):
            return None
        return {"version": tag.lstrip("v"), "url": data.get("html_url"), "name": data.get("name") or tag}

    return await ctx.cache.get(f"release:{repo}", CACHE_SECONDS, load)
