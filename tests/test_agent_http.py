"""Drive the real agent HTTP API against the simulated Docker client."""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from fake_docker import FakeDocker  # noqa: E402
from hearth_agent.app import create_app  # noqa: E402
from hearth_agent.config import AgentConfig  # noqa: E402

TOKEN = "a" * 40
UUID = "0b9f6a2e-3c1d-4e5f-8a7b-6c5d4e3f2a1b"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPEC = {"uuid": UUID, "image": "itzg/minecraft-server:java21", "memory_mb": 2048, "env": {"TYPE": "PAPER"},
        "ports": [{"host_port": 25565, "container_port": 25565}], "console": "exec", "console_exec": ["rcon-cli"],
        "stop_timeout": 5}


@pytest.fixture
def agent(tmp_path):
    config = AgentConfig(token=TOKEN, data_dir=tmp_path / "servers", state_dir=tmp_path / "state",
                         sample_interval=1, history_size=10)
    with TestClient(create_app(config, docker_factory=FakeDocker)) as client:
        yield client


def wait_for(client, state, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = client.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"]
        if current == state:
            return
        time.sleep(0.2)
    raise AssertionError(f"never reached {state}, last {current}")


def test_rejects_bad_token(agent):
    assert agent.get("/api/servers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert agent.get("/api/health").status_code == 200


def test_lifecycle(agent):
    assert agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH).status_code == 202
    wait_for(agent, "offline")
    assert agent.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH).status_code == 202
    wait_for(agent, "running")
    out = agent.post(f"/api/servers/{UUID}/command", json={"command": "list"}, headers=AUTH).json()["output"]
    assert "players online" in out

    with agent.websocket_connect(f"/api/servers/{UUID}/console", headers=AUTH) as ws:
        kinds = {ws.receive_json()["type"] for _ in range(3)}
        assert kinds & {"log", "status"}

    # Applying a new spec while running recreates the container and keeps it running.
    assert agent.put(f"/api/servers/{UUID}", json={**SPEC, "memory_mb": 4096}, headers=AUTH).status_code == 202
    wait_for(agent, "running")

    agent.post(f"/api/servers/{UUID}/power", json={"action": "stop"}, headers=AUTH)
    wait_for(agent, "offline")
    assert agent.post(f"/api/servers/{UUID}/command", json={"command": "list"}, headers=AUTH).status_code == 409
    assert agent.delete(f"/api/servers/{UUID}", headers=AUTH).status_code == 204
    assert agent.get(f"/api/servers/{UUID}", headers=AUTH).status_code == 404


def test_console_requires_token(agent):
    agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with agent.websocket_connect(f"/api/servers/{UUID}/console") as ws:
            ws.receive_json()


def test_uuid_mismatch(agent):
    other = "11111111-2222-4333-8444-555555555555"
    assert agent.put(f"/api/servers/{other}", json=SPEC, headers=AUTH).status_code == 400


def _app(tmp_path, docker):
    config = AgentConfig(token=TOKEN, data_dir=tmp_path / "servers", state_dir=tmp_path / "state",
                         sample_interval=1, history_size=10)
    return TestClient(create_app(config, docker_factory=lambda: docker))


def test_kill_interrupts_a_slow_stop(tmp_path):
    docker = FakeDocker()
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH)
        wait_for(agent, "offline")
        agent.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH)
        wait_for(agent, "running")
        agent.post(f"/api/servers/{UUID}/power", json={"action": "stop"}, headers=AUTH)
        started = time.time()
        agent.post(f"/api/servers/{UUID}/power", json={"action": "kill"}, headers=AUTH)
        container = docker.containers.get(f"hearth-{UUID}")
        while container.status != "exited" and time.time() - started < 5:
            time.sleep(0.05)
        assert time.time() - started < 0.8  # did not wait for the 1.2 s graceful stop


def test_running_servers_are_restored_after_agent_restart(tmp_path):
    docker = FakeDocker()
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH, params={"start": "true"})
        wait_for(agent, "running")
    docker.containers.get(f"hearth-{UUID}").status = "exited"  # host rebooted
    with _app(tmp_path, docker) as agent:
        wait_for(agent, "running")


def test_server_stopped_by_user_stays_stopped_after_restart(tmp_path):
    docker = FakeDocker()
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH, params={"start": "true"})
        wait_for(agent, "running")
        agent.post(f"/api/servers/{UUID}/power", json={"action": "stop"}, headers=AUTH)
        wait_for(agent, "offline")
    with _app(tmp_path, docker) as agent:
        time.sleep(1)
        assert agent.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"] == "offline"


def test_newest_spec_wins_when_updates_race(tmp_path):
    docker = FakeDocker()
    with _app(tmp_path, docker) as agent:
        for memory in (1024, 2048, 3072, 4096):
            agent.put(f"/api/servers/{UUID}", json={**SPEC, "memory_mb": memory}, headers=AUTH)
        wait_for(agent, "offline", timeout=15)
        assert docker.containers.get(f"hearth-{UUID}").kwargs["mem_limit"] == "4096m"


def test_failed_pull_is_reported(tmp_path):
    from docker.errors import ImageNotFound

    docker = FakeDocker()

    def refuse(image):
        raise ImageNotFound(f"pull access denied for {image}")

    docker.images.pull = refuse
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json={**SPEC, "image": "typo/does-not-exist"}, headers=AUTH)
        wait_for(agent, "install_failed")
        status = agent.get(f"/api/servers/{UUID}", headers=AUTH).json()
        assert "pull access denied" in status["error"]
        r = agent.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH)
        assert r.status_code == 409 and "Reinstall" in r.json()["detail"]


def test_game_containers_mount_the_host_path_of_the_chosen_drive(tmp_path):
    import socket

    docker = FakeDocker()

    class Me:  # the agent's own container, as Docker Desktop reports a D:\Hearth bind mount
        status = "running"
        attrs = {"Mounts": [{"Type": "bind", "Source": "/run/desktop/mnt/host/d/Hearth",
                             "Destination": str(tmp_path)}]}

    docker._containers[socket.gethostname()] = Me()
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH)
        wait_for(agent, "offline")
        volumes = docker.containers.get(f"hearth-{UUID}").kwargs["volumes"]
        assert list(volumes) == [f"/run/desktop/mnt/host/d/Hearth/servers/{UUID}"]
        assert (tmp_path / "servers" / UUID).is_dir()  # created through the agent's own view
        assert agent.get("/api/system", headers=AUTH).json()["storage_path"] == "/run/desktop/mnt/host/d/Hearth/servers"


def test_out_of_memory_kill_is_reported_clearly(tmp_path):
    docker = FakeDocker()
    with _app(tmp_path, docker) as agent:
        agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH, params={"start": "true"})
        wait_for(agent, "running")
        docker.simulate_oom(f"hearth-{UUID}")
        deadline = time.time() + 5
        while time.time() < deadline:
            status = agent.get(f"/api/servers/{UUID}", headers=AUTH).json()
            if status["oom_kills"]:
                break
            time.sleep(0.1)
        assert status["oom_kills"] == 1
        assert "Out of memory" in status["error"] and "2048 MB" in status["error"]
        # Giving it more memory (a new spec) clears the report.
        agent.put(f"/api/servers/{UUID}", json={**SPEC, "memory_mb": 4096}, headers=AUTH)
        wait_for(agent, "running")
        assert agent.get(f"/api/servers/{UUID}", headers=AUTH).json()["error"] is None


def test_console_history_arrives_in_one_message(agent):
    """Opening (or refreshing) the console sends the recent history as one message, not a line
    at a time, so the browser draws it in a single update."""
    agent.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH)
    wait_for(agent, "offline")
    agent.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH)
    wait_for(agent, "running")
    container = agent.app.state.manager.docker.containers.get(f"hearth-{UUID}")
    for i in range(300):
        container._log(f"[Server thread/INFO]: line {i}")

    with agent.websocket_connect(f"/api/servers/{UUID}/console", headers=AUTH) as ws:
        first_log = next(m for m in (ws.receive_json() for _ in range(5)) if m["type"] == "log")
    assert "line 0\r\n" in first_log["data"] and "line 299\r\n" in first_log["data"]


# ----- junk in, never a 500 ------------------------------------------------------------------------

JUNK_PATHS = ["", "/", "../../etc/passwd", "..%2f..%2fetc", "a/../../b", "%00", "x" * 3000, "con", "a\\b", "//", "."]


def test_agent_routes_never_500_on_junk(agent):
    import re

    assert agent.put(f"/api/servers/{UUID}", headers=AUTH, json=SPEC).status_code in (200, 201, 202, 204)
    bad = []
    spec = agent.app.openapi()
    for path, ops in spec["paths"].items():
        if "console" in path and "websocket" in path:
            continue
        for method in ops:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            for junk in JUNK_PATHS:
                url = re.sub(r"\{uuid\}", UUID, path)
                url = re.sub(r"\{[^}]+\}", "zz", url)
                kwargs = {"headers": AUTH}
                if method != "get":
                    kwargs["json"] = {"path": junk, "name": junk, "target": junk, "to": junk, "content": junk, "command": junk, "url": junk}
                if method == "get":
                    kwargs["params"] = {"path": junk, "name": junk, "since": junk, "tail": junk}
                if path.endswith("/power") or path.endswith("/delete") or "/api/servers/{uuid}" == path and method == "delete":
                    continue
                if method == "delete":
                    kwargs["params"] = kwargs.pop("json")
                r = getattr(agent, method)(url, **kwargs)
                if r.status_code >= 500:
                    bad.append((method, path, junk[:20], r.status_code, r.text[:100]))
    assert not bad, "\n".join(map(str, bad[:12]))


def test_agent_routes_never_500_on_odd_bodies(agent):
    import re

    agent.put(f"/api/servers/{UUID}", headers=AUTH, json=SPEC)
    bad = []
    for path, ops in agent.app.openapi()["paths"].items():
        if path.endswith("/power") or "console" in path:
            continue
        for method in ("post", "put", "patch"):
            if method not in ops:
                continue
            for body in (None, [], "x", 5, {"path": 5}, {"path": None, "name": None}, {"content": 10**20, "size": -1},
                         {"env": {"A": None}, "memory_mb": "lots"}, {"ports": [{"host_port": 0}]}, {"archive": "../x", "target": "/"}):
                url = re.sub(r"\{[^}]+\}", UUID if "uuid" in path else "zz", path)
                kwargs = {"headers": AUTH}
                if body is not None:
                    kwargs["json"] = body
                if path == "/api/servers/{uuid}" and method == "put":
                    continue  # replaces the server the other requests use
                r = getattr(agent, method)(url, **kwargs)
                if r.status_code >= 500:
                    bad.append((method, path, str(body)[:40], r.status_code, r.text[:80]))
    assert not bad, "\n".join(map(str, bad[:12]))
