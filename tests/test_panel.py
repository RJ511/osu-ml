"""Painel de recolhas: fila, cancelamento, orçamento, intervalo medido e bloqueio (sem rede)."""

from __future__ import annotations

import json

import httpx
import pytest

from osuml.api.http import Cancelled
from osuml.api.lock import ApiLock
from osuml.api.osu import OsuClient
from osuml.panel.core import JOB_REQUEST_BUDGET, MIN_ALLOWED_INTERVAL, PanelController, RequestLog
from osuml.storage.database import Store


def _event(start: float, path: str = "/api/v2/x") -> dict:
    return {"start": start, "duration_ms": 100, "status": 200, "method": "GET", "path": path, "client": "osu"}


def _panel(tmp_path, runner_cls, n_jobs: int = 3):
    store = Store(f"sqlite:///{tmp_path}/p.db", tmp_path / "raw")
    entries = [{"user_id": 100 + i, "band": "top20", "rank_at_snapshot": i + 1} for i in range(n_jobs)]
    entries.append({"user_id": 13745526, "band": "nomeado", "name": "PXD Vieira"})  # excluído do painel
    pf = tmp_path / "panel.json"
    pf.write_text(json.dumps(entries), encoding="utf-8")
    ctrl = PanelController(store, pf, lambda c: runner_cls(c), lock_path=tmp_path / "api.lock", lock_settle=0)
    return ctrl


class _Ok:
    def __init__(self, ctrl): self.ctrl = ctrl
    def run_job(self, uid):
        for i in range(3):
            if self.ctrl._cancel_check():
                raise Cancelled()
            self.ctrl._observe(_event(1000.0 + uid * 10 + i * 1.2))
        return {"status": "ok", "dataset": {"unique_scores": 7}}
    def close(self): pass


def test_request_log_flags_gaps_below_minimum():
    log = RequestLog()
    log.add(_event(0.0), 1.1, None)
    log.add(_event(1.2), 1.1, None)  # 1200 ms: ok
    row = log.add(_event(1.5), 1.1, None)  # 300 ms: violação
    assert row["gap_ms"] == 300 and row["violation"] is True
    snap = log.snapshot(now=2.0)
    assert snap["total"] == 3 and snap["violations"] == 1 and snap["min_gap_ms"] == 300 and snap["last_60s"] == 3


def test_runs_queued_jobs_in_order_and_excludes_pxd(tmp_path):
    ctrl = _panel(tmp_path, _Ok)
    assert [j["user_id"] for j in ctrl.jobs()] == [100, 101, 102]
    assert ctrl.start() is None
    ctrl.wait(10)
    jobs = ctrl.jobs()
    assert [j["status"] for j in jobs] == ["done"] * 3 and all(j["requests"] == 3 for j in jobs)
    assert jobs[0]["scores_total"] == 7 and ctrl.status == "idle"


def test_start_refuses_interval_below_official_limit(tmp_path):
    ctrl = _panel(tmp_path, _Ok)
    assert "60 pedidos/min" in ctrl.start(min_interval=0.5)
    assert ctrl.log.total == 0 and ctrl.status == "idle"


def test_start_refuses_when_another_process_holds_the_api_lock(tmp_path):
    ctrl = _panel(tmp_path, _Ok)
    other = ApiLock(tmp_path / "api.lock")
    assert other.acquire()
    try:
        assert "outra recolha" in ctrl.start()
    finally:
        other.release()
    assert ctrl.start() is None
    ctrl.wait(10)


def test_cancel_all_stops_current_and_cancels_future_jobs(tmp_path):
    class Cancels(_Ok):
        def run_job(self, uid):
            if uid == 101:
                self.ctrl.cancel_all()
            return super().run_job(uid)

    ctrl = _panel(tmp_path, Cancels)
    ctrl.start()
    ctrl.wait(10)
    assert [j["status"] for j in ctrl.jobs()] == ["done", "cancelled", "cancelled"]
    assert ctrl.log.total == 3  # só o 1.º jogador enviou pedidos; depois do cancelamento, nenhum


def test_cancel_current_job_only(tmp_path):
    class CancelsOne(_Ok):
        def run_job(self, uid):
            if uid == 100:
                self.ctrl.cancel_job(100)
            return super().run_job(uid)

    ctrl = _panel(tmp_path, CancelsOne)
    ctrl.start()
    ctrl.wait(10)
    assert [j["status"] for j in ctrl.jobs()] == ["cancelled", "done", "done"]


def test_per_job_request_budget_aborts_runaway_pagination(tmp_path):
    class Runaway(_Ok):
        def run_job(self, uid):
            for i in range(100):
                if self.ctrl._cancel_check():
                    raise Cancelled()
                self.ctrl._observe(_event(2000.0 + i * 1.2))
            return {"status": "ok"}

    ctrl = _panel(tmp_path, Runaway, n_jobs=1)
    ctrl.start()
    ctrl.wait(10)
    job = ctrl.jobs()[0]
    assert job["status"] == "failed" and "orçamento" in job["error"] and job["requests"] == JOB_REQUEST_BUDGET


def test_restart_requeues_cancelled_but_not_done(tmp_path):
    class CancelsOne(_Ok):
        def run_job(self, uid):
            if uid == 101:
                self.ctrl.cancel_all()
            return super().run_job(uid)

    ctrl = _panel(tmp_path, CancelsOne)
    ctrl.start(); ctrl.wait(10)
    assert [j["status"] for j in ctrl.jobs()] == ["done", "cancelled", "cancelled"]
    ctrl._make_runner = lambda c: _Ok(c)
    assert ctrl.start() is None
    ctrl.wait(10)
    assert [j["status"] for j in ctrl.jobs()] == ["done", "done", "done"]
    assert ctrl.log.total == 3 + 6  # o 1.º não foi repetido


def test_http_client_cancel_check_sends_nothing_and_observer_sees_token_and_calls():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400, "token_type": "Bearer"})
        return httpx.Response(200, json={"id": 1, "username": "x"})

    osu = OsuClient("1", "secret", user_agent="t", transport=httpx.MockTransport(handler), min_interval=0.01,
                    sleep=lambda s: None)
    events: list[dict] = []
    osu.http.observer = events.append
    osu.http.cancel_check = lambda: True
    with pytest.raises(Cancelled):
        osu.get_user("x")
    assert calls == [] and events == []

    osu.http.cancel_check = lambda: False
    osu.get_user("x")
    assert calls == ["/oauth/token", "/api/v2/users/@x"]
    assert [e["path"] for e in events] == ["/oauth/token", "/api/v2/users/@x"] and all("start" in e for e in events)
    osu.close()


def test_min_allowed_interval_is_the_official_limit():
    assert MIN_ALLOWED_INTERVAL >= 60 / 60  # nunca mais de 60 pedidos por minuto


def test_actions_returning_none_are_ok_not_404_and_unknown_actions_are_404(tmp_path):
    """Regressão: ao refatorar o servidor, `cancel` (devolve None) passou a responder 404."""
    import threading
    import urllib.error
    import urllib.request

    from osuml.panel.server import make_multi_server

    import socket

    with socket.socket() as sock:  # porta livre concreta: o filtro de Host usa a porta pedida
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    hits: list[str] = []
    server, token = make_multi_server(lambda names: "<html></html>",
                                      {"sec": ("<html>__TOKEN__</html>", lambda: {"ok": 1}, {"/api/cancel": lambda b: hits.append("cancel")})},
                                      free_port)
    port = free_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                     headers={"X-Panel-Token": token, "Content-Type": "application/json",
                                              "Host": f"127.0.0.1:{port}"})
        return urllib.request.urlopen(req)

    try:
        assert post("/sec/api/cancel").status == 200 and hits == ["cancel"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/sec/api/nao_existe")
        assert exc.value.code == 404
    finally:
        server.shutdown()
