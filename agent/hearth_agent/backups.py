"""Backups: compressed archives of a server's folder, restorable in place."""

from __future__ import annotations

import fnmatch
import json
import logging
import shutil
import tarfile
import threading
import time
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
                for path in sorted(source.rglob("*")):
                    rel = path.relative_to(source).as_posix()
                    if any(fnmatch.fnmatch(rel, pat) for pat in patterns):
                        continue
                    if path.is_dir() and not path.is_symlink():
                        continue  # directories are recreated from file paths
                    try:
                        tar.add(path, arcname=rel, recursive=False)
                    except (FileNotFoundError, PermissionError) as exc:  # file vanished mid-backup
                        log.info("skipped %s: %s", rel, exc)
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
        try:
            was_running = manager.stop_for_maintenance(uuid)
            dest = manager.data_path(uuid)
            staging = dest.with_name(f".{dest.name}.restoring")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            with tarfile.open(archive, "r:gz") as tar:
                # The "data" filter rejects absolute paths, ../ escapes, links pointing outside,
                # and device files, so a tampered archive can't write outside the server folder.
                tar.extractall(staging, filter="data")
            old = dest.with_name(f".{dest.name}.old")
            shutil.rmtree(old, ignore_errors=True)
            if dest.exists():
                dest.rename(old)
            staging.rename(dest)
            shutil.rmtree(old, ignore_errors=True)
            manager.end_restore(uuid, error=None, start=was_running)
        except Exception as exc:
            log.exception("restore of %s failed", uuid)
            manager.end_restore(uuid, error=f"Restore failed: {exc}"[:500], start=was_running)

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


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0

