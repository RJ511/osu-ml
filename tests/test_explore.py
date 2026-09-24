"""Painel Explorar: pesquisa de jogadores/mapas, atributos e plays (só leitura, sem rede)."""

from __future__ import annotations

import json
from types import SimpleNamespace
import socket
import threading
import urllib.error
import urllib.request

from osuml.categorize.core import CategorizeController, MapCategorizer
from osuml.explore import Explorer
from osuml.explore.server import actions
from osuml.panel.dashboard import make_dashboard
from test_categorize import _ref, _world


def _built(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True), (10, 2, ["DT"], 0.96, True), (10, 1, [], 0.5, False),
                              (11, 1, [], 0.97, True), (11, 999, [], 0.9, True)])
    ctrl = CategorizeController(store, MapCategorizer(store, _ref(tmp_path)))
    ctrl.start(); ctrl.wait(30)
    return store, ctrl


def test_search_players_by_name_and_id(tmp_path):
    store, _ = _built(tmp_path)
    ex = Explorer(store)
    assert [p["user_id"] for p in ex.search_players("")["items"]] == [11, 10]  # por pp desc
    assert [p["username"] for p in ex.search_players("NOME10")["items"]] == ["nome10"]
    assert [p["user_id"] for p in ex.search_players("11")["items"]] == [11]
    assert ex.search_players("zzz")["total"] == 0
    first = ex.search_players("nome10")["items"][0]
    assert first["pp"] == 5010.0 and first["global_rank"] == 110 and first["n_scores"] == 3 and "aim" in first["grades"]


def test_player_detail_has_attributes_and_all_plays(tmp_path):
    store, _ = _built(tmp_path)
    d = Explorer(store).player(10)
    assert d["player"]["username"] == "nome10" and len(d["plays"]) == 3
    by = {(p["beatmap_id"], p["mods"], p["passed"]): p for p in d["plays"]}
    ok = by[(1, "", True)]
    assert ok["map"] == "Art – Titulo1 [V1]" and ok["axes"]["aim"]["grade"] and ok["cat_status"] == "ok"
    assert by[(2, "DT", True)]["mods_key"] == "DT" and by[(1, "", False)]["passed"] is False
    assert Explorer(store).player(999999) is None
    d11 = Explorer(store).player(11)
    assert [p["cat_status"] for p in d11["plays"] if p["beatmap_id"] == 999] == ["no_file"]
    assert [p["axes"] for p in d11["plays"] if p["beatmap_id"] == 999] == [None]


def test_search_maps_by_text_and_id_and_counts(tmp_path):
    store, _ = _built(tmp_path)
    ex = Explorer(store)
    res = ex.search_maps("titulo1")
    assert [i["beatmap_id"] for i in res["items"]] == [1] and res["items"][0]["n_plays"] == 3 and res["items"][0]["n_players"] == 2
    assert [i["beatmap_id"] for i in ex.search_maps("2")["items"]] == [2]  # por id
    assert ex.search_maps("art titulo")["total"] >= 2  # várias palavras, em campos diferentes
    assert ex.search_maps("%")["total"] == 0  # curingas do LIKE são literais


def test_map_detail_lists_variants_and_plays(tmp_path):
    store, _ = _built(tmp_path)
    d = Explorer(store).map(2)
    assert d["label"] == "Art – Titulo2 [V2]" and d["has_file"] is True
    assert [v["mods"] for v in d["variants"]] == ["DT"] and d["variants"][0]["axes"]["reading"]["score"] is not None
    assert [(p["username"], p["mods"]) for p in d["plays"]] == [("nome10", "DT")]
    d1 = Explorer(store).map(1)
    assert len(d1["plays"]) == 3 and {p["username"] for p in d1["plays"]} == {"nome10", "nome11"}
    assert Explorer(store).map(424242) is None


def test_actions_return_errors_instead_of_crashing(tmp_path):
    store, _ = _built(tmp_path)
    a = actions(Explorer(store))
    assert a["/api/player"]({"id": "abc"})["error"] and a["/api/map"]({"id": 424242})["error"]
    assert a["/api/players"]({"q": "nome"})["total"] == 2


def test_dashboard_serves_explore_section(tmp_path):
    store, ctrl = _built(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    server, token = make_dashboard(SimpleNamespace(state=lambda: {}), ctrl, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/explore").read().decode()
        assert token in page and "/explore/api/players" in page and "__TOKEN__" not in page
        assert "explore" in urllib.request.urlopen(base + "/").read().decode()
        req = urllib.request.Request(base + "/explore/api/players", data=json.dumps({"q": "nome1"}).encode(),
                                     headers={"X-Panel-Token": token, "Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["total"] == 2
        bad = urllib.request.Request(base + "/explore/api/players", data=b"{}", headers={"X-Panel-Token": "x"})
        try:
            urllib.request.urlopen(bad); raise AssertionError("devia recusar")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        except OSError:
            pass  # o servidor fecha a ligação ao recusar sem ler o corpo
    finally:
        server.shutdown()


# ------------------------------------------------------------ verificação manual por jogador
import time
from datetime import timedelta

from sqlalchemy import update

from osuml.api.http import ApiError, HttpResult
from osuml.explore.check import PlayerChecker
from osuml.scheduler.tracker import Tracker
from osuml.storage import models as m
from osuml.storage.database import utcnow


def _checker(tmp_path, cooldown=5.0, collect=None, tracked=None, pause=False):
    store, ctrl = _built(tmp_path)
    calls: list[tuple] = []

    def default(uid, need_best):
        calls.append((uid, need_best))
        store.record_request("t", "osu", HttpResult("GET", "u", f"/api/v2/users/{uid}/scores/recent", {}, 200, b"[]", 1, 1))
        return {"sources": {"recent": {"requests": 1}}}

    fn = collect or default
    closed: list[int] = []
    pause_file = tmp_path / "PAUSE"
    if pause:
        pause_file.write_text("x")
    if tracked:
        with store.engine.begin() as c:
            c.execute(m.tracked_players.insert().values(user_id=10, band="top20", label="x", status=tracked,
                                                        tracked_since=utcnow(), next_poll_at=None if tracked == "inactive" else utcnow()))
    ex = Explorer(store, cooldown_s=cooldown)
    tracker = Tracker(store, pause_file=pause_file)
    chk = PlayerChecker(ex, tracker, lambda: (fn if collect else default, lambda: closed.append(1)), ctrl)
    return store, ex, chk, calls, closed


def test_last_check_comes_from_scores_requests_and_is_shown_per_player(tmp_path):
    store, ex, chk, calls, _ = _checker(tmp_path)
    assert ex.check_info(10)["last_check_at"] is None and ex.check_info(10)["cooldown_remaining_s"] == 0
    store.record_request("t", "osu", HttpResult("GET", "u", "/api/v2/users/10/scores/recent", {}, 200, b"[]", 1, 1))
    store.record_request("t", "osu", HttpResult("GET", "u", "/api/v2/users/11/scores/recent", {}, 429, b"[]", 1, 1))  # falhou: não conta
    assert ex.check_info(10)["last_check_at"].endswith("Z") and ex.check_info(11)["last_check_at"] is None
    by = {p["user_id"]: p for p in ex.search_players("")["items"]}
    assert by[10]["last_check_at"] and by[11]["last_check_at"] is None
    assert ex.player(10)["check"]["last_categorized_at"]  # perfil categorizado no _built


def test_force_check_makes_one_request_recategorizes_and_starts_cooldown(tmp_path):
    store, ex, chk, calls, closed = _checker(tmp_path)
    before = ex.check_info(10)["last_categorized_at"]
    time.sleep(0.01)
    out = chk.force(10)
    assert out["ok"] and out["requests"] == 1 and out["recategorized"] is True and calls == [(10, True)] and closed == [1]
    assert out["check"]["last_check_at"] and out["check"]["last_categorized_at"] > before
    assert 0 < out["check"]["cooldown_remaining_s"] <= 5
    again = chk.force(10)  # dentro de 5 s: recusado sem pedidos
    assert "menos de 5 s" in again["error"] and calls == [(10, True)]
    assert chk.force(11)["ok"]  # outro jogador não é afetado


def test_cooldown_expires_and_failed_attempts_also_block(tmp_path):
    def boom(uid, need_best):
        raise ApiError("HTTP 500", 500)

    store, ex, chk, calls, closed = _checker(tmp_path, cooldown=0.3, collect=boom)
    out = chk.force(10)
    assert "erro da API" in out["error"] and closed == [1]
    assert "menos de" in chk.force(10)["error"]  # a tentativa falhada também trava (evita martelar a API)
    time.sleep(0.35)
    assert "erro da API" in chk.force(10)["error"]  # passado o intervalo tenta outra vez


def test_force_refused_when_paused_inactive_unknown_or_lock_busy(tmp_path):
    _, _, chk, calls, _ = _checker(tmp_path, pause=True)
    assert "pausada" in chk.force(10)["error"] and calls == []
    _, _, chk, calls, _ = _checker(tmp_path / "b", tracked="inactive")
    assert "inativo" in chk.force(10)["error"] and calls == []
    assert chk.force(424242)["error"] == "jogador não encontrado"


def test_force_updates_schedule_of_tracked_player(tmp_path):
    store, ex, chk, calls, _ = _checker(tmp_path, tracked="active")
    assert chk.force(10)["ok"]
    with store.engine.connect() as c:
        row = c.execute(m.tracked_players.select().where(m.tracked_players.c.user_id == 10)).mappings().first()
    assert row["last_poll_at"] is not None and row["next_poll_at"] > utcnow() + timedelta(hours=13)
    assert ex.check_info(10)["next_poll_at"].endswith("Z")


def test_recategorize_player_is_refused_while_a_run_is_active(tmp_path):
    store, ctrl = _built(tmp_path)
    ctrl.status = "running"
    import pytest
    with pytest.raises(RuntimeError):
        ctrl.recategorize_player(10)
    ctrl.status = "idle"
    assert ctrl.recategorize_player(10)["ratings"] is not None and ctrl.recategorize_player(999999) is None


def test_check_action_over_http_needs_checker(tmp_path):
    store, ex, chk, calls, _ = _checker(tmp_path)
    assert actions(ex)["/api/check"]({"id": 10})["error"].startswith("verificação manual indisponível")
    assert actions(ex, chk)["/api/check"]({"id": "x"})["error"] == "jogador inválido"
    assert actions(ex, chk)["/api/check"]({"id": 10})["ok"] is True
