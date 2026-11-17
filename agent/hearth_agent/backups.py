"""Backups: compressed archives of a server's folder, restorable in place."""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import logging
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .runtime import RuntimeFailure

if TYPE_CHECKING:
    from .runtime import ServerManager

log = logging.getLogger("hearth.backups")

# Never worth archiving: crash dumps, caches and log spam that can be huge.
DEFAULT_IGNORE = ["*.hprof", "cache/*", ".cache/*", "logs/*.log.gz", "crash-reports/*", "*.hearth-tmp"]


class BackupStore:
    def __init__(self, manager: "ServerManager") -> None:
        self.manager = manager
        self.root = manager.config.data_dir.parent / "backups"
        self.root.mkdir(parents=True, exist_ok=True)
        self._active: set[tuple[str, str]] = set()  # (server, backup) being written right now
        self._guard = threading.Lock()

    def _dir(self, uuid: str) -> Path:
        return self.root / uuid

    def archive_path(self, uuid: str, backup_id: str) -> Path:
        return self._dir(uuid) / f"{backup_id}.tar.gz"

    def _meta_path(self, uuid: str, backup_id: str) -> Path:
        return self._dir(uuid) / f"{backup_id}.json"

    def _write_meta(self, uuid: str, backup_id: str, **meta: Any) -> None:
        path = self._meta_path(uuid, backup_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(path)

    def status(self, uuid: str, backup_id: str) -> dict[str, Any]:
        path = self._meta_path(uuid, backup_id)
        if not path.exists():
            return {"state": "missing"}
        meta = json.loads(path.read_text())
        if meta.get("state") == "creating" and (uuid, backup_id) not in self._active:
            # The agent restarted while writing it; the thread that would finish it is gone.
            self.archive_path(uuid, backup_id).with_suffix(".part").unlink(missing_ok=True)
            meta = {**meta, "state": "failed", "error": "The node restarted during this backup."}
            self._write_meta(uuid, backup_id, **meta)
        return meta

    def creating(self, uuid: str) -> bool:
        with self._guard:
            return any(u == uuid for u, _ in self._active)

    # ----- create ------------------------------------------------------------------------

    def create(self, uuid: str, backup_id: str, ignore: list[str]) -> None:
        self.manager.load_spec(uuid)
        self.manager.ensure_idle(uuid)
        with self._guard:
            if any(u == uuid for u, _ in self._active):
                raise RuntimeFailure("A backup of this server is already running.")
            self._active.add((uuid, backup_id))
        self._write_meta(uuid, backup_id, state="creating", size=0, error=None, started=time.time())
        threading.Thread(target=self._create, args=(uuid, backup_id, ignore),
                         name=f"backup-{uuid[:8]}", daemon=True).start()

    def _create(self, uuid: str, backup_id: str, ignore: list[str]) -> None:
        source = self.manager.data_path(uuid)
        target = self.archive_path(uuid, backup_id)
        partial = target.with_suffix(".part")
        patterns = DEFAULT_IGNORE + list(ignore)
        try:
            with self.manager.quiesced(uuid), tarfile.open(partial, "w:gz", compresslevel=6) as tar:
                _add_tree(tar, source, patterns)
            partial.replace(target)
            self._write_meta(uuid, backup_id, state="ready", size=target.stat().st_size, error=None,
                             finished=time.time())
        except Exception as exc:
            log.exception("backup %s of %s failed", backup_id, uuid)
            partial.unlink(missing_ok=True)
            self._write_meta(uuid, backup_id, state="failed", size=0, error=str(exc)[:500])
        finally:
            with self._guard:
                self._active.discard((uuid, backup_id))

    # ----- restore / delete --------------------------------------------------------------

    def restore(self, uuid: str, backup_id: str) -> None:
        archive = self.archive_path(uuid, backup_id)
        if not archive.exists():
            raise RuntimeFailure("That backup file is missing on the node.")
        if self.creating(uuid):
            raise RuntimeFailure("A backup of this server is still being written; restore when it's done.")
        self.manager.begin_restore(uuid)
        threading.Thread(target=self._restore, args=(uuid, archive), name=f"restore-{uuid[:8]}", daemon=True).start()

    def _restore(self, uuid: str, archive: Path) -> None:
        manager = self.manager
        was_running = False
        dest = manager.data_path(uuid)
        staging = dest.with_name(f".{dest.name}.restoring")
        old = dest.with_name(f".{dest.name}.old")
        try:
            was_running = manager.stop_for_maintenance(uuid)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            with tarfile.open(archive, "r:gz") as tar:
                # The "data" filter rejects absolute paths, ../ escapes, links pointing outside,
                # and device files, so a tampered archive can't write outside the server folder.
                tar.extractall(staging, filter="data")
            shutil.rmtree(old, ignore_errors=True)
            if dest.exists():
                dest.rename(old)
            staging.rename(dest)
            shutil.rmtree(old, ignore_errors=True)
            manager.end_restore(uuid, error=None, start=was_running)
        except Exception as exc:
            log.exception("restore of %s failed", uuid)
            # Put the server's own files back if they were already moved aside, and drop the
            # half-unpacked copy so a failed restore (often a full disk) doesn't keep the disk full.
            if not dest.exists() and old.exists():
                old.rename(dest)
            shutil.rmtree(staging, ignore_errors=True)
            manager.end_restore(uuid, error=f"Restore failed, nothing was changed: {exc}"[:500], start=was_running)

    # ----- off-site copies ------------------------------------------------------------------
    # The panel holds the storage credentials and sends presigned URLs: one per part for an
    # upload, one for a download. The agent never sees the keys.

    def push(self, uuid: str, backup_id: str, urls: list[str], part_size: int) -> None:
        archive = self.archive_path(uuid, backup_id)
        if not archive.exists():
            raise RuntimeFailure("That backup file is missing on the node.")
        meta = self.status(uuid, backup_id)
        if (meta.get("push") or {}).get("state") == "uploading":
            raise RuntimeFailure("That backup is already being copied off-site.")
        self._write_meta(uuid, backup_id, **{**meta, "push": {"state": "uploading", "done": 0, "parts": len(urls)}})
        threading.Thread(target=self._push, args=(uuid, backup_id, urls, part_size),
                         name=f"push-{backup_id[:8]}", daemon=True).start()

    def _push(self, uuid: str, backup_id: str, urls: list[str], part_size: int) -> None:
        archive = self.archive_path(uuid, backup_id)
        etags: list[str] = []
        try:
            size = archive.stat().st_size
            with open(archive, "rb") as f:
                for n, url in enumerate(urls):
                    offset = n * part_size if len(urls) > 1 else 0
                    length = min(part_size, size - offset) if len(urls) > 1 else size
                    etags.append(_put_range(f, offset, length, url))
                    meta = self.status(uuid, backup_id)
                    self._write_meta(uuid, backup_id, **{**meta, "push": {"state": "uploading", "done": n + 1,
                                                                           "parts": len(urls)}})
            push = {"state": "done", "etags": etags}
        except Exception as exc:
            log.warning("off-site copy of %s failed: %s", backup_id, exc)
            push = {"state": "failed", "error": str(exc)[:500]}
        meta = self.status(uuid, backup_id)
        self._write_meta(uuid, backup_id, **{**meta, "push": push})

    def pull(self, uuid: str, backup_id: str, url: str) -> None:
        """Download a backup from off-site storage back onto this node (e.g. after a disk swap,
        or onto a new node when moving a server)."""
        self.manager.load_spec(uuid)
        with self._guard:
            if (uuid, backup_id) in self._active:
                raise RuntimeFailure("That backup is busy.")
            self._active.add((uuid, backup_id))
        self._write_meta(uuid, backup_id, state="creating", size=0, error=None, started=time.time())
        threading.Thread(target=self._pull, args=(uuid, backup_id, url), name=f"pull-{backup_id[:8]}",
                         daemon=True).start()

    def _pull(self, uuid: str, backup_id: str, url: str) -> None:
        target = self.archive_path(uuid, backup_id)
        partial = target.with_suffix(".part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:  # noqa: S310 (panel-signed URL)
                need = int(resp.headers.get("Content-Length") or 0)
                if need and need > shutil.disk_usage(target.parent).free - 256 * 1024 * 1024:
                    raise RuntimeError(f"not enough free disk space ({need // 1048576} MB needed)")
                with open(partial, "wb") as out:
                    shutil.copyfileobj(resp, out, 1 << 20)
            partial.replace(target)
            self._write_meta(uuid, backup_id, state="ready", size=target.stat().st_size, error=None,
                             finished=time.time(), push={"state": "done"})
        except Exception as exc:
            partial.unlink(missing_ok=True)
            self._write_meta(uuid, backup_id, state="failed", size=0, error=f"Download failed: {exc}"[:500])
        finally:
            with self._guard:
                self._active.discard((uuid, backup_id))

    def receive_open(self, uuid: str, backup_id: str, length: int):
        """Start receiving a backup archive sent by the panel (moving a server here from another
        node). Returns a writable file; finish with receive_done() or receive_abort()."""
        self.manager.load_spec(uuid)
        target = self.archive_path(uuid, backup_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if length and length > shutil.disk_usage(target.parent).free - 256 * 1024 * 1024:
            raise RuntimeFailure(f"This node doesn't have {length // 1048576} MB free for the server's files.")
        with self._guard:
            if (uuid, backup_id) in self._active:
                raise RuntimeFailure("That backup is busy.")
            self._active.add((uuid, backup_id))
        self._write_meta(uuid, backup_id, state="creating", size=0, error=None, started=time.time())
        return open(target.with_suffix(".part"), "wb")

    def receive_done(self, uuid: str, backup_id: str, f) -> None:
        f.close()
        target = self.archive_path(uuid, backup_id)
        try:
            target.with_suffix(".part").replace(target)
            with tarfile.open(target) as tar:  # refuse a truncated or corrupt transfer now, not at restore
                for _ in tar:
                    pass
            self._write_meta(uuid, backup_id, state="ready", size=target.stat().st_size, error=None, finished=time.time())
        except (OSError, EOFError, tarfile.TarError) as exc:
            target.unlink(missing_ok=True)
            self._write_meta(uuid, backup_id, state="failed", size=0, error=f"Transfer was corrupt: {exc}"[:500])
            raise RuntimeFailure("The backup arrived corrupt.") from exc
        finally:
            with self._guard:
                self._active.discard((uuid, backup_id))

    def receive_abort(self, uuid: str, backup_id: str, f) -> None:
        f.close()
        self.archive_path(uuid, backup_id).with_suffix(".part").unlink(missing_ok=True)
        self._meta_path(uuid, backup_id).unlink(missing_ok=True)
        with self._guard:
            self._active.discard((uuid, backup_id))

    def delete(self, uuid: str, backup_id: str) -> None:
        if (uuid, backup_id) in self._active:
            raise RuntimeFailure("That backup is still being written.")
        self.archive_path(uuid, backup_id).unlink(missing_ok=True)
        self._meta_path(uuid, backup_id).unlink(missing_ok=True)
        self.archive_path(uuid, backup_id).with_suffix(".part").unlink(missing_ok=True)

    def delete_all(self, uuid: str) -> None:
        shutil.rmtree(self._dir(uuid), ignore_errors=True)

    def usage(self) -> int:
        return sum(f.stat().st_size for f in self.root.rglob("*.tar.gz") if f.is_file())


class _Slice:
    """Read at most `length` bytes of an open file from `offset` (one multipart part)."""

    def __init__(self, f, offset: int, length: int) -> None:
        self.f, self.left = f, length
        f.seek(offset)

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.f.read(n)
        self.left -= len(data)
        return data


def _put_range(f, offset: int, length: int, url: str, attempts: int = 3) -> str:
    """PUT one part (or the whole file) to a presigned URL; returns the ETag. Retries transient
    failures, re-reading the same bytes each time."""
    if not url.startswith(("https://", "http://")):
        raise RuntimeError("bad upload URL")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=_Slice(f, offset, length), method="PUT",
                                         headers={"Content-Length": str(length)})
            with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 (panel-signed URL)
                return (resp.headers.get("ETag") or "").strip()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code < 500:
                break  # e.g. expired or wrong signature: retrying won't help
        except OSError as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"upload failed: {last}")


def _add_tree(tar: tarfile.TarFile, source: Path, patterns: list[str]) -> None:
    """Archive a live server folder through fds (see files.py): a mod swapping a folder for a
    link mid-backup can't make the agent read files from outside the server folder."""
    from . import files

    with files.Dir(source, []) as top:
        for rel, dirs, names, fd in files.walk(top):
            for name in sorted(dirs):  # keep empty folders too
                path = files._join(rel, name)
                if any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path + "/-", pat) for pat in patterns):
                    dirs.remove(name)  # e.g. "cache/*": skip the folder and don't walk into it
                    continue
                info = tarfile.TarInfo(path)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            for name in sorted(names):
                path = files._join(rel, name)
                if any(fnmatch.fnmatch(path, pat) for pat in patterns):
                    continue
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode):
                        info = tarfile.TarInfo(path)
                        info.type, info.linkname = tarfile.SYMTYPE, os.readlink(name, dir_fd=fd)
                        info.mtime = int(st.st_mtime)
                        tar.addfile(info)
                        continue
                    file_fd = files._open_regular(name, fd)
                except (OSError, files.FileAccessError) as exc:  # vanished, or a FIFO/socket
                    log.info("skipped %s: %s", path, exc)
                    continue
                with os.fdopen(file_fd, "rb") as f:
                    info = tar.gettarinfo(arcname=path, fileobj=f)
                    tar.addfile(info, f)

