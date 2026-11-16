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
    assert r.headers["cache-control"] == "no-store"
