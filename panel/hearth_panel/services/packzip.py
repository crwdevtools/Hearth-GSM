"""Modpack zips downloaded by hand from Modrinth (.mrpack) or CurseForge (.zip).

The panel only reads the small manifest inside the archive: enough to tell which Minecraft version
and mod loader the pack needs, so the server gets the right Java and the right loader. The archive
itself is stored untouched, sent to the node, and unpacked by the Minecraft image inside the
container, never by the panel.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

MAX_PACK_BYTES = 2 * 1024**3  # a modpack zip is mostly mods and configs; 2 GiB is far beyond real ones
MAX_MANIFEST_BYTES = 8 * 1024**2
KEEP_SECONDS = 6 * 3600  # an upload nobody turned into a server is thrown away after this
MAX_STAGED_PER_USER = 3

TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,43}$")
_MC_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
_LOADER_KEYS = {"forge": "forge", "neoforge": "neoforge", "fabric-loader": "fabric", "quilt-loader": "quilt"}
_CF_LOADER_RE = re.compile(r"^(forge|neoforge|fabric|quilt)-(.+)$", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PackError(ValueError):
    """The upload isn't a usable modpack. The message is shown to the person."""


def _text(value: Any, limit: int = 255) -> str:
    return _CONTROL.sub("", str(value or "")).strip()[:limit]


def slugify(name: str) -> str:
    """CurseForge's page slug for a pack name, as best it can be guessed."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:128]


def _read_member(zf: zipfile.ZipFile, name: str) -> Any:
    info = zf.getinfo(name)
    if info.file_size > MAX_MANIFEST_BYTES:
        raise PackError(f"{name} is unreasonably large, so this doesn't look like a modpack.")
    with zf.open(info) as fh:
        raw = fh.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise PackError(f"{name} is unreasonably large, so this doesn't look like a modpack.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackError(f"{name} in this zip isn't valid JSON.") from exc
    if not isinstance(data, dict):
        raise PackError(f"{name} in this zip isn't a modpack manifest.")
    return data


def _check_mc(version: Any) -> str:
    version = _text(version, 32)
    if not _MC_RE.match(version):
        raise PackError(f"This pack targets Minecraft “{version or 'unknown'}”. Hearth runs release versions like 1.20.1.")
    return version


def inspect(path: Path) -> dict[str, Any]:
    """Describe a modpack zip: {format, name, version, mc_version, loader, loader_version, mods, ...}."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackError("That file isn't a valid zip archive.") from exc
    with zf:
        names = set(zf.namelist())
        try:
            if "modrinth.index.json" in names:
                return _modrinth(_read_member(zf, "modrinth.index.json"))
            if "manifest.json" in names:
                return _curseforge(_read_member(zf, "manifest.json"))
        except (NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
            raise PackError("Couldn't read that zip (encrypted or an unsupported compression).") from exc
        nested = next((n for n in names if n.count("/") == 1 and n.rsplit("/", 1)[1] in ("manifest.json", "modrinth.index.json")), None)
        if nested:
            raise PackError(f"The pack files are inside a folder (“{nested.split('/')[0]}”) in this zip. "
                            "Upload the zip exactly as downloaded, or zip the folder's contents instead.")
        if any(n.endswith(("run.sh", "start.sh", "startserver.sh", "start.bat")) for n in names):
            raise PackError("This looks like a ready-made server pack, not a modpack. Download the regular modpack "
                            "file instead (Modrinth's .mrpack, or CurseForge's main file rather than “Server files”).")
        raise PackError("No modpack manifest found. Modrinth packs are .mrpack files and CurseForge packs are "
                        "zips with a manifest.json at the top.")


def _modrinth(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("game", "minecraft") != "minecraft":
        raise PackError("That Modrinth pack isn't for Minecraft.")
    deps = data.get("dependencies")
    if not isinstance(deps, dict) or "minecraft" not in deps:
        raise PackError("This pack doesn't say which Minecraft version it needs.")
    loader = loader_version = None
    for key, name in _LOADER_KEYS.items():
        if deps.get(key):
            loader, loader_version = name, _text(deps[key], 64)
            break
    if loader is None:
        raise PackError("This pack doesn't use Forge, NeoForge, Fabric or Quilt, so Hearth can't set it up.")
    files = data.get("files")
    return {
        "format": "modrinth",
        "name": _text(data.get("name")) or "Modpack",
        "version": _text(data.get("versionId"), 64) or None,
        "summary": _text(data.get("summary"), 300) or None,
        "mc_version": _check_mc(deps["minecraft"]),
        "loader": loader,
        "loader_version": loader_version,
        "mods": len(files) if isinstance(files, list) else 0,
        "slug": None,
    }


def _curseforge(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("manifestType") != "minecraftModpack":
        raise PackError("manifest.json in this zip isn't a CurseForge modpack manifest.")
    mc = data.get("minecraft")
    if not isinstance(mc, dict) or not mc.get("version"):
        raise PackError("This pack doesn't say which Minecraft version it needs.")
    loaders = [m for m in (mc.get("modLoaders") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)]
    loaders.sort(key=lambda m: not m.get("primary"))
    parsed = next((m for m in (_CF_LOADER_RE.match(l["id"]) for l in loaders) if m), None)
    if parsed is None:
        raise PackError("This pack doesn't use Forge, NeoForge, Fabric or Quilt, so Hearth can't set it up.")
    name = _text(data.get("name")) or "Modpack"
    files = data.get("files")
    return {
        "format": "curseforge",
        "name": name,
        "version": _text(data.get("version"), 64) or None,
        "summary": None,
        "mc_version": _check_mc(mc["version"]),
        "loader": parsed.group(1).lower(),
        "loader_version": _text(parsed.group(2), 64),
        "mods": len(files) if isinstance(files, list) else 0,
        "slug": slugify(name) or None,
    }


def clean_slug(value: str | None) -> str | None:
    """A CurseForge page URL or slug typed by the person, reduced to the slug."""
    value = (value or "").strip()
    if not value:
        return None
    match = re.search(r"curseforge\.com/minecraft/modpacks/([A-Za-z0-9_\-]+)", value)
    slug = match.group(1) if match else value
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", slug):
        raise PackError("That isn't a CurseForge modpack page address or slug.")
    return slug


# ----- staging ------------------------------------------------------------------------------------


def stage_dir(data_dir: Path) -> Path:
    path = data_dir / "staging" / "modpacks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def packed(stage: Path, token: str) -> Path:
    if not TOKEN_RE.match(token or ""):
        raise PackError("Unknown upload. Upload the modpack again.")
    return stage / f"{token}.pack"


def meta_of(stage: Path, token: str) -> Path:
    return packed(stage, token).with_suffix(".json")


def load(stage: Path, token: str, user_id: int) -> tuple[Path, dict[str, Any]]:
    """The staged pack and its details, if this person uploaded it and it hasn't expired."""
    pack, meta = packed(stage, token), meta_of(stage, token)
    try:
        info = json.loads(meta.read_text())
    except (OSError, ValueError):
        raise PackError("That upload has expired. Upload the modpack again.") from None
    if info.get("user_id") != user_id or not pack.exists():
        raise PackError("That upload has expired. Upload the modpack again.")
    return pack, info


def discard(stage: Path, token: str) -> None:
    for path in (packed(stage, token), meta_of(stage, token), packed(stage, token).with_suffix(".part")):
        path.unlink(missing_ok=True)


def sweep(stage: Path, user_id: int | None = None) -> None:
    """Drop expired uploads, and keep one person to MAX_STAGED_PER_USER - 1 so a new one fits."""
    now = time.time()
    mine: list[tuple[float, str]] = []
    for meta in stage.glob("*.json"):
        token = meta.stem
        try:
            info = json.loads(meta.read_text())
        except (OSError, ValueError):
            info = {}
        created = float(info.get("created") or 0)
        if now - created > KEEP_SECONDS:
            discard(stage, token)
        elif user_id is not None and info.get("user_id") == user_id:
            mine.append((created, token))
    for path in stage.glob("*.part"):  # an upload that was cut off
        if now - path.stat().st_mtime > 3600:
            path.unlink(missing_ok=True)
    mine.sort()
    for _, token in mine[: max(0, len(mine) - (MAX_STAGED_PER_USER - 1))]:
        discard(stage, token)
