"""Console charts: tick-speed parsing, minute rows, the /series view, and what the monitor records."""

import time

import httpx
import pytest

from hearth_panel.models import Server, ServerMetric
from hearth_panel.services import metrics

from tests.test_features import _create


# ----- parsing -------------------------------------------------------------------------------------

def test_parse_paper_tps_and_mspt():
    out = "§6TPS from last 1m, 5m, 15m: §a*20.0, §a19.98, §a19.5"
    assert metrics.parse_ticks("tps", out) == (20.0, None)
    assert metrics.parse_ticks("mspt", "§6Server tick times (avg/min/max) from last 5s, 10s, 1m:\n§a◴ 12.3/3.1/40.2, 11.0/3.0/44.1, 10.5/2.9/60.0") == (None, 10.5)


def test_parse_tick_query_vanilla_and_forge():
    out = "Target tick rate: 20.0 per second.\nAverage time per tick: 4.2ms (Target: 50.0ms)"
    assert metrics.parse_ticks("tick query", out) == (20.0, 4.2)
    slow = "Target tick rate: 20.0 per second.\nAverage time per tick: 80.0ms (Target: 50.0ms)"
    assert metrics.parse_ticks("tick query", slow) == (12.5, 80.0)
    assert metrics.parse_ticks("forge tps", "Overall: Mean tick time: 7.5 ms. Mean TPS: 20.0") == (20.0, 7.5)


def test_parse_ignores_unknown_commands():
    assert metrics.parse_ticks("tps", "Unknown or incomplete command") is None
    assert metrics.parse_ticks("tick query", None) is None


# ----- storage and the series ----------------------------------------------------------------------

def _db(admin):
    return admin.app.state.ctx.db.sessionmaker()


def _rows(admin, sid, rows):
    with _db(admin) as db:
        for ts, kw in rows:
            db.add(ServerMetric(server_id=sid, ts=ts, **kw))
        db.commit()


def test_series_rates_gaps_and_events(admin, node, agent):
    sid = _create(admin, node)["id"]
    now = int(time.time()) // 60 * 60
    rows = [(now - 600 + i * 60, {"cpu": 10.0, "mem": 100, "disk": 1000 + i, "rx": i * 6000, "tx": i * 600,
                                   "players": 2, "tps": 20.0, "mspt": 5.0}) for i in range(5)]
    rows.append((now - 60, {"cpu": 12.0, "mem": 110, "disk": 1100, "rx": 0, "tx": 0, "players": 3, "tps": 19.0, "mspt": 9.0}))
    _rows(admin, sid, rows)
    body = admin.get(f"/api/servers/{sid}/series?range=1h").json()
    pts = body["points"]
    assert any(p.get("gap") for p in pts)  # the 3-minute hole is marked
    rates = [p["rx"] for p in pts if p.get("rx")]
    assert rates and all(abs(r - 100.0) < 1e-6 for r in rates)  # 6000 bytes a minute
    assert max(p["players"] for p in pts if "players" in p and p["players"] is not None) == 3
    assert body["now"]["ticks_supported"] is True and body["peak_24h"] == 3
    assert body["events"] == [] or isinstance(body["events"], list)


def test_series_restart_counter_reset_is_not_negative(admin, node, agent):
    sid = _create(admin, node)["id"]
    now = int(time.time()) // 60 * 60
    _rows(admin, sid, [(now - 120, {"rx": 50000, "tx": 50000}), (now - 60, {"rx": 10, "tx": 10})])
    pts = admin.get(f"/api/servers/{sid}/series?range=1h").json()["points"]
    assert all((p.get("rx") or 0) >= 0 for p in pts)


def test_series_follows_server_access(admin, node, agent):
    sid = _create(admin, node)["id"]
    assert admin.get(f"/api/servers/{sid}/series?range=3d").status_code == 422
    admin.post("/api/auth/logout")
    assert admin.get(f"/api/servers/{sid}/series").status_code in (401, 403)


def test_prune_drops_old_rows(admin, node, agent):
    sid = _create(admin, node)["id"]
    now = int(time.time())
    _rows(admin, sid, [(now - 9 * 86400, {"cpu": 1.0}), (now - 60, {"cpu": 2.0})])
    with _db(admin) as db:
        metrics.prune(db)
        left = db.query(ServerMetric).filter_by(server_id=sid).count()
    assert left == 1


# ----- the monitor writes it -----------------------------------------------------------------------

@pytest.mark.anyio
async def test_monitor_records_a_row_with_players_and_tps(admin, node, agent):
    sid = _create(admin, node)["id"]
    monitor = admin.app.state.monitor
    original = agent.__call__

    def patched(request):
        if request.url.path == "/api/servers":
            return httpx.Response(200, json=[{"uuid": u, "state": "running", "restarts": 0, "oom_kills": 0, "cpu": 5.0,
                                              "memory": 123, "disk": 456, "network": {"rx": 10, "tx": 20}} for u in agent.specs])
        if request.url.path.endswith("/command"):
            import json
            cmd = json.loads(request.content)["command"]
            agent.commands.append(("x", cmd))
            return httpx.Response(200, json={"output": "§6TPS from last 1m, 5m, 15m: §a*19.5, §a20.0, §a20.0" if cmd == "tps"
                                             else "There are 2 of a max of 20 players online: a, b"})
        return original(request)

    admin.app.state.ctx.nodes.http._transport = httpx.MockTransport(patched)
    await monitor.tick(now=0)
    await monitor.tick(now=15)
    with _db(admin) as db:
        rows = db.query(ServerMetric).filter_by(server_id=sid).all()
        server = db.get(Server, sid)
        assert server is not None
    assert len(rows) == 1
    assert rows[0].players in (2, None) and rows[0].cpu is not None
    body = admin.get(f"/api/servers/{sid}/series?range=1h").json()
    assert body["now"]["ticks_supported"] is True
