"""Docker-backed server runtime: container lifecycle, status, and resource sampling."""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .models import ServerSpec

log = logging.getLogger("hearth.agent")

LABEL_MANAGED = "hearth.managed"
LABEL_SERVER = "hearth.server"


class ServerNotFound(Exception):
    pass


class RuntimeFailure(Exception):
    """A Docker operation failed in a way the user should see."""


def container_name(uuid: str) -> str:
    return f"hearth-{uuid}"


def build_create_kwargs(spec: ServerSpec, data_path: Path) -> dict[str, Any]:
    """Translate a ServerSpec into docker-py `containers.create` keyword arguments.

    Pure function so it can be tested without a Docker daemon.
    """
    ports: dict[str, list[tuple[str, int]]] = {}
    for p in spec.ports:
        ports.setdefault(f"{p.container_port}/{p.protocol}", []).append((p.host_ip, p.host_port))

    kwargs: dict[str, Any] = {
        "image": spec.image,
        "name": container_name(spec.uuid),
        "environment": dict(spec.env),
        "ports": ports,
        "volumes": {str(data_path): {"bind": spec.data_mount, "mode": "rw"}},
        "mem_limit": f"{spec.memory_mb}m",
        # memswap is memory + swap; equal to mem_limit means "no swap".
        "memswap_limit": f"{spec.memory_mb + spec.swap_mb}m",
        "stdin_open": True,
        "tty": True,
        "detach": True,
        "labels": {LABEL_MANAGED: "1", LABEL_SERVER: spec.uuid},
        # Crashes restart; a clean exit (e.g. `stop` typed in the console) stays stopped. The agent
        # itself restores servers that should be running after a host reboot (see resume()).
        "restart_policy": {"Name": "on-failure", "MaximumRetryCount": 10},
        "log_config": {"type": "json-file", "config": {"max-size": "10m", "max-file": "3"}},
    }
    if spec.cpu_percent > 0:
        kwargs["nano_cpus"] = spec.cpu_percent * 10_000_000
    if spec.command:
        kwargs["command"] = spec.command
    return kwargs


def host_path_for(mounts: list[dict[str, Any]], path: Path) -> Path | None:
    """Translate a path inside this container to the path the Docker daemon uses for it,
    from the container's mount table. Returns None when `path` isn't under a bind mount."""
    best: tuple[int, Path] | None = None
    for m in mounts:
        dest = Path(m.get("Destination", ""))
        source = m.get("Source")
        if not source or m.get("Type", "bind") != "bind":
            continue
        if path == dest or dest in path.parents:
            depth = len(dest.parts)
            if best is None or depth > best[0]:  # the most specific mount wins
                best = (depth, Path(source) / path.relative_to(dest))
    return best[1] if best else None


def inspect_self(docker_client: Any) -> dict[str, Any]:
    """The agent's own container (image, networks), or {} when not running in one."""
    import socket

    try:
        return docker_client.containers.get(socket.gethostname()).attrs or {}
    except Exception:
        return {}


def detect_host_root(docker_client: Any, root: Path) -> Path:
    """Find where `root` lives on the Docker host by inspecting the agent's own container.

    This is what lets the data folder be on any drive (D:\\Hearth, /mnt/games, ~/hearth): game
    containers are created by the host's Docker daemon, so their bind mounts must use the
    daemon's path, not the agent's. Outside a container the paths are the same.
    """
    import socket

    try:
        me = docker_client.containers.get(socket.gethostname())
        found = host_path_for(me.attrs.get("Mounts") or [], root)
    except Exception:
        found = None
    if found is None:
        log.info("not running in a container (or no mount for %s); using it as the host path", root)
        return root
    return found


def cpu_percent_from_stats(stats: dict[str, Any]) -> float:
    """CPU usage as a percentage of one core (100 == one full core), like `docker stats`."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        online = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
    except (KeyError, TypeError):
        return 0.0
    if cpu_delta <= 0 or sys_delta <= 0:
        return 0.0
    return round(cpu_delta / sys_delta * online * 100.0, 1)


def memory_bytes_from_stats(stats: dict[str, Any]) -> int:
    mem = stats.get("memory_stats") or {}
    usage = int(mem.get("usage") or 0)
    detail = mem.get("stats") or {}
    # cgroup v2 reports inactive_file, v1 reports total_inactive_file; docker CLI subtracts it.
    cache = int(detail.get("inactive_file") or detail.get("total_inactive_file") or 0)
    return max(usage - cache, 0)


def network_bytes_from_stats(stats: dict[str, Any]) -> tuple[int, int]:
    rx = tx = 0
    for iface in (stats.get("networks") or {}).values():
        rx += int(iface.get("rx_bytes") or 0)
        tx += int(iface.get("tx_bytes") or 0)
    return rx, tx


def directory_size(path: Path, limit_entries: int = 500_000) -> int:
    total = 0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > limit_entries:
                        return total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


@dataclass
class Sample:
    ts: float
    cpu: float
    memory: int


@dataclass
class ServerRuntimeState:
    install: str = "ready"  # installing | ready | failed
    pending: int = 0  # queued (re)provisions; "installing" until all have run
    error: str | None = None
    transition: str | None = None  # starting | stopping, set while a power action runs
    history: deque[Sample] = field(default_factory=lambda: deque(maxlen=60))
    network: tuple[int, int] = (0, 0)
    disk_bytes: int = 0
    disk_checked: float = 0.0
    oom_kills: int = 0
    oom_seen: str | None = None  # container exit already counted from Docker's OOMKilled flag
    # Per-minute averages for the last 24 h: (minute_ts, cpu, memory_bytes).
    minutes: deque[tuple[int, float, int]] = field(default_factory=lambda: deque(maxlen=1440))
    minute_acc: list[tuple[float, int]] = field(default_factory=list)
    minute_key: int = 0
    wake_key: str | None = None


class ServerManager:
    def __init__(self, config: AgentConfig, docker_client: Any) -> None:
        self.config = config
        self.docker = docker_client
        self._states: dict[str, ServerRuntimeState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hearth-stats")
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        root = config.data_dir.parent
        self.host_root = config.host_root or detect_host_root(docker_client, root)
        self.host_data_dir = self.host_root / config.data_dir.relative_to(root)
        log.info("server files: %s in the agent, %s on the Docker host", config.data_dir, self.host_data_dir)

    # ----- bookkeeping -------------------------------------------------------------------

    def _state(self, uuid: str) -> ServerRuntimeState:
        with self._registry_lock:
            if uuid not in self._states:
                st = ServerRuntimeState()
                st.history = deque(maxlen=self.config.history_size)
                self._states[uuid] = st
            return self._states[uuid]

    def _lock(self, uuid: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(uuid, threading.Lock())

    def data_path(self, uuid: str) -> Path:
        """A server's files as the agent sees them."""
        return self.config.data_dir / uuid

    def host_data_path(self, uuid: str) -> Path:
        """The same folder as the Docker daemon sees it; used for the game container's mount."""
        return self.host_data_dir / uuid

    def _spec_path(self, uuid: str) -> Path:
        return self.config.state_dir / f"{uuid}.json"

    def _run_marker(self, uuid: str) -> Path:
        return self.config.state_dir / f"{uuid}.running"

    def _want_running(self, uuid: str, running: bool) -> None:
        """Remember whether the server should be up, so the agent can restore it after a reboot."""
        marker = self._run_marker(uuid)
        if running:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)

    def load_spec(self, uuid: str) -> ServerSpec:
        path = self._spec_path(uuid)
        if not path.exists():
            raise ServerNotFound(uuid)
        return ServerSpec.model_validate_json(path.read_text())

    def disk_used(self, uuid: str, max_age: float = 10.0) -> int:
        st = self._state(uuid)
        now = time.time()
        if now - st.disk_checked > max_age:
            st.disk_bytes = directory_size(self.data_path(uuid))
            st.disk_checked = now
        return st.disk_bytes

    def disk_room(self, root: Path) -> int | None:
        """Bytes a server folder may still grow by under its disk limit (None: no limit)."""
        uuid = root.name
        try:
            spec = self.load_spec(uuid)
        except (ServerNotFound, ValueError):
            return None
        if not spec.disk_mb or root != self.data_path(uuid):
            return None
        return spec.disk_mb * 1048576 - self.disk_used(uuid)

    def known_servers(self) -> list[str]:
        return sorted(p.stem for p in self.config.state_dir.glob("*.json"))

    def _container(self, uuid: str) -> Any | None:
        from docker.errors import NotFound

        try:
            return self.docker.containers.get(container_name(uuid))
        except NotFound:
            return None

    # ----- provisioning ------------------------------------------------------------------

    def apply_spec(self, spec: ServerSpec, start: bool = False) -> None:
        """Persist the spec, then pull and (re)create the container in a background thread."""
        self.ensure_idle(spec.uuid)
        path = self._spec_path(spec.uuid)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(spec.model_dump_json(indent=2))
        tmp.replace(path)
        st = self._state(spec.uuid)
        with self._registry_lock:
            st.pending += 1
            st.install, st.error, st.oom_kills = "installing", None, 0
        threading.Thread(
            target=self._provision, args=(spec.uuid, start), name=f"provision-{spec.uuid[:8]}", daemon=True
        ).start()

    def _provision(self, uuid: str, start: bool) -> None:
        st = self._state(uuid)
        mine: str | None = None
        with self._lock(uuid):
            try:
                # Always build from the newest spec on disk: if two updates race for the lock,
                # the older one must not win. A deleted server has no spec and is skipped.
                failed = False
                try:
                    spec = self.load_spec(uuid)
                except ServerNotFound:
                    return
                self.data_path(uuid).mkdir(parents=True, exist_ok=True)
                self._pull(spec.image)
                existing = self._container(spec.uuid)
                was_running = bool(existing and existing.status in ("running", "restarting"))
                if existing is not None:
                    if was_running:
                        st.transition = mine = "stopping"
                        existing.stop(timeout=spec.stop_timeout)
                    existing.remove(force=True)
                container = self.docker.containers.create(**build_create_kwargs(spec, self.host_data_path(uuid)))
                if start or was_running:
                    st.transition = mine = "starting"
                    self._remove_sleeper(uuid)
                    container.start()
                    self._want_running(uuid, True)
                failed = False
            except Exception as exc:  # surfaced to the panel verbatim
                log.exception("provisioning %s failed", uuid)
                st.error = _docker_error_text(exc)
                failed = True
            finally:
                _end_transition(st, mine)
                with self._registry_lock:
                    st.pending -= 1
                    if st.pending <= 0:
                        st.pending = 0
                        st.install = "failed" if failed else "ready"

    def _pull(self, image: str) -> None:
        from docker.errors import ImageNotFound

        try:
            self.docker.images.get(image)
            # Refresh mutable tags (e.g. :latest, :java21) but keep going if the registry is down.
            try:
                self.docker.images.pull(image)
            except Exception:
                log.warning("could not refresh %s; using local copy", image)
        except ImageNotFound:
            self.docker.images.pull(image)

    def delete(self, uuid: str, purge: bool) -> None:
        self.ensure_idle(uuid)
        with self._lock(uuid):
            container = self._container(uuid)
            if container is not None:
                container.remove(force=True)
            self._remove_sleeper(uuid)
            self._spec_path(uuid).unlink(missing_ok=True)
            self._run_marker(uuid).unlink(missing_ok=True)
            if purge:
                shutil.rmtree(self.data_path(uuid), ignore_errors=True)
        with self._registry_lock:
            self._states.pop(uuid, None)

    # ----- power -------------------------------------------------------------------------

    def power(self, uuid: str, action: str) -> None:
        spec = self.load_spec(uuid)
        st = self._state(uuid)
        self.ensure_idle(uuid)
        if st.install == "installing":
            raise RuntimeFailure("Server is still installing.")
        container = self._container(uuid)
        if container is None:
            raise RuntimeFailure("Container is missing. Reinstall the server from Settings.")
        if action in ("stop", "kill") and self._sleeper(uuid) is not None:
            self._remove_sleeper(uuid)  # "stop" on a sleeping server means: stop waking it
            return
        mine = {"start": "starting", "stop": "stopping", "restart": "stopping"}.get(action)
        if mine:
            st.transition = mine
        self._want_running(uuid, action in ("start", "restart"))

        def run() -> None:
            try:
                if action == "kill":
                    # No lock: kill must interrupt a stop that is still waiting for a clean shutdown.
                    container.kill()
                    return
                with self._lock(uuid):
                    if action in ("start", "restart"):
                        self._remove_sleeper(uuid)
                    if action == "start":
                        container.start()
                    elif action == "stop":
                        container.stop(timeout=self._stop_wait(container, spec))
                    elif action == "restart":
                        container.restart(timeout=self._stop_wait(container, spec))
            except Exception as exc:
                st.error = _docker_error_text(exc)
                log.warning("power %s on %s failed: %s", action, uuid, exc)
            else:
                st.error = None
                st.oom_kills = 0
            finally:
                _end_transition(st, mine)

        threading.Thread(target=run, name=f"power-{uuid[:8]}", daemon=True).start()

    @staticmethod
    def _stop_wait(container, spec) -> int:
        """Seconds to wait for a clean stop. A server that is still starting (a Forge or modpack
        install, say) has no world to save and nothing listening for the shutdown signal, so
        waiting the full timeout only makes the panel hang on "stopping"."""
        try:
            container.reload()
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
        except Exception:
            return spec.stop_timeout
        return min(spec.stop_timeout, 10) if health == "starting" else spec.stop_timeout

    # ----- console -----------------------------------------------------------------------

    def send_command(self, uuid: str, command: str) -> str | None:
        """Send a console command. Returns command output when the template uses exec mode."""
        spec = self.load_spec(uuid)
        container = self._container(uuid)
        if container is None or container.status != "running":
            raise RuntimeFailure("Server is not running.")
        command = command.replace("\r", "").replace("\n", " ").strip()
        if spec.console == "exec" and spec.console_exec:
            result = container.exec_run([*spec.console_exec, command], demux=False)
            return (result.output or b"").decode("utf-8", "replace")
        sock = self.docker.api.attach_socket(container.id, params={"stdin": 1, "stream": 1})
        try:
            raw = getattr(sock, "_sock", sock)
            raw.sendall((command + "\n").encode())
        finally:
            sock.close()
        return None

    def log_stream(self, uuid: str, tail: int | str = 200, since: float | None = None):
        container = self._container(uuid)
        if container is None:
            return None
        if since is not None:
            return container.logs(stream=True, follow=True, since=since)
        return container.logs(stream=True, follow=True, tail=tail)

    # ----- status ------------------------------------------------------------------------

    def status(self, uuid: str) -> dict[str, Any]:
        st = self._state(uuid)
        container = self._container(uuid)
        state = _status_word(container, st, self._sleeper(uuid) is not None)
        last = st.history[-1] if st.history else None
        started_at = None
        if container is not None and state in ("running", "starting"):
            started_at = (container.attrs.get("State") or {}).get("StartedAt")
        restarts = 0
        if container is not None:
            restarts = int(container.attrs.get("RestartCount") or 0)
            cstate = container.attrs.get("State") or {}
            exit_key = f"{getattr(container, 'id', '')}@{cstate.get('FinishedAt', '')}"
            if cstate.get("OOMKilled") and st.install == "ready" and not st.pending and st.oom_seen != exit_key:
                st.oom_seen = exit_key  # count each kill once, even after a reset
                if not st.oom_kills:
                    self.record_oom(uuid)  # killed before this agent was watching
        return {
            "uuid": uuid,
            "state": state,
            "error": st.error,
            "restarts": restarts,
            "oom_kills": st.oom_kills,
            "started_at": started_at,
            "cpu": last.cpu if last and state == "running" else 0.0,
            "memory": last.memory if last and state == "running" else 0,
            "disk": st.disk_bytes,
            "network": {"rx": st.network[0], "tx": st.network[1]},
            "history": [{"t": int(s.ts), "cpu": s.cpu, "mem": s.memory} for s in st.history],
        }

    def all_statuses(self) -> list[dict[str, Any]]:
        return [self.status(u) for u in self.known_servers()]

    # ----- sampler -----------------------------------------------------------------------

    def resume(self) -> None:
        """After an agent or host restart, start servers that were running before."""
        for uuid in self.known_servers():
            info = self._sleep_info(uuid)
            if info is not None:
                # The stand-in calls the agent by its container IP, which can change when the
                # agent restarts; replace it so joining still wakes the server.
                try:
                    self.sleep(uuid, info.get("motd") or "Sleeping - join to wake me up")
                except Exception as exc:
                    log.warning("couldn't put %s back to sleep: %s", uuid, exc)
                continue
            if not self._run_marker(uuid).exists():
                continue
            container = self._container(uuid)
            if container is not None and container.status not in ("running", "restarting"):
                try:
                    log.info("restoring %s", uuid)
                    container.start()
                except Exception as exc:
                    self._state(uuid).error = _docker_error_text(exc)

    def start_sampler(self) -> None:
        threading.Thread(target=self._sample_loop, name="hearth-sampler", daemon=True).start()
        threading.Thread(target=self._watch_oom, name="hearth-oom", daemon=True).start()

    def _watch_oom(self) -> None:
        """Docker kills a container that exceeds its memory limit with no message in its log; the
        server just vanishes and the restart policy brings it back, in a loop. Report it instead."""
        while not self._stop.is_set():
            try:
                events = self.docker.events(decode=True, filters={
                    "type": "container", "event": "oom", "label": f"{LABEL_MANAGED}=1"})
                for event in events:
                    if self._stop.is_set():
                        return
                    uuid = ((event.get("Actor") or {}).get("Attributes") or {}).get(LABEL_SERVER)
                    if uuid:
                        self.record_oom(uuid)
            except Exception as exc:
                log.warning("watching for out-of-memory events failed (%s); retrying", exc)
            self._stop.wait(5)

    def record_oom(self, uuid: str) -> None:
        st = self._state(uuid)
        container = self._container(uuid)
        if container is not None:
            cstate = container.attrs.get("State") or {}
            st.oom_seen = f"{getattr(container, 'id', '')}@{cstate.get('FinishedAt', '')}"
        st.oom_kills += 1
        try:
            limit = self.load_spec(uuid).memory_mb
        except ServerNotFound:
            return
        st.error = (
            f"Out of memory: Docker stopped the server at its {limit} MB limit"
            f" ({st.oom_kills} time{'s' if st.oom_kills != 1 else ''}). Give it more memory under"
            " Settings → Resources. Modpacks usually need 6–10 GB."
        )
        log.warning("%s was killed for exceeding %s MB", uuid, limit)

    def stop_sampler(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                list(self._pool.map(self._sample_one, self.known_servers()))
            except Exception:
                log.exception("sampler pass failed")
            self._stop.wait(max(self.config.sample_interval - (time.monotonic() - started), 1.0))

    def _sample_one(self, uuid: str) -> None:
        st = self._state(uuid)
        now = time.time()
        if now - st.disk_checked > 60:
            st.disk_bytes = directory_size(self.data_path(uuid))
            st.disk_checked = now
        container = self._container(uuid)
        if container is None or container.status != "running":
            st.history.append(Sample(now, 0.0, 0))
            exit_code = ((container.attrs.get("State") or {}).get("ExitCode") if container else None)
            if container is not None and container.status == "exited" and exit_code == 0 and not st.transition:
                self._want_running(uuid, False)  # stopped from inside (console `stop`)
            return
        stats = container.stats(stream=False)
        sample = Sample(now, cpu_percent_from_stats(stats), memory_bytes_from_stats(stats))
        st.history.append(sample)
        st.network = network_bytes_from_stats(stats)
        self._aggregate(st, sample)

    @staticmethod
    def _aggregate(st: ServerRuntimeState, sample: Sample) -> None:
        minute = int(sample.ts // 60) * 60
        if st.minute_key and minute != st.minute_key and st.minute_acc:
            n = len(st.minute_acc)
            st.minutes.append((st.minute_key, round(sum(c for c, _ in st.minute_acc) / n, 1),
                               int(max(m for _, m in st.minute_acc))))
            st.minute_acc = []
        st.minute_key = minute
        st.minute_acc.append((sample.cpu, sample.memory))

    def metrics(self, uuid: str) -> dict[str, Any]:
        self.load_spec(uuid)
        st = self._state(uuid)
        return {"minutes": [{"t": t, "cpu": c, "mem": m} for t, c, m in st.minutes],
                "recent": [{"t": int(s.ts), "cpu": s.cpu, "mem": s.memory} for s in st.history]}

    # ----- maintenance (restore, imports) -------------------------------------------------

    def is_running(self, uuid: str) -> bool:
        container = self._container(uuid)
        return bool(container and container.status in ("running", "restarting"))

    def busy(self, uuid: str) -> str | None:
        """A reason this server's files or container mustn't be touched right now, if any."""
        return "A restore or world import is running; try again when it finishes." \
            if self._state(uuid).transition == "restoring" else None

    def ensure_idle(self, uuid: str) -> None:
        reason = self.busy(uuid)
        if reason:
            raise RuntimeFailure(reason)

    def begin_restore(self, uuid: str) -> None:
        with self._registry_lock:
            st = self._states.get(uuid) or ServerRuntimeState()
            self._states.setdefault(uuid, st)
            if st.transition == "restoring":
                raise RuntimeFailure("A restore or world import is already running.")
            st.transition, st.error = "restoring", None

    def stop_for_maintenance(self, uuid: str) -> bool:
        """Stop the server (and any sleeper) for file surgery. Returns whether it should come back."""
        spec = self.load_spec(uuid)
        with self._lock(uuid):
            was_sleeping = self._sleeper(uuid) is not None
            self._remove_sleeper(uuid)
            container = self._container(uuid)
            if container is not None and container.status in ("running", "restarting"):
                container.stop(timeout=spec.stop_timeout)
                return True
        return was_sleeping  # it comes back up and goes to sleep again once it's empty

    def end_restore(self, uuid: str, error: str | None, start: bool) -> None:
        st = self._state(uuid)
        st.error = error
        st.transition = None
        if start:
            container = self._container(uuid)
            if container is not None:
                try:
                    container.start()
                    self._want_running(uuid, True)
                except Exception as exc:
                    st.error = _docker_error_text(exc)

    @contextlib.contextmanager
    def quiesced(self, uuid: str):
        """Ask a running server to flush and pause saving (e.g. save-off + save-all flush) while
        its files are read, so copies and archives aren't half-written."""
        spec = self.load_spec(uuid)
        running = self.is_running(uuid)
        if running:
            for cmd in spec.pre_backup:
                self._try_command(uuid, cmd)
            if spec.pre_backup:
                time.sleep(2)  # let the flush land on disk
        try:
            yield running
        finally:
            if running:
                for cmd in spec.post_backup:
                    self._try_command(uuid, cmd)

    def _try_command(self, uuid: str, command: str) -> None:
        try:
            self.send_command(uuid, command)
        except Exception as exc:
            log.warning("save hook %r on %s failed: %s", command, uuid, exc)

    # ----- wake-on-join ------------------------------------------------------------------

    def _sleep_marker(self, uuid: str) -> Path:
        return self.config.state_dir / f"{uuid}.sleeping"

    def _sleep_info(self, uuid: str) -> dict[str, str] | None:
        marker = self._sleep_marker(uuid)
        try:
            text = marker.read_text().strip()
        except OSError:
            return None
        try:
            info = json.loads(text)
        except ValueError:
            info = {"key": text}  # written by an older agent
        return info if isinstance(info, dict) else None

    def _sleeper(self, uuid: str) -> Any | None:
        from docker.errors import NotFound

        try:
            return self.docker.containers.get(f"hearth-sleep-{uuid}")
        except NotFound:
            return None

    def _remove_sleeper(self, uuid: str) -> None:
        sleeper = self._sleeper(uuid)
        if sleeper is not None:
            sleeper.remove(force=True)
        self._sleep_marker(uuid).unlink(missing_ok=True)

    def sleep(self, uuid: str, motd: str) -> None:
        """Stop the server and leave a tiny stand-in on its port that wakes it when someone joins.

        The stand-in runs from the agent's own image and calls the agent back over the Docker
        network with a one-off key, so it never holds the agent token.
        """
        spec = self.load_spec(uuid)
        self.ensure_idle(uuid)
        tcp = [p for p in spec.ports if p.protocol == "tcp"]
        me = inspect_self(self.docker)
        image = os.environ.get("HEARTH_AGENT_IMAGE") or (me.get("Config") or {}).get("Image")
        networks = (me.get("NetworkSettings") or {}).get("Networks") or {}
        if not tcp:
            raise RuntimeFailure("Wake-on-join needs a TCP game port.")
        if not image or not networks:
            raise RuntimeFailure("Wake-on-join needs the agent itself to run in Docker.")
        net_name, net = next(iter(networks.items()))
        key = secrets.token_urlsafe(24)
        port = int(os.environ.get("HEARTH_AGENT_PORT", "8080"))
        scheme = "https" if os.environ.get("HEARTH_TLS_CERT") else "http"
        with self._lock(uuid):
            container = self._container(uuid)
            if container is not None and container.status in ("running", "restarting"):
                st = self._state(uuid)
                st.transition = "stopping"
                try:
                    container.stop(timeout=spec.stop_timeout)
                finally:
                    _end_transition(st, "stopping")
            self._want_running(uuid, False)
            self._remove_sleeper(uuid)
            game = tcp[0]
            self.docker.containers.run(
                image, ["python", "-m", "hearth_agent.sleeper"], name=f"hearth-sleep-{uuid}", detach=True,
                environment={"LISTEN_PORT": str(game.container_port), "MOTD": motd,
                             "WAKE_URL": f"{scheme}://{net.get('IPAddress')}:{port}/api/wake/{uuid}?key={key}"},
                ports={f"{game.container_port}/tcp": (game.host_ip, game.host_port)}, network=net_name,
                labels={LABEL_MANAGED: "1", LABEL_SERVER: uuid, "hearth.role": "sleeper"},
                restart_policy={"Name": "unless-stopped"}, mem_limit="64m",
            )
            self._sleep_marker(uuid).write_text(json.dumps({"key": key, "motd": motd}))

    def wake(self, uuid: str, key: str | None = None) -> None:
        """Called by the stand-in (with its key) or by a start action: swap it for the server."""
        if key is not None:
            expected = (self._sleep_info(uuid) or {}).get("key") or ""
            if not expected or not hmac.compare_digest(expected, key):
                raise RuntimeFailure("Invalid wake key.")
        self.ensure_idle(uuid)
        with self._lock(uuid):
            self._remove_sleeper(uuid)
            container = self._container(uuid)
            if container is not None and container.status != "running":
                st = self._state(uuid)
                st.transition = "starting"
                try:
                    container.start()
                finally:
                    _end_transition(st, "starting")
            self._want_running(uuid, True)

    # ----- cloning -----------------------------------------------------------------------

    def copy_from(self, uuid: str, source: str) -> None:
        """Copy another server's files into this (stopped) server's folder."""
        self.load_spec(uuid)
        self.load_spec(source)
        self.ensure_idle(uuid)
        self.ensure_idle(source)
        container = self._container(uuid)
        if container is not None and container.status == "running":
            raise RuntimeFailure("Stop the server before copying files into it.")
        from .files import copy_tree

        with self._lock(uuid), self.quiesced(source):
            copy_tree(self.data_path(source), self.data_path(uuid))

    # ----- playit.gg tunnel --------------------------------------------------------------

    TUNNEL_NAME = "hearth-playit"

    def tunnel_status(self) -> dict[str, Any]:
        from docker.errors import NotFound

        try:
            c = self.docker.containers.get(self.TUNNEL_NAME)
        except NotFound:
            return {"configured": False, "state": None, "log": ""}
        try:
            text = c.logs(tail=40).decode("utf-8", "replace")
        except Exception:
            text = ""
        return {"configured": True, "state": c.status, "log": text}

    def tunnel_set(self, secret: str) -> None:
        image = os.environ.get("HEARTH_PLAYIT_IMAGE", "ghcr.io/playit-cloud/playit-agent:latest")
        self.tunnel_clear()
        self._pull(image)
        # Host networking lets the tunnel reach every game port published on this machine.
        self.docker.containers.run(
            image, name=self.TUNNEL_NAME, detach=True, network_mode="host",
            environment={"SECRET_KEY": secret}, restart_policy={"Name": "unless-stopped"},
            labels={LABEL_MANAGED: "1", "hearth.role": "tunnel"},
        )

    def tunnel_clear(self) -> None:
        from docker.errors import NotFound

        try:
            self.docker.containers.get(self.TUNNEL_NAME).remove(force=True)
        except NotFound:
            pass

    # ----- system ------------------------------------------------------------------------

    def system_info(self) -> dict[str, Any]:
        info = self.docker.info()
        disk = shutil.disk_usage(self.config.data_dir)
        return {
            "docker_version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
            "architecture": info.get("Architecture"),
            "cpus": info.get("NCPU"),
            "memory_total": info.get("MemTotal"),
            "disk_total": disk.total,
            "disk_free": disk.free,
            "containers_running": info.get("ContainersRunning"),
            "servers": len(self.known_servers()),
            "storage_path": str(self.host_data_dir),
        }


def _end_transition(st: ServerRuntimeState, mine: str | None) -> None:
    """Clear the transition only if it's still the one this action set. A restore can begin
    while a power action is finishing; that action must not wipe out "restoring"."""
    if mine is not None and st.transition == mine:
        st.transition = None


def _status_word(container: Any | None, st: ServerRuntimeState, sleeping: bool = False) -> str:
    if st.install == "installing" or st.pending:
        return "installing"
    if st.transition == "restoring":
        return "restoring"
    if sleeping and (container is None or container.status != "running"):
        return "sleeping"
    if container is None:
        return "install_failed" if st.install == "failed" else "missing"
    if st.transition == "stopping":
        return "stopping"
    status = container.status
    if status == "running":
        health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
        return "starting" if health == "starting" else "running"
    if status == "restarting" or st.transition == "starting":
        return "starting"
    return "offline"


def _docker_error_text(exc: Exception) -> str:
    explanation = getattr(exc, "explanation", None)
    text = explanation if isinstance(explanation, str) and explanation else str(exc)
    if "port is already allocated" in text or "address already in use" in text:
        return "A port assigned to this server is already in use on the node. " + text
    return text[:500]
