"""Panel features: sharing, quotas, 2FA, API tokens, backups, schedules, players, mods, events,
suspension, cloning, joinability, auto-sleep."""

import time
from datetime import datetime, timezone

import pytest

from hearth_panel.security import totp_now


def _create(client, node, **extra):
    body = {"name": "Survival", "node_id": node["id"], "template": "minecraft-java", "memory_mb": 4096,
            "accept_eula": True, "variables": {"TYPE": "PAPER", "VERSION": "1.21.4"}}
    body.update(extra)
    r = client.post("/api/servers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, username, password, code=None):
    client.post("/api/auth/logout")
    body = {"username": username, "password": password}
    if code:
        body["totp_code"] = code
    return client.post("/api/auth/login", json=body)


def _user(client, name, **extra):
    r = client.post("/api/users", json={"username": name, "password": f"{name}-password-1", **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ----- sharing -----------------------------------------------------------------------------------


def test_share_grants_only_chosen_permissions(admin, node, agent):
    sid = _create(admin, node)["id"]
    _user(admin, "friend")
    r = admin.post(f"/api/servers/{sid}/shares", json={"username": "friend", "permissions": ["console", "power"]})
    assert r.status_code == 201
    _login(admin, "friend", "friend-password-1")
    servers = admin.get("/api/servers").json()
    assert [s["id"] for s in servers] == [sid]
    assert set(servers[0]["permissions"]) == {"console", "power"}
    assert admin.post(f"/api/servers/{sid}/command", json={"command": "say hi"}).status_code == 200
    assert admin.post(f"/api/servers/{sid}/power", json={"action": "restart"}).status_code == 202
    assert admin.get(f"/api/servers/{sid}/files").status_code == 403
    assert admin.get(f"/api/servers/{sid}/backups").status_code == 403
    assert admin.patch(f"/api/servers/{sid}", json={"name": "mine"}).status_code == 403
    assert admin.delete(f"/api/servers/{sid}").status_code == 403
    assert admin.get(f"/api/servers/{sid}/shares").status_code == 403


def test_share_validation(admin, node):
    sid = _create(admin, node)["id"]
    assert admin.post(f"/api/servers/{sid}/shares", json={"username": "ghost"}).status_code == 404
    _user(admin, "friend")
    assert admin.post(f"/api/servers/{sid}/shares", json={"username": "friend", "permissions": ["root"]}).status_code == 422
    share = admin.post(f"/api/servers/{sid}/shares", json={"username": "friend"}).json()
    assert admin.post(f"/api/servers/{sid}/shares", json={"username": "friend"}).status_code == 409
    r = admin.put(f"/api/servers/{sid}/shares/{share['id']}", json={"permissions": ["files"]})
    assert r.json()["permissions"] == ["files"]
    assert admin.delete(f"/api/servers/{sid}/shares/{share['id']}").status_code == 204


# ----- quotas ------------------------------------------------------------------------------------


def test_quota_lets_friends_create_within_limits(admin, node):
    _user(admin, "builder", max_servers=1, max_memory_mb=6144)
    _login(admin, "builder", "builder-password-1")
    mine = _create(admin, node, memory_mb=4096)
    assert mine["owner"]["username"] == "builder"
    r = admin.post("/api/servers", json={"name": "x", "node_id": node["id"], "template": "minecraft-java",
                                         "memory_mb": 2048, "accept_eula": True})
    assert r.status_code == 403 and "quota allows 1" in r.json()["detail"]
    assert admin.patch(f"/api/servers/{mine['id']}", json={"memory_mb": 6144}).status_code == 200
    r = admin.patch(f"/api/servers/{mine['id']}", json={"memory_mb": 8192})
    assert r.status_code == 403 and "memory left" in r.json()["detail"]


def test_no_quota_no_create(admin, node):
    _user(admin, "friend")
    _login(admin, "friend", "friend-password-1")
    r = admin.post("/api/servers", json={"name": "x", "node_id": node["id"], "template": "minecraft-java",
                                         "memory_mb": 2048, "accept_eula": True})
    assert r.status_code == 403


# ----- 2FA and API tokens ------------------------------------------------------------------------


def _fresh_totp_window():
    """These tests compare codes across 30-second steps; don't start one just before a boundary."""
    if time.time() % 30 > 24:
        time.sleep(30 - time.time() % 30 + 0.2)


def test_two_factor_login(admin):
    _fresh_totp_window()
    setup = admin.post("/api/account/2fa/setup").json()
    assert setup["uri"].startswith("otpauth://totp/Hearth:admin") and "<svg" in setup["qr_svg"]
    assert admin.post("/api/account/2fa/enable", json={"code": "000000"}).status_code == 400
    assert admin.post("/api/account/2fa/enable", json={"code": totp_now(setup["secret"])}).status_code == 200

    r = _login(admin, "admin", "correct-horse-battery")
    assert r.status_code == 401 and r.json()["totp_required"] is True
    assert admin.get("/api/servers").status_code == 401
    assert _login(admin, "admin", "correct-horse-battery", "123456").status_code == 401
    # The code that turned 2FA on was already used once; it can't sign anyone in.
    assert _login(admin, "admin", "correct-horse-battery", totp_now(setup["secret"])).status_code == 401
    assert _login(admin, "admin", "correct-horse-battery", totp_now(setup["secret"], time.time() + 90)).status_code == 401
    assert _login(admin, "admin", "correct-horse-battery", totp_now(setup["secret"], time.time() + 30)).status_code == 200
    assert admin.get("/api/servers").status_code == 200

    r = admin.post("/api/account/2fa/disable", json={"password": "correct-horse-battery",
                                                     "code": totp_now(setup["secret"])})
    assert r.json()["enabled"] is False


def test_two_factor_code_cannot_be_replayed(admin):
    _fresh_totp_window()
    _user(admin, "friend")
    _login(admin, "friend", "friend-password-1")
    secret = admin.post("/api/account/2fa/setup").json()["secret"]
    admin.post("/api/account/2fa/enable", json={"code": totp_now(secret, time.time() - 30)})
    code = totp_now(secret)
    assert _login(admin, "friend", "friend-password-1", code).status_code == 200
    assert _login(admin, "friend", "friend-password-1", code).status_code == 401  # someone who saw it


def test_admin_can_reset_a_friends_2fa(admin):
    friend = _user(admin, "friend")
    _login(admin, "friend", "friend-password-1")
    secret = admin.post("/api/account/2fa/setup").json()["secret"]
    admin.post("/api/account/2fa/enable", json={"code": totp_now(secret)})
    _login(admin, "admin", "correct-horse-battery")
    assert admin.patch(f"/api/users/{friend['id']}", json={"reset_2fa": True}).json()["totp_enabled"] is False
    assert _login(admin, "friend", "friend-password-1").status_code == 200


def test_api_tokens(admin, node, client):
    sid = _create(admin, node)["id"]
    token = admin.post("/api/account/tokens", json={"name": "billing"}).json()["token"]
    assert token.startswith("hearth_")
    admin.post("/api/auth/logout")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/servers", headers=headers).status_code == 200
    assert client.post(f"/api/servers/{sid}/suspend", json={"reason": "unpaid"}, headers=headers).status_code == 200
    assert client.get("/api/servers", headers={"Authorization": "Bearer hearth_nope_x"}).status_code == 401
    listed = client.get("/api/account/tokens", headers=headers).json()
    assert listed[0]["last_used"] is not None and "token" not in listed[0]
    assert client.delete(f"/api/account/tokens/{listed[0]['id']}", headers=headers).status_code == 204
    assert client.get("/api/servers", headers=headers).status_code == 401


# ----- suspension --------------------------------------------------------------------------------


def test_suspended_server_cannot_be_started_by_owner(admin, node, agent):
    friend = _user(admin, "friend")
    sid = _create(admin, node, owner_id=friend["id"])["id"]
    assert admin.post(f"/api/servers/{sid}/suspend", json={"reason": "unpaid"}).json()["suspended"] is True
    assert (sid and agent.power[-1][1]) == "stop"
    _login(admin, "friend", "friend-password-1")
    r = admin.post(f"/api/servers/{sid}/power", json={"action": "start"})
    assert r.status_code == 403 and "unpaid" in r.json()["detail"]
    _login(admin, "admin", "correct-horse-battery")
    assert admin.post(f"/api/servers/{sid}/unsuspend").json()["suspended"] is False


# ----- backups -----------------------------------------------------------------------------------


def _wait_backups(client, sid, state="ready"):
    import time

    for _ in range(200):
        rows = client.get(f"/api/servers/{sid}/backups").json()
        if rows and all(r["state"] == state for r in rows):
            return rows
        time.sleep(0.02)
    raise AssertionError(rows)


def test_backup_lifecycle(admin, node, agent):
    sid = _create(admin, node)["id"]
    r = admin.post(f"/api/servers/{sid}/backups", json={"name": "Before the dragon"})
    assert r.status_code == 202
    rows = _wait_backups(admin, sid)
    assert rows[0]["name"] == "Before the dragon" and rows[0]["size"] == 4096
    bid = rows[0]["id"]
    assert admin.get(f"/api/servers/{sid}/backups/{bid}/download").content == b"tar-bytes"
    assert admin.patch(f"/api/servers/{sid}/backups/{bid}", json={"locked": True}).json()["locked"]
    assert admin.delete(f"/api/servers/{sid}/backups/{bid}").status_code == 409

    assert admin.post(f"/api/servers/{sid}/backups/{bid}/restore", json={"backup_first": True}).status_code == 202
    kinds = [b["kind"] for b in admin.get(f"/api/servers/{sid}/backups").json()]
    assert "pre-restore" in kinds and len(agent.restored) == 1

    admin.patch(f"/api/servers/{sid}/backups/{bid}", json={"locked": False})
    assert admin.delete(f"/api/servers/{sid}/backups/{bid}").status_code == 204


def test_failed_backup_blocks_auto_update(admin, node, agent, public):
    sid = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"}, auto_update=True)["id"]
    agent.fail_backups = True
    public.versions.insert(0, {"id": "v4", "name": "Pack 4.0", "game_versions": ["1.21.4"],
                               "version_type": "release", "date_published": "2026-11-10T00:00:00Z"})
    admin.post("/api/admin/updates/run")
    server = admin.get(f"/api/servers/{sid}").json()
    assert server["modpack"]["version_id"] == "v2"  # left alone
    assert server["modpack"]["update"]["id"] == "v4"


def test_version_change_takes_safety_backup(admin, node, agent):
    sid = _create(admin, node)["id"]
    admin.put(f"/api/servers/{sid}/source", json={"kind": "software", "server_type": "FABRIC", "version": "1.21.4"})
    assert [b["kind"] for b in admin.get(f"/api/servers/{sid}/backups").json()] == ["pre-update"]
    agent.fail_backups = True
    r = admin.put(f"/api/servers/{sid}/source", json={"kind": "software", "server_type": "PAPER", "version": "1.21.4"})
    assert r.status_code == 409 and "safety backup failed" in r.json()["detail"]


# ----- schedules ---------------------------------------------------------------------------------


def test_schedule_validation_and_preview(admin, node):
    sid = _create(admin, node)["id"]
    r = admin.post(f"/api/servers/{sid}/schedules", json={"name": "x", "cron": "99 * * * *", "action": "restart"})
    assert r.status_code == 422 and "between 0 and 59" in r.json()["detail"]
    r = admin.post(f"/api/servers/{sid}/schedules", json={"name": "x", "cron": "0 4 * * *", "action": "command"})
    assert r.status_code == 400
    assert len(admin.get(f"/api/servers/{sid}/schedules/preview", params={"cron": "@daily"}).json()["next"]) == 3


@pytest.mark.anyio
async def test_scheduler_runs_due_tasks_once(admin, node, agent):
    sid = _create(admin, node)["id"]
    assert admin.put("/api/admin/settings", json={"timezone": "America/Toronto"}).status_code == 200
    admin.post(f"/api/servers/{sid}/schedules", json={"name": "Greeting", "cron": "30 4 * * *",
                                                     "action": "command", "payload": "say good morning"})
    admin.post(f"/api/servers/{sid}/schedules", json={"name": "Nightly", "cron": "30 4 * * *",
                                                     "action": "backup", "keep_backups": 1})
    scheduler = admin.app.state.scheduler
    assert await scheduler.tick(datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)) == 0  # 4:30 UTC is 23:30 Toronto
    at = datetime(2026, 11, 16, 9, 30, 5, tzinfo=timezone.utc)  # 4:30 in Toronto (EST, UTC-5)
    assert await scheduler.tick(at) == 2
    assert await scheduler.tick(at) == 0  # same minute: not again
    for task in list(scheduler._running):
        await task
    assert (sid and any(c == "say good morning" for _, c in agent.commands))
    rows = admin.get(f"/api/servers/{sid}/schedules").json()["schedules"]
    assert all(r["last_result"] for r in rows)
    assert await scheduler.tick(datetime(2026, 11, 16, 9, 31, tzinfo=timezone.utc)) == 0


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ----- players & mods ----------------------------------------------------------------------------


def test_players_overview_and_actions(admin, node, agent):
    sid = _create(admin, node)["id"]
    agent.command_output = "There are 2 of a max of 20 players online: alex, sam\n"
    agent.files["/whitelist.json"] = '[{"uuid": "x", "name": "alex"}]'
    agent.files["/banned-players.json"] = '[{"name": "griefer", "reason": "lava"}]'
    data = admin.get(f"/api/servers/{sid}/players").json()
    assert data["online"] == ["alex", "sam"] and data["max"] == 20
    assert data["whitelist"] == ["alex"] and data["banned"][0]["reason"] == "lava"
    admin.post(f"/api/servers/{sid}/players", json={"action": "ban", "name": "sam", "reason": "spam\nnow"})
    assert agent.commands[-1][1] == "ban sam spam now"
    r = admin.post(f"/api/servers/{sid}/players", json={"action": "op", "name": "bad name!"})
    assert r.status_code == 422


def test_plugin_install_includes_required_dependency(admin, node, agent):
    sid = _create(admin, node)["id"]
    found = admin.get(f"/api/servers/{sid}/mods/search").json()
    assert found["target"]["kind"] == "plugin" and found["target"]["folder"] == "/plugins"
    r = admin.post(f"/api/servers/{sid}/mods", json={"project_id": "PLUGIN01"})
    assert r.json()["installed"] == ["plugin01.jar", "dep00001.jar"]
    assert {f["path"] for f in agent.fetched} == {"/plugins/plugin01.jar", "/plugins/dep00001.jar"}


def test_vanilla_cannot_take_mods(admin, node):
    sid = _create(admin, node, variables={"TYPE": "VANILLA", "VERSION": "1.21.4"})["id"]
    r = admin.get(f"/api/servers/{sid}/mods")
    assert r.status_code == 400 and "Vanilla" in r.json()["detail"]


# ----- events, Discord, webhooks, activity -------------------------------------------------------


def test_discord_and_webhooks_receive_events(admin, node, public):
    sid = _create(admin, node)["id"]
    hook = "https://discord.com/api/webhooks/123/abc"
    assert admin.patch(f"/api/servers/{sid}", json={"discord_webhook": "https://evil.example"}).status_code == 422
    admin.patch(f"/api/servers/{sid}", json={"discord_webhook": hook})
    created = admin.post("/api/admin/webhooks", json={"url": "https://billing.example/hook",
                                                      "events": ["server.suspended"]}).json()
    assert len(created["secret"]) == 48
    admin.post(f"/api/servers/{sid}/suspend", json={"reason": "test"})
    import time

    for _ in range(100):
        if len(public.posts) >= 2:
            break
        time.sleep(0.02)
    urls = [u for u, _ in public.posts]
    assert hook in urls and "https://billing.example/hook" in urls
    discord_body = next(b for u, b in public.posts if u == hook)
    assert discord_body["embeds"][0]["title"] == "Survival was suspended"
    log = admin.get("/api/admin/activity").json()
    assert {"server.created", "server.suspended"} <= {e["action"] for e in log}


# ----- clone, join check, metrics ----------------------------------------------------------------


def test_clone_copies_files_to_new_port(admin, node, agent):
    src = _create(admin, node)
    r = admin.post(f"/api/servers/{src['id']}/clone", json={"name": "Test copy"})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["address"].endswith(":25566") and copy["uuid"] != src["uuid"]
    assert (copy["uuid"], src["uuid"]) in agent.copies


def test_join_check_reports_reachability(admin, node, agent, public):
    sid = _create(admin, node)["id"]
    data = admin.get(f"/api/servers/{sid}/join-check").json()
    keys = {c["key"]: c for c in data["checks"]}
    assert keys["running"]["ok"] is False and data["address"] == "play.example.net:25565"
    assert keys["internet"]["ok"] is None  # not checked while the server is off


def test_tunnel_address_replaces_node_address(admin, node):
    sid = _create(admin, node)["id"]
    r = admin.patch(f"/api/servers/{sid}", json={"public_address": "abc.joinmc.link:4433"})
    assert r.json()["address"] == "abc.joinmc.link:4433"
    assert r.json()["direct_address"] == "play.example.net:25565"
    assert admin.patch(f"/api/servers/{sid}", json={"public_address": "bad address!"}).status_code == 422


def test_metrics_proxy(admin, node):
    sid = _create(admin, node)["id"]
    data = admin.get(f"/api/servers/{sid}/metrics").json()
    assert data["minutes"][0]["cpu"] == 5.0 and data["memory_limit"] == 4096 * 1048576


# ----- monitor: events + auto-sleep --------------------------------------------------------------


@pytest.mark.anyio
async def test_monitor_emits_transitions_and_puts_empty_servers_to_sleep(admin, node, agent):
    sid = _create(admin, node)["id"]
    admin.patch(f"/api/servers/{sid}", json={"auto_stop_minutes": 10})
    monitor = admin.app.state.monitor
    state = {"value": "offline"}
    original = agent.__call__

    import httpx

    def patched(request):
        if request.url.path == "/api/servers":
            return httpx.Response(200, json=[{"uuid": u, "state": state["value"], "restarts": 0, "oom_kills": 0}
                                             for u in agent.specs])
        return original(request)

    admin.app.state.ctx.nodes.http._transport = httpx.MockTransport(patched)
    agent.command_output = "There are 0 of a max of 20 players online:"
    await monitor.tick(now=0)  # first sight
    state["value"] = "running"
    await monitor.tick(now=15)  # started + first empty check
    await monitor.tick(now=15 + 61)  # still empty, < 10 min
    assert agent.slept == []
    await monitor.tick(now=15 + 61 + 600)  # 10+ minutes empty
    assert len(agent.slept) == 1
    actions = [e["action"] for e in admin.get(f"/api/servers/{sid}/activity").json()]
    assert "server.started" in actions


def test_join_check_when_running(admin, node, agent, public):
    import httpx

    sid = _create(admin, node)["id"]
    original = agent.__call__

    def running(request):
        if request.url.path == "/api/servers":
            return httpx.Response(200, json=[{"uuid": u, "state": "running"} for u in agent.specs])
        return original(request)

    admin.app.state.ctx.nodes.http._transport = httpx.MockTransport(running)
    ok = {c["key"]: c for c in admin.get(f"/api/servers/{sid}/join-check").json()["checks"]}
    assert ok["internet"]["ok"] is True and "1/20" in ok["internet"]["detail"]
    public.reachable = False
    admin.app.state.ctx.cache.invalidate()
    bad = {c["key"]: c for c in admin.get(f"/api/servers/{sid}/join-check").json()["checks"]}
    assert bad["internet"]["ok"] is False and "port 25565/TCP" in bad["internet"]["detail"]


# ----- bug-check regressions ---------------------------------------------------------------------


def test_schedules_need_the_permission_they_use(admin, node, agent):
    sid = _create(admin, node)["id"]
    _user(admin, "friend")
    admin.post(f"/api/servers/{sid}/shares", json={"username": "friend", "permissions": ["schedules"]})
    _login(admin, "friend", "friend-password-1")
    body = {"name": "Op me", "cron": "* * * * *", "action": "command", "payload": "op friend"}
    r = admin.post(f"/api/servers/{sid}/schedules", json=body)
    assert r.status_code == 403 and "console" in r.json()["detail"]
    assert admin.post(f"/api/servers/{sid}/schedules",
                      json={"name": "x", "cron": "0 4 * * *", "action": "restart"}).status_code == 403
    _login(admin, "admin", "correct-horse-battery")
    sched = admin.post(f"/api/servers/{sid}/schedules", json=body).json()
    _login(admin, "friend", "friend-password-1")
    assert admin.post(f"/api/servers/{sid}/schedules/{sched['id']}/run").status_code == 403
    assert not any(c == "op friend" for _, c in agent.commands)


@pytest.mark.anyio
async def test_scheduled_restart_leaves_a_stopped_server_alone(admin, node, agent):
    sid = _create(admin, node)["id"]
    sched = admin.post(f"/api/servers/{sid}/schedules",
                       json={"name": "Nightly", "cron": "0 4 * * *", "action": "restart"}).json()
    scheduler = admin.app.state.scheduler
    agent.power.clear()
    agent.state = "offline"
    assert (await scheduler.execute(sched["id"])).startswith("Skipped")
    agent.state = "sleeping"
    assert (await scheduler.execute(sched["id"])).startswith("Skipped")
    assert agent.power == []
    agent.state = "running"
    assert await scheduler.execute(sched["id"]) == "Restarted."
    assert agent.power[-1][1] == "restart"


def test_suspended_server_cannot_be_cloned_by_its_owner(admin, node, agent):
    friend = _user(admin, "friend", max_servers=5)
    sid = _create(admin, node, owner_id=friend["id"])["id"]
    admin.post(f"/api/servers/{sid}/suspend", json={"reason": "unpaid"})
    _login(admin, "friend", "friend-password-1")
    r = admin.post(f"/api/servers/{sid}/clone", json={"name": "Escape", "copy_files": False})
    assert r.status_code == 403 and "suspended" in r.json()["detail"]


@pytest.mark.anyio
async def test_unfinished_backups_are_picked_up_after_a_panel_restart(admin, node, agent):
    from hearth_panel.models import Backup
    from hearth_panel.services import backups as jobs

    sid = _create(admin, node)["id"]
    ctx = admin.app.state.ctx
    with ctx.db.sessionmaker() as db:
        row = Backup(server_id=sid, name="Interrupted", kind="manual", state="creating")
        db.add(row)
        db.commit()
        uuid = row.uuid
    assert admin.post(f"/api/servers/{sid}/backups", json={}).status_code == 409  # blocked, as before
    assert admin.delete(f"/api/servers/{sid}/backups/{row.id}").status_code == 409
    agent.backups[uuid] = {"state": "ready", "size": 2048}  # the node finished it meanwhile
    assert jobs.resume_pending(ctx) == 1
    for task in list(jobs._tasks):
        await task
    assert admin.get(f"/api/servers/{sid}/backups").json()[0]["state"] == "ready"
    assert admin.post(f"/api/servers/{sid}/backups", json={}).status_code == 202


def test_join_check_local_address_detection():
    from hearth_panel.routers.servers import _is_local_host

    assert _is_local_host("192.168.1.20") and _is_local_host("10.0.0.5") and _is_local_host("172.20.0.1")
    assert _is_local_host("localhost") and _is_local_host("100.72.1.1")  # CGNAT
    assert not _is_local_host("172.67.1.1")  # public (outside 172.16.0.0/12)
    assert not _is_local_host("mc.example.com") and not _is_local_host("8.8.8.8")


def test_admin_password_reset_revokes_api_tokens(admin, client):
    friend = _user(admin, "friend")
    _login(admin, "friend", "friend-password-1")
    token = admin.post("/api/account/tokens", json={"name": "bot"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/servers", headers=headers).status_code == 200
    _login(admin, "admin", "correct-horse-battery")
    admin.patch(f"/api/users/{friend['id']}", json={"password": "a-brand-new-password"})
    assert client.get("/api/servers", headers=headers).status_code == 401


def test_password_guessing_is_throttled_for_signed_in_sessions(admin):
    for _ in range(8):
        r = admin.post("/api/auth/password", json={"current_password": "nope-nope-nope", "new_password": "x" * 12})
        assert r.status_code == 400
    r = admin.post("/api/auth/password", json={"current_password": "correct-horse-battery", "new_password": "x" * 12})
    assert r.status_code == 429


@pytest.mark.anyio
async def test_racing_creates_cannot_exceed_a_quota(admin, node, monkeypatch):
    """Two creates sent at once used to both pass the quota check while waiting on Modrinth."""
    import asyncio

    import httpx

    from hearth_panel.routers import servers as servers_router

    _user(admin, "friend", max_servers=1, max_memory_mb=8192)
    original = servers_router.resolve_modpack

    async def slow_resolve(*args, **kwargs):
        await asyncio.sleep(0.05)  # both requests are now past the quota check
        return await original(*args, **kwargs)

    monkeypatch.setattr(servers_router, "resolve_modpack", slow_resolve)
    transport = httpx.ASGITransport(app=admin.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as friend:
        r = await friend.post("/api/auth/login", json={"username": "friend", "password": "friend-password-1"})
        assert r.status_code == 200
        body = {"name": "Pack", "node_id": node["id"], "template": "minecraft-java", "memory_mb": 4096,
                "accept_eula": True, "modpack": {"provider": "modrinth", "project_id": "cool-pack"}, "start": False}
        results = await asyncio.gather(friend.post("/api/servers", json=body), friend.post("/api/servers", json=body))
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 403], [r.text for r in results]
    assert len([s for s in admin.get("/api/servers").json() if s["name"] == "Pack"]) == 1


def test_first_run_setup_only_creates_one_admin(client):
    from hearth_panel.models import User

    ctx = client.app.state.ctx
    with ctx.db.sessionmaker() as db:  # someone else already finished setup
        db.add(User(username="early", password_hash="x", is_admin=True))
        db.commit()
    r = client.post("/api/auth/setup", json={"username": "late", "password": "correct-horse-battery"})
    assert r.status_code == 409
    with ctx.db.sessionmaker() as db:
        assert [u.username for u in db.query(User)] == ["early"]


def _wait_for_posts(public, count, seconds=2.0):
    import time

    for _ in range(int(seconds / 0.02)):
        if len(public.posts) >= count:
            return
        time.sleep(0.02)


def test_webhooks_cannot_target_the_panels_own_network(admin, node, public):
    for url in ("http://agent:8080/api/servers", "http://localhost:8000/x", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/hook", "https://inside.example.com/hook", "https://metadata.example.com/x",
                "http://nas.lan/hook"):
        r = admin.post("/api/admin/webhooks", json={"url": url, "events": []})
        assert r.status_code == 400 and "HEARTH_ALLOW_PRIVATE_WEBHOOKS" in r.json()["detail"], url

    # A name that was public when saved but now points inside the network is skipped at send time.
    from conftest import FAKE_DNS

    assert admin.post("/api/admin/webhooks", json={"url": "https://moves.example.com/hook", "events": []}).status_code == 201
    FAKE_DNS["moves.example.com"] = ["192.168.1.10"]
    try:
        admin.post("/api/admin/webhooks/test")
        _wait_for_posts(public, 1, seconds=0.5)
        assert not any("moves.example.com" in u for u, _ in public.posts)
    finally:
        FAKE_DNS.pop("moves.example.com")


def test_private_webhooks_allowed_when_opted_in(admin, public, monkeypatch):
    import dataclasses

    ctx = admin.app.state.ctx
    monkeypatch.setattr(ctx, "settings", dataclasses.replace(ctx.settings, allow_private_webhooks=True))
    assert admin.post("/api/admin/webhooks", json={"url": "http://10.0.0.5/hook", "events": []}).status_code == 201
    admin.post("/api/admin/webhooks/test")
    _wait_for_posts(public, 1)
    assert any(u == "http://10.0.0.5/hook" for u, _ in public.posts)


def test_webhooks_do_not_follow_redirects(admin, public):
    public.redirect_posts_to = "http://169.254.169.254/latest/meta-data"
    admin.post("/api/admin/webhooks", json={"url": "https://billing.example/hook", "events": []})
    admin.post("/api/admin/webhooks/test")
    _wait_for_posts(public, 1)
    assert [u for u, _ in public.posts] == ["https://billing.example/hook"]


def test_share_lookups_cannot_enumerate_usernames(admin, node):
    friend = _user(admin, "friend", max_servers=2)
    sid = _create(admin, node, owner_id=friend["id"])["id"]
    _user(admin, "zoe")
    _login(admin, "friend", "friend-password-1")
    for i in range(10):
        r = admin.post(f"/api/servers/{sid}/shares", json={"username": f"guess{i}", "permissions": []})
        assert r.status_code == 404
    r = admin.post(f"/api/servers/{sid}/shares", json={"username": "zoe", "permissions": []})
    assert r.status_code == 429  # paused, even for a name that exists


# ----- full bug check -----------------------------------------------------------------------------


class _FakeUpstream:
    """Stands in for the agent's console socket."""

    sent: list = []

    def __init__(self, *args, **kwargs):
        import asyncio

        self.queue: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        await self.queue.put('{"type":"log","data":"[Server] ready\\n"}')
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        _FakeUpstream.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


def test_console_commands_are_logged_and_access_is_rechecked(admin, node, monkeypatch):
    import json as _json

    import websockets.asyncio.client

    from hearth_panel.routers import servers as servers_router

    monkeypatch.setattr(websockets.asyncio.client, "connect", _FakeUpstream)
    monkeypatch.setattr(servers_router, "CONSOLE_RECHECK", 0.2)
    _FakeUpstream.sent = []
    sid = _create(admin, node)["id"]
    _user(admin, "friend")
    share = admin.post(f"/api/servers/{sid}/shares", json={"username": "friend", "permissions": ["console"]}).json()
    _login(admin, "friend", "friend-password-1")
    with admin.websocket_connect(f"/api/servers/{sid}/console") as ws:
        assert ws.receive_json()["type"] == "log"
        ws.send_text(_json.dumps({"type": "command", "data": "say hi"}))
        ws.send_text(_json.dumps({"type": "power", "data": "start"}))  # not a console command: dropped
        ws.send_text(_json.dumps({"type": "command", "data": "list"}))
        import time

        for _ in range(50):
            if len(_FakeUpstream.sent) >= 2:
                break
            time.sleep(0.02)
        assert [_json.loads(m)["data"] for m in _FakeUpstream.sent] == ["say hi", "list"]

        # Access removed while the console is open: the session ends.
        ctx = admin.app.state.ctx
        from hearth_panel.models import ServerShare

        with ctx.db.sessionmaker() as db:
            db.delete(db.get(ServerShare, share["id"]))
            db.commit()
        msg = ws.receive_json()
        while msg["type"] != "error":
            msg = ws.receive_json()
        assert "ended" in msg["data"]
    _login(admin, "admin", "correct-horse-battery")
    log = admin.get(f"/api/servers/{sid}/activity").json()
    assert [e["detail"] for e in log if e["action"] == "console.command"] == ["list", "say hi"]


def test_cron_star_step_day_fields_are_unrestricted():
    from datetime import datetime

    from hearth_panel.services.scheduler import Cron

    # "*/2" doesn't count as a day restriction in standard cron, so this means: Mondays that are
    # also odd-numbered days (AND), not "every odd day OR every Monday".
    every_other_day_on_mondays = Cron.parse("0 4 */2 * 1")
    assert every_other_day_on_mondays.matches(datetime(2026, 11, 16, 4, 0)) is False  # Monday the 16th (even)
    assert every_other_day_on_mondays.matches(datetime(2026, 11, 17, 4, 0)) is False  # Tuesday the 17th
    assert every_other_day_on_mondays.matches(datetime(2026, 11, 23, 4, 0)) is True   # Monday the 23rd (odd)
    either = Cron.parse("0 4 1 * 1")  # both really restricted: the 1st OR any Monday
    assert either.matches(datetime(2026, 11, 16, 4, 0)) and either.matches(datetime(2026, 12, 1, 4, 0))


def test_swap_counts_toward_the_memory_quota(admin, node):
    _user(admin, "friend", max_servers=3, max_memory_mb=4096)
    _login(admin, "friend", "friend-password-1")
    body = {"name": "Swappy", "node_id": node["id"], "template": "minecraft-java", "memory_mb": 1024,
            "swap_mb": 65536, "accept_eula": True, "start": False}
    r = admin.post("/api/servers", json=body)
    assert r.status_code == 403 and "quota" in r.json()["detail"]
    sid = admin.post("/api/servers", json={**body, "swap_mb": 0}).json()["id"]
    assert admin.patch(f"/api/servers/{sid}", json={"swap_mb": 65536}).status_code == 403


def test_players_tab_survives_hand_edited_files(admin, node, agent, monkeypatch):
    sid = _create(admin, node)["id"]
    from hearth_panel.services.node_client import NodeClient

    async def odd_file(self, node, uuid, path):
        return '[{"name": "Steve", "uuid": "x"}, "junk", 42, null]'

    monkeypatch.setattr(NodeClient, "read_file", odd_file)
    r = admin.get(f"/api/servers/{sid}/players")
    assert r.status_code == 200 and r.json()["whitelist"] == ["Steve"]


def test_https_node_install_command_sets_up_tls(admin):
    r = admin.post("/api/nodes", json={"name": "VPS", "url": "https://vps.example.com:8443", "public_host": "vps.example.com"})
    command = r.json()["install_command"]
    assert "-p 8443:8080" in command and "HEARTH_TLS_CERT=/etc/letsencrypt/live/vps.example.com/fullchain.pem" in command
    plain = admin.post("/api/nodes", json={"name": "LAN", "url": "http://192.168.1.5:8080", "public_host": "x.lan"})
    assert "HEARTH_TLS_CERT" not in plain.json()["install_command"]


def test_custom_server_environment_is_admin_only_to_read(admin, node):
    friend = _user(admin, "friend")
    r = admin.post("/api/servers", json={
        "name": "Custom", "node_id": node["id"], "template": "custom", "memory_mb": 1024, "owner_id": friend["id"],
        "start": False, "variables": {"IMAGE": "ghcr.io/x/game:1", "CONTAINER_PORT": 7777,
                                      "EXTRA_ENV": "ADMIN_PASSWORD=hunter2\nMAX=10"}})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert admin.get(f"/api/servers/{sid}").json()["variables"]["EXTRA_ENV"].startswith("ADMIN_PASSWORD")
    _login(admin, "friend", "friend-password-1")
    seen = admin.get(f"/api/servers/{sid}").json()
    assert seen["variables"]["EXTRA_ENV"] == "" and "hunter2" not in str(seen)
    spec = admin.get(f"/api/servers/{sid}/spec").json()
    assert spec["env"]["ADMIN_PASSWORD"] == "••••••" and "hunter2" not in str(spec)
    # Saving the settings form sends the masked value back; that must not count as a change.
    r = admin.patch(f"/api/servers/{sid}", json={"variables": {**seen["variables"]}})
    assert r.status_code == 200, r.text
    _login(admin, "admin", "correct-horse-battery")
    assert "hunter2" in admin.get(f"/api/servers/{sid}").json()["variables"]["EXTRA_ENV"]


@pytest.mark.anyio
async def test_scheduled_restart_is_skipped_if_stopped_during_the_countdown(admin, node, agent, monkeypatch):
    sid = _create(admin, node)["id"]
    sched = admin.post(f"/api/servers/{sid}/schedules", json={"name": "Nightly", "cron": "0 4 * * *",
                                                             "action": "restart", "warn_minutes": 1}).json()
    scheduler = admin.app.state.scheduler

    async def countdown(*_args):
        agent.state = "offline"  # someone stopped it while players were being warned

    monkeypatch.setattr(scheduler, "_warn", countdown)
    agent.state, agent.power = "running", []
    assert (await scheduler.execute(sched["id"])).startswith("Skipped")
    assert agent.power == []


@pytest.mark.anyio
async def test_unexpected_schedule_errors_are_recorded(admin, node, monkeypatch):
    sid = _create(admin, node)["id"]
    sched = admin.post(f"/api/servers/{sid}/schedules", json={"name": "Say", "cron": "* * * * *",
                                                             "action": "command", "payload": "say hi"}).json()
    scheduler = admin.app.state.scheduler

    async def boom(*_args):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(scheduler, "_act", boom)
    assert (await scheduler.execute(sched["id"])) == "Failed: something unexpected"
    row = next(s for s in admin.get(f"/api/servers/{sid}/schedules").json()["schedules"] if s["id"] == sched["id"])
    assert row["last_result"] == "Failed: something unexpected"


@pytest.mark.anyio
async def test_auto_sleep_uses_the_motd_and_auto_stop_is_announced_once(admin, node, agent, public):
    sid = _create(admin, node, variables={"MOTD": "Hearth SMP"})["id"]
    admin.patch(f"/api/servers/{sid}", json={"auto_stop_minutes": 1, "wake_on_join": True})
    monitor = admin.app.state.monitor
    agent.state, agent.command_output = "running", "There are 0 of a max of 20 players online:"
    for t in (0, 61, 122, 183):
        await monitor.tick(now=float(t))
    assert agent.slept and agent.slept[-1][1] == "Hearth SMP"

    sid2 = _create(admin, node, name="Plain")["id"]
    admin.patch(f"/api/servers/{sid2}", json={"auto_stop_minutes": 1, "wake_on_join": False})
    monitor.seen.clear()
    for t in (1000, 1061, 1122, 1183):
        await monitor.tick(now=float(t))
    agent.state = "offline"
    await monitor.tick(now=1200.0)
    log = admin.get(f"/api/servers/{sid2}/activity").json()
    assert [e["action"] for e in log].count("server.stopped") == 1
