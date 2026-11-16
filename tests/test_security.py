"""Every API route must reject anonymous callers, and owners must not reach other servers."""

import re

PUBLIC = {"/api/auth/state", "/api/auth/setup", "/api/auth/login", "/api/auth/logout", "/api/docs",
          "/api/docs/oauth2-redirect", "/openapi.json", "/"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _endpoints(app):
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            yield method.upper(), path


def test_all_routes_require_login(client):
    client.post("/api/auth/setup", json={"username": "admin", "password": "correct-horse-battery"})
    client.post("/api/auth/logout")
    checked = 0
    for method, path in _endpoints(client.app):
        if path in PUBLIC:
            continue
        r = client.request(method, _concrete(path), json={})
        assert r.status_code in (401, 422), f"{method} {path} -> {r.status_code}"
        checked += 1
    assert checked > 30


def test_admin_routes_reject_owners(admin, node):
    admin.post("/api/users", json={"username": "friend", "password": "friend-password-1"})
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"username": "friend", "password": "friend-password-1"})
    checked = 0
    for method, path in _endpoints(admin.app):
        admin_only = path.startswith(("/api/nodes", "/api/users", "/api/admin")) or (
            path == "/api/servers" and method == "POST")
        if admin_only:
            r = admin.request(method, _concrete(path), json={})
            assert r.status_code in (403, 422), f"{method} {path} -> {r.status_code}"
            checked += 1
    assert checked > 10


def test_console_socket_rejects_strangers_and_cross_origin(admin, node):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    sid = admin.post("/api/servers", json={"name": "s", "node_id": node["id"], "template": "minecraft-java",
                                           "memory_mb": 2048, "accept_eula": True}).json()["id"]
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"/api/servers/{sid}/console", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    admin.post("/api/auth/logout")
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"/api/servers/{sid}/console") as ws:
            ws.receive_text()


def test_session_cookie_flags(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "correct-horse-battery"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_security_headers(admin):
    r = admin.get("/api/servers")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert admin.get("/").headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def _panel(tmp_path, agent, public, address, **overrides):
    import httpx
    from fastapi.testclient import TestClient

    from hearth_panel.config import Settings
    from hearth_panel.context import PanelContext
    from hearth_panel.main import create_app

    base = Settings.from_env()
    settings = Settings(**{**base.__dict__, "database_url": f"sqlite:///{tmp_path / 'p.db'}",
                           "secret_key": "test-secret-" + "x" * 32, **overrides})
    context = PanelContext.build(settings, node_transport=httpx.MockTransport(agent),
                                 public_transport=httpx.MockTransport(public))
    return TestClient(create_app(context, run_updater=False), client=(address, 40000))


def test_first_admin_can_only_be_created_from_the_local_network(tmp_path, agent, public):
    body = {"username": "admin", "password": "correct-horse-battery"}
    with _panel(tmp_path, agent, public, "8.8.4.4") as stranger:
        assert stranger.get("/api/auth/state").json()["setup_allowed"] is False
        r = stranger.post("/api/auth/setup", json=body)
        assert r.status_code == 403 and "HEARTH_ALLOW_REMOTE_SETUP" in r.json()["detail"]
    for address in ("192.168.1.40", "10.0.0.8", "172.18.0.1", "::1"):  # LAN, Docker gateway, loopback
        with _panel(tmp_path / address.replace(":", "_"), agent, public, address) as local:
            assert local.get("/api/auth/state").json()["setup_allowed"] is True
    with _panel(tmp_path / "remote-ok", agent, public, "8.8.4.4", allow_remote_setup=True) as allowed:
        assert allowed.post("/api/auth/setup", json=body).status_code == 200


def test_public_http_agent_urls_need_explicit_opt_in(admin):
    node = {"name": "VPS", "public_host": "vps.example.com"}
    r = admin.post("/api/nodes", json={**node, "url": "http://vps.example.com:8080"})
    assert r.status_code == 400 and "https://" in r.json()["detail"]
    assert admin.post("/api/nodes", json={**node, "url": "http://8.8.4.4:8080"}).status_code == 400
    for ok in ("https://vps.example.com:8443", "http://agent:8080", "http://192.168.1.20:8080", "http://nas.lan:8080"):
        r = admin.post("/api/nodes", json={**node, "name": ok[-14:], "url": ok})
        assert r.status_code == 201, (ok, r.text)
    r = admin.post("/api/nodes", json={**node, "name": "VPN box", "url": "http://vps.example.com:8080",
                                       "allow_insecure": True})
    assert r.status_code == 201 and "allow_insecure" not in r.json()
    nid = r.json()["id"]
    assert admin.patch(f"/api/nodes/{nid}", json={"url": "http://other.example.com:8080"}).status_code == 400
    assert admin.patch(f"/api/nodes/{nid}", json={"name": "Renamed"}).status_code == 200  # unchanged URL is fine


def test_one_address_cannot_spray_passwords_across_many_accounts(tmp_path, agent, public):
    with _panel(tmp_path, agent, public, "8.8.4.4", allow_remote_setup=True) as internet:
        internet.post("/api/auth/setup", json={"username": "admin", "password": "correct-horse-battery"})
        internet.post("/api/auth/logout")
        for i in range(30):  # one wrong guess each against 30 different names: under the per-name limit
            r = internet.post("/api/auth/login", json={"username": f"user{i:02d}", "password": "Summer2026!"})
            assert r.status_code == 401
        r = internet.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
        assert r.status_code == 429  # the address is paused, even for a right password


def test_shared_internal_address_is_not_locked_out_as_a_whole(admin):
    # Behind Docker Desktop or a proxy, everyone can appear as one local address: one person's
    # failures must not lock everyone out (the per-username limit still applies).
    for i in range(30):
        admin.post("/api/auth/login", json={"username": f"user{i:02d}", "password": "Summer2026!"})
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"}).status_code == 200


def test_breached_passwords_are_refused(admin, public):
    r = admin.post("/api/users", json={"username": "friend", "password": "password1234"})
    assert r.status_code == 400 and "data breaches" in r.json()["detail"]
    assert all(len(p) == 5 for p in public.pwned_prefixes)  # only a 5-character hash prefix leaves
    assert admin.post("/api/users", json={"username": "friend", "password": "a-long-unique-phrase"}).status_code == 201
    r = admin.post("/api/auth/password", json={"current_password": "correct-horse-battery", "new_password": "password1234"})
    assert r.status_code == 400
    public.pwned_down = True  # the service being down never blocks setting a password
    assert admin.post("/api/users", json={"username": "friend2", "password": "password1234"}).status_code == 201


def test_breached_password_check_can_be_turned_off(admin, monkeypatch):
    import dataclasses

    ctx = admin.app.state.ctx
    monkeypatch.setattr(ctx, "settings", dataclasses.replace(ctx.settings, breached_password_check=False))
    assert admin.post("/api/users", json={"username": "friend", "password": "password1234"}).status_code == 201


def test_short_session_secret_is_warned_about(tmp_path, caplog):
    import logging

    from hearth_panel.config import Settings
    from hearth_panel.db import Database
    from hearth_panel.services import settings_store

    base = Settings.from_env()
    db = Database(f"sqlite:///{tmp_path / 's.db'}")
    db.create_all()
    with db.sessionmaker() as session, caplog.at_level(logging.WARNING):
        short = Settings(**{**base.__dict__, "secret_key": "hunter2"})
        assert settings_store.secret_key(session, short) == "hunter2"  # still used: nothing breaks
        assert "only 7 characters" in caplog.text
        caplog.clear()
        settings_store.secret_key(session, Settings(**{**base.__dict__, "secret_key": "x" * 48}))
        assert caplog.text == ""


def test_sign_out_everywhere_invalidates_copied_session_cookies(admin):
    stolen = admin.cookies.get("hearth_session")
    token = admin.post("/api/account/tokens", json={"name": "bot"}).json()["token"]
    assert admin.post("/api/auth/logout-everywhere").status_code == 204
    admin.cookies.set("hearth_session", stolen)
    assert admin.get("/api/servers").status_code == 401
    admin.cookies.clear()
    assert admin.get("/api/servers", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_plain_sign_out_still_leaves_other_sessions_alone(admin):
    other = admin.cookies.get("hearth_session")
    admin.post("/api/auth/logout")
    admin.cookies.set("hearth_session", other)
    assert admin.get("/api/servers").status_code == 200
