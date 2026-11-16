"""Sandboxed file access inside one server's data directory."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 2 * 1024 * 1024


class FileAccessError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def resolve(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes it (incl. via symlinks)."""
    root = root.resolve()
    candidate = (root / relative.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise FileAccessError("Path is outside the server directory.", 403)
    return candidate


def resolve_entry(root: Path, relative: str) -> Path:
    """Like resolve(), but does not follow a symlink in the final component, so deleting or
    renaming a link acts on the link itself rather than on what it points to."""
    parts = [p for p in relative.split("/") if p]
    if not parts or parts[-1] in (".", ".."):
        raise FileAccessError("Refusing to act on the server root.", 403)
    parent = resolve(root, "/".join(parts[:-1]))
    return parent / parts[-1]


def list_dir(root: Path, relative: str) -> list[dict[str, Any]]:
    path = resolve(root, relative)
    if not path.is_dir():
        raise FileAccessError("Directory not found.", 404)
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append(
                {
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "is_symlink": entry.is_symlink(),
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                }
            )
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def read_text(root: Path, relative: str) -> str:
    path = resolve(root, relative)
    if not path.is_file():
        raise FileAccessError("File not found.", 404)
    if path.stat().st_size > MAX_READ_BYTES:
        raise FileAccessError("File is larger than 2 MB; download it over SFTP instead.", 413)
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise FileAccessError("This looks like a binary file and can't be edited here.", 415)
    return data.decode("utf-8", "replace")


def write_text(root: Path, relative: str, content: str) -> None:
    path = resolve(root, relative)
    if path == root.resolve() or path.is_dir():
        raise FileAccessError("Cannot overwrite a directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.hearth-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def make_dir(root: Path, relative: str) -> None:
    resolve(root, relative).mkdir(parents=True, exist_ok=True)


def delete(root: Path, relative: str) -> None:
    path = resolve_entry(root, relative)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise FileAccessError("Not found.", 404)


def rename(root: Path, source: str, target: str) -> None:
    src, dst = resolve_entry(root, source), resolve_entry(root, target)
    if not src.exists() and not src.is_symlink():
        raise FileAccessError("Not found.", 404)
    if dst.exists():
        raise FileAccessError("A file with that name already exists.", 409)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


# ----- uploads, archives, downloads -----------------------------------------------------------

import hashlib  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
import zipfile  # noqa: E402

MAX_FETCH_BYTES = 1024 * 1024 * 1024
FETCH_HOSTS = {"cdn.modrinth.com", "edge.forgecdn.net", "mediafilez.forgecdn.net"}


def open_upload(root: Path, relative: str) -> tuple[Path, Path]:
    """Validate an upload target; returns (final path, temp path to stream into)."""
    path = resolve_entry(root, relative)
    if path.is_dir():
        raise FileAccessError("A folder with that name already exists.", 409)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.with_name(f".{path.name}.hearth-tmp")


def archive_folder(root: Path, relative: str, out: Path) -> Path:
    """Zip a folder (e.g. the world) for download."""
    source = resolve(root, relative)
    if not source.is_dir():
        raise FileAccessError("Folder not found.", 404)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink():
                zf.write(path, Path(source.name) / path.relative_to(source))
    return out


def _safe_zip_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if target != dest and dest not in target.parents:
            raise FileAccessError(f"Archive entry escapes the folder: {info.filename}", 400)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:  # symlink
            raise FileAccessError("Archives with symbolic links aren't accepted.", 400)
    zf.extractall(dest)


DIMENSION_SUFFIXES = ("_nether", "_the_end")  # Paper/Spigot keep these next to the overworld


def _find_world(folder: Path) -> Path | None:
    """The shallowest folder containing level.dat (worlds are often zipped inside a folder).
    A Paper world zipped with its world_nether/world_the_end siblings has three level.dat files
    at the same depth; the overworld is the one that isn't a dimension folder."""
    hits = sorted((p.parent for p in folder.rglob("level.dat")),
                  key=lambda d: (len(d.parts), d.name.endswith(DIMENSION_SUFFIXES), d.name))
    return hits[0] if hits else None


def _check_size(entries: list[tuple[str, int]], dest: Path) -> None:
    total = sum(size for _, size in entries)
    free = shutil.disk_usage(dest).free
    if total > free - 256 * 1024 * 1024:
        raise FileAccessError(f"Unpacked, this archive needs {total // 1048576} MB; the node has "
                              f"{free // 1048576} MB free.", 507)


def _move_into(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        dest = target / item.name
        if dest.exists() or dest.is_symlink():
            shutil.rmtree(dest) if dest.is_dir() and not dest.is_symlink() else dest.unlink()
        shutil.move(str(item), dest)


def extract_archive(root: Path, archive_rel: str, target_rel: str, *, replace: bool, world: bool) -> str:
    archive = resolve(root, archive_rel)
    if not archive.is_file():
        raise FileAccessError("Archive not found.", 404)
    target = resolve(root, target_rel)
    if target == root.resolve() and replace:
        raise FileAccessError("Refusing to replace the whole server folder.", 403)
    with tempfile.TemporaryDirectory(dir=root, prefix=".hearth-extract-") as tmp:
        staging = Path(tmp)
        name = archive.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                _check_size([(i.filename, i.file_size) for i in zf.infolist()], root)  # zip bombs
                _safe_zip_extract(zf, staging)
        elif name.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(archive) as tar:
                _check_size([(m.name, m.size) for m in tar.getmembers()], root)
                tar.extractall(staging, filter="data")
        else:
            raise FileAccessError("Upload a .zip, .tar.gz or .tgz file.", 415)
        if not world:
            if replace and target.exists():
                shutil.rmtree(target)
            _move_into(staging, target)
        else:
            source = _find_world(staging)
            if source is None:
                raise FileAccessError("No Minecraft world in that archive (couldn't find level.dat).", 400)
            # Replace the overworld and its Paper dimension folders together; leftovers from the
            # old world would otherwise reappear as its nether and end.
            pairs = [(source, target)] + [
                (source.with_name(source.name + suffix), target.with_name(target.name + suffix))
                for suffix in DIMENSION_SUFFIXES]
            for src, dst in pairs:
                if replace and dst.exists() and dst != root.resolve():
                    shutil.rmtree(dst)
                if src.is_dir():
                    _move_into(src, dst)
    archive.unlink(missing_ok=True)
    return str(target.relative_to(root.resolve()))


def fetch(root: Path, url: str, relative: str, sha512: str | None) -> int:
    """Download a mod/plugin from Modrinth or CurseForge's CDN into the server folder."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in FETCH_HOSTS:
        raise FileAccessError("Downloads are only allowed from Modrinth and CurseForge.", 400)
    path, tmp = open_upload(root, relative)
    digest = hashlib.sha512()
    size = 0
    req = urllib.request.Request(url, headers={"User-Agent": "hearth-agent"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:  # noqa: S310 (host allowlisted)
            while chunk := resp.read(1 << 16):
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    raise FileAccessError("Download is larger than 1 GB.", 413)
                digest.update(chunk)
                out.write(chunk)
        if sha512 and digest.hexdigest() != sha512:
            raise FileAccessError("Downloaded file doesn't match its checksum; not installed.", 502)
        tmp.replace(path)
    except OSError as exc:
        raise FileAccessError(f"Download failed: {exc}", 502) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return size
