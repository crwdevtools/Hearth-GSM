"""Sandboxed file access inside one server's data directory.

The server folder is shared with the game container, and whatever runs there (a mod, a plugin)
can create, replace and swap files in it at any moment. The agent runs as root, so a check made
on a path and an action taken later on the same path can be raced: swap a folder for a symlink
in between and the action lands anywhere on the host. Everything here therefore walks paths one
component at a time with O_NOFOLLOW, and acts through the resulting directory file descriptors
(dir_fd=...), never through a path string that the container could change underneath us.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_FETCH_BYTES = 1024 * 1024 * 1024
FREE_SPACE_MARGIN = 256 * 1024 * 1024  # never fill a node's disk to the last byte
FETCH_HOSTS = {"cdn.modrinth.com", "edge.forgecdn.net", "mediafilez.forgecdn.net"}

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# O_NONBLOCK: opening a FIFO someone planted must not hang the agent; regular files ignore it.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


class FileAccessError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


OUTSIDE = "Path is outside the server directory."


# ----- walking without following links --------------------------------------------------------


def split(relative: str) -> list[str]:
    """Path components of a request path, refusing anything that could climb out."""
    if "\x00" in relative:
        raise FileAccessError("Invalid path.", 400)
    parts = [p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FileAccessError(OUTSIDE, 403)
    return parts


def open_dir(root: Path, parts: list[str], *, create: bool = False) -> int:
    """A directory fd for root/parts, refusing any symlink on the way. Caller closes it."""
    fd = os.open(root, _DIR_FLAGS)
    try:
        for name in parts:
            nfd = child_dir(fd, name, create=create)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def child_dir(parent_fd: int, name: str, *, create: bool = False) -> int:
    """Open (or create) one folder inside an open folder, refusing a symlink. Caller closes it."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise FileAccessError("Directory not found.", 404) from None
    except OSError as exc:
        raise _step_error(exc, parent_fd, name) from None
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass  # created meanwhile; the open below checks what it is
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _step_error(exc, parent_fd, name) from None


def _step_error(exc: OSError, parent_fd: int, name: str) -> FileAccessError:
    # With O_NOFOLLOW|O_DIRECTORY, Linux reports a symlink as ELOOP or ENOTDIR depending on the
    # kernel; either way a link where a folder was expected means "outside".
    st = _lstat(name, parent_fd)
    if exc.errno == errno.ELOOP or (st is not None and stat.S_ISLNK(st.st_mode)):
        return FileAccessError(OUTSIDE, 403)
    if exc.errno == errno.ENOTDIR:
        return FileAccessError("Directory not found.", 404)
    return FileAccessError(f"Can't open that folder: {exc.strerror}.", 400)


class Dir:
    """`with Dir(root, parts) as fd:` closes the fd however the block ends."""

    def __init__(self, root: Path, parts: list[str], *, create: bool = False) -> None:
        self.fd = open_dir(root, parts, create=create)

    def __enter__(self) -> int:
        return self.fd

    def __exit__(self, *exc: Any) -> None:
        os.close(self.fd)


def _lstat(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_regular(name: str, dir_fd: int) -> int:
    """Open a regular file for reading without following a link; anything else is 'not found'."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise FileAccessError("File not found.", 404) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileAccessError(OUTSIDE, 403) from None
        raise FileAccessError("File not found.", 404) from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FileAccessError("File not found.", 404)
    return fd


def _remove(name: str, dir_fd: int) -> None:
    st = _lstat(name, dir_fd)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)  # fd-based on Linux: doesn't follow links inside
    else:
        os.unlink(name, dir_fd=dir_fd)


def free_space(dir_fd: int) -> int:
    st = os.fstatvfs(dir_fd)
    return st.f_bavail * st.f_frsize


# ----- the operations the panel calls ---------------------------------------------------------


def list_dir(root: Path, relative: str) -> list[dict[str, Any]]:
    with Dir(root, split(relative)) as fd:
        entries = []
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": entry.name,
                    "is_dir": stat.S_ISDIR(st.st_mode),
                    "is_symlink": stat.S_ISLNK(st.st_mode),
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                })
    entries.sort(key=lambda e: (not e["is_dir"], str(e["name"]).lower()))
    return entries


def read_text(root: Path, relative: str) -> str:
    parts = split(relative)
    if not parts:
        raise FileAccessError("File not found.", 404)
    with Dir(root, parts[:-1]) as d:
        fd = _open_regular(parts[-1], d)
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_size > MAX_READ_BYTES:
            raise FileAccessError("File is larger than 2 MB; download it instead.", 413)
        data = f.read(MAX_READ_BYTES + 1)
    if b"\x00" in data[:8192]:
        raise FileAccessError("This looks like a binary file and can't be edited here.", 415)
    return data.decode("utf-8", "replace")


def open_read(root: Path, relative: str) -> tuple[BinaryIO, int, str]:
    """(file, size, name) for a download. The caller closes the file."""
    parts = split(relative)
    if not parts:
        raise FileAccessError("File not found.", 404)
    with Dir(root, parts[:-1]) as d:
        fd = _open_regular(parts[-1], d)
    f = os.fdopen(fd, "rb")
    return f, os.fstat(fd).st_size, parts[-1]


# Set by the agent: how many more bytes a server folder may grow by (None == no limit).
quota_room: Callable[[Path], int | None] = lambda _root: None


def _limit_error(room: int) -> FileAccessError:
    return FileAccessError("This server is at its disk limit. Delete some files or ask an admin to raise it."
                           if room <= 0 else f"This server only has {room // 1048576} MB left under its disk limit.", 507)


class PendingWrite:
    """A file being written next to its final name, then swapped in atomically by commit().
    Holds the parent folder open the whole time, so the folder can't be swapped for a link."""

    def __init__(self, root: Path, relative: str, *, margin: int = FREE_SPACE_MARGIN) -> None:
        self.margin = margin
        self.room = quota_room(root)
        if self.room is not None and self.room <= 0:
            raise _limit_error(self.room)
        parts = split(relative)
        if not parts:
            raise FileAccessError("Cannot overwrite a directory.", 400)
        self.name = parts[-1]
        self.tmp = f".{self.name}.hearth-tmp"
        self.dir_fd = open_dir(root, parts[:-1], create=True)
        try:
            existing = _lstat(self.name, self.dir_fd)
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                raise FileAccessError("A folder with that name already exists.", 409)
            _remove(self.tmp, self.dir_fd)
            fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o644,
                         dir_fd=self.dir_fd)
            self.file: BinaryIO = os.fdopen(fd, "wb")
            self.free = free_space(self.dir_fd)
        except BaseException:
            os.close(self.dir_fd)
            raise
        self.written = 0
        self._done = False

    def write(self, chunk: bytes) -> None:
        self.written += len(chunk)
        if self.written > self.free - self.margin:
            raise FileAccessError("Not enough free disk space on the node for this file.", 507)
        if self.room is not None and self.written > self.room:
            raise _limit_error(self.room)
        self.file.write(chunk)

    def commit(self) -> None:
        self.file.close()
        try:
            os.replace(self.tmp, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        except OSError as exc:  # e.g. a folder appeared under that name meanwhile
            raise FileAccessError(f"Couldn't save {self.name}: {exc.strerror}.", 409) from None
        self._finish()

    def abort(self) -> None:
        if self._done:
            return
        try:
            self.file.close()
            os.unlink(self.tmp, dir_fd=self.dir_fd)
        except OSError:
            pass
        self._finish()

    def _finish(self) -> None:
        if not self._done:
            self._done = True
            os.close(self.dir_fd)


def open_upload(root: Path, relative: str) -> PendingWrite:
    return PendingWrite(root, relative)


def write_text(root: Path, relative: str, content: str) -> None:
    # No safety margin for small edits: fixing a config on a nearly full disk must still work.
    pending = PendingWrite(root, relative, margin=0)
    try:
        pending.write(content.encode("utf-8"))
        pending.commit()
    except BaseException:
        pending.abort()
        raise


def make_dir(root: Path, relative: str) -> None:
    os.close(open_dir(root, split(relative), create=True))


def delete(root: Path, relative: str) -> None:
    parts = split(relative)
    if not parts:
        raise FileAccessError("Refusing to act on the server root.", 403)
    with Dir(root, parts[:-1]) as d:
        if _lstat(parts[-1], d) is None:
            raise FileAccessError("Not found.", 404)
        _remove(parts[-1], d)  # a link is removed itself, never what it points to


def rename(root: Path, source: str, target: str) -> None:
    src, dst = split(source), split(target)
    if not src or not dst:
        raise FileAccessError("Refusing to act on the server root.", 403)
    with Dir(root, src[:-1]) as sd, Dir(root, dst[:-1], create=True) as dd:
        if _lstat(src[-1], sd) is None:
            raise FileAccessError("Not found.", 404)
        if _lstat(dst[-1], dd) is not None:
            raise FileAccessError("A file with that name already exists.", 409)
        try:
            os.rename(src[-1], dst[-1], src_dir_fd=sd, dst_dir_fd=dd)
        except OSError as exc:
            raise FileAccessError(f"Couldn't rename: {exc.strerror}.", 400) from None


def hash_files(root: Path, relative: str, suffixes: tuple[str, ...] = (".jar",)) -> dict[str, str]:
    """SHA-512 of each regular file in one folder whose name ends with `suffixes` (not recursive).
    The panel asks Modrinth which project each hash belongs to."""
    out: dict[str, str] = {}
    with Dir(root, split(relative)) as d:
        for name in sorted(os.listdir(d)):
            if not name.endswith(suffixes):
                continue
            try:
                fd = _open_regular(name, d)
            except FileAccessError:
                continue
            digest = hashlib.sha512()
            with os.fdopen(fd, "rb") as f:
                while chunk := f.read(1 << 20):
                    digest.update(chunk)
            out[name] = digest.hexdigest()
    return out


# ----- walking a whole tree -------------------------------------------------------------------


def walk(dir_fd: int) -> Iterator[tuple[str, list[str], list[str], int]]:
    """os.fwalk below an open folder: (relative dir, dir names, other names, dir fd). fwalk never
    follows links and acts on fds, so a swap mid-walk can't lead it outside the folder."""
    for dirpath, dirnames, filenames, fd in os.fwalk(".", dir_fd=dir_fd, follow_symlinks=False):
        rel = os.path.normpath(dirpath)
        # fwalk lists links to folders among the folders (it just doesn't enter them); report
        # them with the other non-folders so callers treat them as links.
        links = [d for d in dirnames if (st := _lstat(d, fd)) is None or stat.S_ISLNK(st.st_mode)]
        for d in links:
            dirnames.remove(d)
        yield ("" if rel == "." else rel), dirnames, filenames + links, fd


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def archive_folder(root: Path, relative: str, out: Path) -> Path:
    """Zip a folder (e.g. the world) for download. Links and special files are left out."""
    parts = split(relative)
    top = parts[-1] if parts else (root.name or "server")
    with Dir(root, parts) as d, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, _dirs, names, fd in walk(d):
            for name in sorted(names):
                try:
                    file_fd = _open_regular(name, fd)
                except FileAccessError:
                    continue  # a link, socket, FIFO, or gone
                with os.fdopen(file_fd, "rb") as src:
                    mtime = time.localtime(max(os.fstat(file_fd).st_mtime, 315532800))  # zip can't go before 1980
                    info = zipfile.ZipInfo(f"{top}/{_join(rel, name)}", date_time=mtime[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with zf.open(info, "w", force_zip64=True) as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
    return out


def copy_tree(source_root: Path, dest: Path) -> None:
    """Copy one server's folder into another's (cloning). The source may be live, so it is read
    through fds; the destination belongs to a stopped server only the agent touches."""
    dest.mkdir(parents=True, exist_ok=True)
    with Dir(source_root, []) as d:
        for rel, dirs, names, fd in walk(d):
            target_dir = dest / rel if rel else dest
            for name in dirs:
                (target_dir / name).mkdir(exist_ok=True)
            for name in names:
                st = _lstat(name, fd)
                if st is None:
                    continue
                target = target_dir / name
                if stat.S_ISLNK(st.st_mode):
                    link = os.readlink(name, dir_fd=fd)
                    if target.is_symlink() or target.exists():
                        target.unlink()
                    os.symlink(link, target)  # copied as a link; never followed by the agent
                    continue
                try:
                    file_fd = _open_regular(name, fd)
                except FileAccessError:
                    continue
                out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | _CLOEXEC, 0o644)
                with os.fdopen(file_fd, "rb") as src, os.fdopen(out_fd, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.utime(target, (st.st_atime, st.st_mtime))


# ----- importing archives ---------------------------------------------------------------------


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


def _check_size(entries: list[int], free: int, room: int | None = None) -> None:
    total = sum(entries)
    if total > free - FREE_SPACE_MARGIN:
        raise FileAccessError(f"Unpacked, this archive needs {total // 1048576} MB; the node has "
                              f"{free // 1048576} MB free.", 507)
    if room is not None and total > room:
        raise FileAccessError(f"Unpacked, this archive needs {total // 1048576} MB; the server has "
                              f"{max(room, 0) // 1048576} MB left under its disk limit.", 507)


def _move_into(source: Path, dest_fd: int) -> None:
    """Move everything in `source` (private to the agent) into an open folder, replacing
    same-named entries."""
    for item in source.iterdir():
        _remove(item.name, dest_fd)
        try:
            os.rename(item, item.name, dst_dir_fd=dest_fd)
        except OSError as exc:
            raise FileAccessError(f"Couldn't move {item.name} into place: {exc.strerror}.", 400) from None


def extract_archive(root: Path, archive_rel: str, target_rel: str, *, replace: bool, world: bool,
                    staging_root: Path) -> str:
    """Unpack an uploaded archive into the server folder. It is first unpacked into
    `staging_root`, which the game container can't see (so nothing can be planted mid-extract),
    then moved into place through folder fds."""
    archive_parts, target_parts = split(archive_rel), split(target_rel)
    if not archive_parts:
        raise FileAccessError("Archive not found.", 404)
    if not target_parts and replace:
        raise FileAccessError("Refusing to replace the whole server folder.", 403)
    name = archive_parts[-1].lower()
    if not name.endswith((".zip", ".tar.gz", ".tgz", ".tar")):
        raise FileAccessError("Upload a .zip, .tar.gz or .tgz file.", 415)
    staging_root.mkdir(parents=True, exist_ok=True)
    with Dir(root, archive_parts[:-1]) as archive_dir, \
            tempfile.TemporaryDirectory(dir=staging_root, prefix="extract-") as tmp:
        staging = Path(tmp)
        free = free_space(archive_dir)
        room = quota_room(root)
        with os.fdopen(_open_regular(archive_parts[-1], archive_dir), "rb") as archive:
            if name.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    _check_size([i.file_size for i in zf.infolist()], free, room)  # zip bombs
                    _safe_zip_extract(zf, staging)
            else:
                with tarfile.open(fileobj=archive) as tar:
                    _check_size([m.size for m in tar.getmembers()], free, room)
                    tar.extractall(staging, filter="data")
        if not world:
            with Dir(root, target_parts, create=True) as dest:
                if replace:  # empty it first; the folder itself stays (it might be a mount)
                    for entry in os.listdir(dest):
                        _remove(entry, dest)
                _move_into(staging, dest)
        else:
            source = _find_world(staging)
            if source is None:
                raise FileAccessError("No Minecraft world in that archive (couldn't find level.dat).", 400)
            # Replace the overworld and its Paper dimension folders together; leftovers from the
            # old world would otherwise reappear as its nether and end.
            with Dir(root, target_parts[:-1], create=True) as parent:
                world_name = target_parts[-1]
                for suffix in ("",) + DIMENSION_SUFFIXES:
                    src = source.with_name(source.name + suffix)
                    if replace:
                        _remove(world_name + suffix, parent)
                    if src.is_dir():
                        dst = child_dir(parent, world_name + suffix, create=True)
                        try:
                            _move_into(src, dst)
                        finally:
                            os.close(dst)
        os.unlink(archive_parts[-1], dir_fd=archive_dir)
    return "/".join(target_parts)




# ----- mod downloads ----------------------------------------------------------------------------


def _check_fetch_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in FETCH_HOSTS:
        raise FileAccessError("Downloads are only allowed from Modrinth and CurseForge.", 400)


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a CDN's redirects only while they stay on the allowed hosts, so a redirect can't
    send the agent somewhere inside its own network."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_fetch_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_AllowlistRedirects)


def fetch(root: Path, url: str, relative: str, sha512: str | None) -> int:
    """Download a mod/plugin from Modrinth or CurseForge's CDN into the server folder."""
    _check_fetch_url(url)
    pending = PendingWrite(root, relative)
    digest = hashlib.sha512()
    req = urllib.request.Request(url, headers={"User-Agent": "hearth-agent"})
    try:
        with _opener.open(req, timeout=60) as resp:  # noqa: S310 (host allowlisted, per redirect too)
            while chunk := resp.read(1 << 16):
                if pending.written + len(chunk) > MAX_FETCH_BYTES:
                    raise FileAccessError("Download is larger than 1 GB.", 413)
                digest.update(chunk)
                pending.write(chunk)
        if sha512 and digest.hexdigest() != sha512:
            raise FileAccessError("Downloaded file doesn't match its checksum; not installed.", 502)
        pending.commit()
    except OSError as exc:
        pending.abort()
        raise FileAccessError(f"Download failed: {exc}", 502) from exc
    except BaseException:
        pending.abort()
        raise
    return pending.written
