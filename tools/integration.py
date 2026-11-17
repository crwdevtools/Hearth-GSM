"""End-to-end test against real Docker: the actual panel and agent containers, a real Minecraft server.

    python tools/integration.py            # needs Docker with Compose v2; takes a few minutes

It brings the stack up with docker compose (in an isolated compose project and data folder),
creates the first admin, creates a small Paper server, waits for it to finish starting, runs a
console command, makes a backup, changes the world, restores the backup and checks the change is
gone, then deletes the server and tears everything down. Exit code 0 means every step passed.
Used by .github/workflows/integration.yml.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "hearth-integration"
PORT = int(os.environ.get("HEARTH_IT_PORT", "18000"))
BASE = f"http://localhost:{PORT}"
MC_VERSION = os.environ.get("HEARTH_IT_MC_VERSION", "1.21.1")


def step(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def compose(*args: str, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-p", PROJECT, *args], cwd=ROOT, env=env, check=check,
                          text=True)


class Panel:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict | list]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + "/api" + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"detail": raw.decode(errors="replace")}

    def ok(self, method: str, path: str, body: dict | None = None, timeout: float = 60):
        status, data = self.call(method, path, body, timeout)
        if status >= 400:
            raise AssertionError(f"{method} {path} → {status}: {data}")
        return data


def wait_for(what: str, check, timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (OSError, AssertionError) as exc:
            last = exc
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last})")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="hearth-it-"))
    env_file = ROOT / ".env"
    backup_env = env_file.read_bytes() if env_file.exists() else None
    env = {**os.environ, "HEARTH_AGENT_TOKEN": secrets.token_hex(32), "HEARTH_PUBLIC_HOST": "localhost",
           "HEARTH_DATA_DIR": str(data_dir), "HEARTH_PANEL_PORT": str(PORT),
           "HEARTH_BREACHED_PASSWORD_CHECK": "false"}
    # Compose reads .env too; give it this run's settings so a developer's own .env isn't used.
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items() if k.startswith("HEARTH_")))
    panel = Panel()
    failed = False
    try:
        step("Building and starting the panel and agent")
        compose("up", "-d", "--build", env=env)
        wait_for("the panel", lambda: panel.call("GET", "/auth/state")[0] == 200, 180)

        step("Creating the first admin (from this machine)")
        panel.ok("POST", "/auth/setup", {"username": "admin", "password": "integration-pass-123"})
        nodes = wait_for("the local node", lambda: [n for n in panel.ok("GET", "/nodes") if n["online"]], 120)
        node = nodes[0]
        print(f"   node {node['name']}: Docker {node['system']['docker_version']}")

        step(f"Creating a Paper {MC_VERSION} server")
        server = panel.ok("POST", "/servers", {
            "name": "Integration", "node_id": node["id"], "template": "minecraft-java", "memory_mb": 2048,
            "accept_eula": True, "variables": {"TYPE": "PAPER", "VERSION": MC_VERSION, "LEVEL_TYPE": "minecraft:flat"},
        })
        sid = server["id"]

        step("Waiting for it to finish starting (downloads Paper the first time)")
        wait_for("the server to run", lambda: panel.ok("GET", f"/servers/{sid}")["status"]["state"] == "running", 600, 5)
        wait_for("Paper to finish loading", lambda: "There are" in (panel.ok(
            "POST", f"/servers/{sid}/command", {"command": "list"}).get("output") or ""), 300, 5)
        print("   console replied to `list`")

        step("Backing up")
        backup = panel.ok("POST", f"/servers/{sid}/backups", {"name": "integration"})
        wait_for("the backup", lambda: next(b for b in panel.ok("GET", f"/servers/{sid}/backups")
                                            if b["id"] == backup["id"])["state"] == "ready", 300)

        step("Changing a file, then restoring the backup")
        panel.ok("PUT", f"/servers/{sid}/files/content", {"path": "/integration.txt", "content": "after backup"})
        panel.ok("POST", f"/servers/{sid}/backups/{backup['id']}/restore", {"backup_first": False})
        wait_for("the restore", lambda: panel.ok("GET", f"/servers/{sid}")["status"]["state"] in ("running", "starting"), 300)
        status, _ = panel.call("GET", f"/servers/{sid}/files/content?path=/integration.txt")
        assert status == 404, "the file written after the backup should be gone after restoring"
        print("   restore put the server back as it was")

        step("Deleting the server")
        panel.ok("DELETE", f"/servers/{sid}")
        assert not (data_dir / "servers" / server["uuid"]).exists(), "server files should be removed"
        print("\nAll integration steps passed.")
    except Exception as exc:  # report, dump logs, and fail
        failed = True
        print(f"\nFAILED: {exc}", file=sys.stderr)
        compose("logs", "--tail", "200", env=env, check=False)
    finally:
        step("Tearing down")
        compose("down", "-v", env=env, check=False)
        for cid in subprocess.run(["docker", "ps", "-aq", "--filter", "label=hearth.managed=1"],
                                  capture_output=True, text=True, check=False).stdout.split():
            subprocess.run(["docker", "rm", "-f", cid], check=False, capture_output=True)
        if backup_env is None:
            env_file.unlink(missing_ok=True)
        else:
            env_file.write_bytes(backup_env)
        shutil.rmtree(data_dir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
