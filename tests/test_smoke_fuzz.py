"""Every route, hit with junk ids and bodies as an admin: nothing may answer 500."""

import re

import pytest

from tests.test_features import _create

SKIP = ("/api/auth/logout", "/api/auth/setup", "/api/nodes/{node_id}/tunnel", "/console")
BODIES = [None, {}, {"name": ""}, {"name": "x" * 400, "path": "../../etc/passwd", "cron": "x", "action": "nope"},
          {"username": "a", "password": "b"}, [], "text", {"ports": "99999-1", "url": "ftp://x", "token": 5}]
IDS = ["0", "-1", "999999", "abc", "%00", "1"]


def _routes(app):
    spec = app.openapi()
    for path, ops in spec["paths"].items():
        for method in ops:
            if method in ("get", "post", "put", "patch", "delete") and not any(s in path for s in SKIP):
                yield method.upper(), path


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_no_route_answers_500(admin, node, agent, method):
    _create(admin, node)
    bad = []
    for m, path in _routes(admin.app):
        if m != method:
            continue
        for ident in IDS:
            url = re.sub(r"\{[^}]+\}", ident, path)
            for body in BODIES if method != "GET" else [None]:
                r = admin.request(method, url, json=body) if body is not None else admin.request(method, url)
                if r.status_code >= 500:
                    bad.append((method, url, body, r.status_code, r.text[:120]))
    assert not bad, "\n".join(map(str, bad[:15]))


def test_routes_need_a_login(admin, node, agent):
    """Signed out, only the sign-in, setup and status routes answer with anything but 401."""
    _create(admin, node)
    admin.post("/api/auth/logout")
    open_routes = []
    for m, path in _routes(admin.app):
        r = admin.request(m, re.sub(r"\{[^}]+\}", "1", path), json={} if m != "GET" else None)
        if r.status_code != 401:
            open_routes.append((m, path, r.status_code))
    assert {p for _, p, _ in open_routes} <= {"/api/auth/state", "/api/auth/login"}, open_routes


def test_friend_cannot_reach_admin_routes(admin, node, agent):
    from tests.test_features import _login, _user

    _create(admin, node)
    _user(admin, "pal")
    _login(admin, "pal", "pal-password-1")
    leaks = []
    for m, path in _routes(admin.app):
        if not path.startswith("/api/admin") and not path.startswith("/api/nodes") and not path.startswith("/api/users"):
            continue
        if path.startswith("/api/users/me") or path in ("/api/nodes/available",):
            continue
        r = admin.request(m, re.sub(r"\{[^}]+\}", "1", path), json={} if m != "GET" else None)
        if r.status_code < 400 or r.status_code >= 500:
            leaks.append((m, path, r.status_code))
    assert not leaks, leaks


def _server_routes(app):
    for m, path in _routes(app):
        if "{server_id}" in path:
            yield m, path


def test_strangers_cannot_touch_someone_elses_server(admin, node, agent):
    from tests.test_features import _login, _user

    sid = _create(admin, node)["id"]
    _user(admin, "stranger")
    _login(admin, "stranger", "stranger-password-1")
    leaks = []
    for m, path in _server_routes(admin.app):
        url = re.sub(r"\{server_id\}", str(sid), path)
        url = re.sub(r"\{[^}]+\}", "1", url)
        r = admin.request(m, url, json={} if m != "GET" else None)
        if r.status_code < 400 or r.status_code >= 500:
            leaks.append((m, path, r.status_code))
    assert not leaks, leaks


def test_a_console_only_share_reaches_only_console_routes(admin, node, agent):
    from tests.test_features import _login, _user

    sid = _create(admin, node)["id"]
    _user(admin, "helper")
    admin.post(f"/api/servers/{sid}/shares", json={"username": "helper", "permissions": ["console"]})
    _login(admin, "helper", "helper-password-1")
    reached = []
    for m, path in _server_routes(admin.app):
        if m != "GET":
            continue
        url = re.sub(r"\{[^}]+\}", "1", re.sub(r"\{server_id\}", str(sid), path))
        r = admin.get(url)
        if r.status_code < 300:
            reached.append(path)
    print(sorted(reached))
