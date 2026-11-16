"""An in-memory stand-in for the docker-py client, for demos and tests without a Docker daemon.

It simulates containers that "boot" a Minecraft-like server log, report plausible stats, and
accept console commands. Nothing here is real data.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from datetime import datetime, timezone

from docker.errors import ImageNotFound, NotFound


class _Exec:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.exit_code = 0


class FakeContainer:
    _ids = itertools.count(1)

    def __init__(self, client: "FakeDocker", **kwargs) -> None:
        self.client = client
        self.kwargs = kwargs
        self.name = kwargs["name"]
        self.id = f"{next(self._ids):064x}"
        self.status = "created"
        self.started_at = "0001-01-01T00:00:00Z"
        self.lines: list[tuple[float, bytes]] = []
        self.exit_code = 0
        self.oom_killed = False
        self.restart_count = 0
        self.cond = threading.Condition()
        self.cpu_total = 0
        self.sys_total = 0
        self.health = None

    @property
    def attrs(self):
        state = {"StartedAt": self.started_at, "ExitCode": self.exit_code, "OOMKilled": self.oom_killed}
        if self.health:
            state["Health"] = {"Status": self.health}
        return {"State": state, "RestartCount": self.restart_count}

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.cond:
            self.lines.append((time.time(), f"[{stamp}] {text}\r\n".encode()))
            self.cond.notify_all()

    def _boot(self) -> None:
        env = self.kwargs.get("environment", {})
        steps = [
            "\x1b[33m[init]\x1b[0m Running as uid=1000 gid=1000",
            f"\x1b[33m[init]\x1b[0m Resolving type given {env.get('TYPE', 'VANILLA')}",
            f"\x1b[33m[init]\x1b[0m Setting initial memory to {env.get('MEMORY', '1G')}",
            "[Server thread/INFO]: Starting minecraft server version 1.21.1",
            "[Server thread/INFO]: Loading properties",
            f"[Server thread/INFO]: Starting Minecraft server on *:{next(iter(self.kwargs['ports'])).split('/')[0]}",
            "[Server thread/INFO]: Preparing level \"world\"",
            "[Worker-Main-2/INFO]: Preparing spawn area: 46%",
            "[Worker-Main-2/INFO]: Preparing spawn area: 100%",
            "[Server thread/INFO]: \x1b[32mDone (6.214s)! For help, type \"help\"\x1b[0m",
        ]
        self.health = "starting"
        for line in steps:
            if self.status != "running":
                return
            time.sleep(0.35)
            self._log(line)
        self.health = "healthy"
        chatter = ["Player joined the game", "Saving the game (this may take a moment!)", "Saved the game",
                   "Can't keep up! Is the server overloaded? Running 2041ms or 40 ticks behind"]
        while self.status == "running":
            time.sleep(random.uniform(6, 14))
            if self.status == "running":
                self._log(f"[Server thread/INFO]: {random.choice(chatter)}")

    def start(self) -> None:
        if self.status == "running":
            return
        self.status = "running"
        self.started_at = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=self._boot, daemon=True).start()

    def stop(self, timeout: int = 10) -> None:
        if self.status != "running":
            return
        self._log("[Server thread/INFO]: Stopping the server")
        time.sleep(1.2)
        self._log("[Server thread/INFO]: ThreadedAnvilChunkStorage: All dimensions are saved")
        self.status = "exited"
        self.exit_code = 0
        with self.cond:
            self.cond.notify_all()

    def restart(self, timeout: int = 10) -> None:
        self.stop(timeout)
        self.start()

    def kill(self) -> None:
        self.status = "exited"
        self.exit_code = 137
        with self.cond:
            self.cond.notify_all()

    def remove(self, force: bool = False) -> None:
        self.status = "removed"
        self.client._containers.pop(self.name, None)

    def stats(self, stream: bool = False):
        pre_cpu, pre_sys = self.cpu_total, self.sys_total
        self.cpu_total += random.randint(2_000_000, 40_000_000)
        self.sys_total += 1_000_000_000
        limit = int(self.kwargs["mem_limit"].rstrip("m")) * 1024 * 1024
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": self.cpu_total}, "system_cpu_usage": self.sys_total, "online_cpus": 8},
            "precpu_stats": {"cpu_usage": {"total_usage": pre_cpu}, "system_cpu_usage": pre_sys},
            "memory_stats": {"usage": int(limit * random.uniform(0.45, 0.62)), "stats": {"inactive_file": 0}},
            "networks": {"eth0": {"rx_bytes": random.randint(10**6, 10**8), "tx_bytes": random.randint(10**6, 10**8)}},
        }

    def logs(self, stream=True, follow=True, tail=200, since=None):
        def gen():
            with self.cond:
                if since is not None:
                    index = next((i for i, (ts, _) in enumerate(self.lines) if ts >= since), len(self.lines))
                else:
                    index = max(0, len(self.lines) - tail) if tail else len(self.lines)
            while True:
                with self.cond:
                    while index >= len(self.lines) and self.status == "running":
                        self.cond.wait(timeout=1)
                    chunk = self.lines[index:]
                    index = len(self.lines)
                for _, line in chunk:
                    yield line
                if self.status != "running" and index >= len(self.lines):
                    return
        return _Closable(gen())

    def exec_run(self, cmd, demux=False):
        command = cmd[-1]
        self._log("[RCON Listener #1/INFO]: Thread RCON Client /127.0.0.1 started")
        if command == "list":
            return _Exec(b"There are 2 of a max of 20 players online: alex, sam\n")
        if command.startswith("say "):
            self._log(f"[Server thread/INFO]: [Rcon] {command[4:]}")
            return _Exec(b"")
        return _Exec(f"Unknown or incomplete command: {command}\n".encode())


class _Closable:
    def __init__(self, gen) -> None:
        self.gen = gen

    def __iter__(self):
        return self.gen

    def close(self) -> None:
        self.gen.close()


class _Containers:
    def __init__(self, client: "FakeDocker") -> None:
        self.client = client

    def get(self, name):
        try:
            return self.client._containers[name]
        except KeyError:
            raise NotFound(name)

    def create(self, **kwargs):
        time.sleep(0.6)
        c = FakeContainer(self.client, **kwargs)
        self.client._containers[c.name] = c
        return c

    def run(self, image, command=None, **kwargs):
        kwargs.setdefault("ports", {"25565/tcp": None})
        kwargs.setdefault("mem_limit", "64m")
        c = FakeContainer(self.client, image=image, command=command, **kwargs)
        c.status = "running"  # helpers (sleeper, tunnel) just run; no game boot log
        self.client._containers[c.name] = c
        return c


class _Images:
    def __init__(self) -> None:
        self.local: set[str] = set()

    def get(self, image):
        if image not in self.local:
            raise ImageNotFound(image)
        return image

    def pull(self, image):
        time.sleep(1.5)
        self.local.add(image)


class FakeDocker:
    def __init__(self) -> None:
        self._containers: dict[str, FakeContainer] = {}
        self.containers = _Containers(self)
        self.images = _Images()
        self.api = self
        import queue
        self.event_queue: "queue.Queue[dict]" = queue.Queue()

    def events(self, decode=True, filters=None):
        """Blocking stream of events pushed onto event_queue (e.g. simulated OOM kills)."""
        while True:
            yield self.event_queue.get()

    def simulate_oom(self, container_name: str) -> None:
        c = self._containers[container_name]
        c.oom_killed = True
        c.exit_code = 137
        self.event_queue.put({"Type": "container", "Action": "oom",
                              "Actor": {"Attributes": {"hearth.server": c.kwargs["labels"]["hearth.server"]}}})

    def info(self):
        return {"ServerVersion": "27.3.1 (simulated)", "OperatingSystem": "Demo Linux", "Architecture": "x86_64",
                "NCPU": 8, "MemTotal": 32 * 1024**3,
                "ContainersRunning": sum(c.status == "running" for c in self._containers.values())}

    def attach_socket(self, container_id, params=None):
        raise RuntimeError("stdin console not simulated")
