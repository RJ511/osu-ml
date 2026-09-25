"""Registo de previsões e comparação automática previsão/realidade (avaliação-sombra + recomendações jogadas)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from osuml.api.http import HttpResult
from osuml.recommend import Recommender
from osuml.recommend.log import evaluate_pending, log_recommendations, outcome, report
from osuml.storage import models as m
from osuml.storage.database import Store, utcnow

from tests.test_recommend import StubPredictor, _index


def _score(sid, b, ended, passed=True, acc=0.96, legacy=None, stats=None, uid=7):
    return {"id": sid, "user_id": uid, "beatmap_id": 1000 + b, "passed": passed, "accuracy": acc, "pp": 120.0 if passed else None, "rank": "A" if passed else "F",
            "ended_at": ended.isoformat() + "Z", "mods": [], "legacy_score_id": legacy, "statistics": stats or {}, "maximum_statistics": {"great": 300},
            "beatmap": {"id": 1000 + b, "beatmapset_id": b, "version": "V", "status": "ranked", "checksum": hashlib.md5(str(b).encode()).hexdigest()},
            "beatmapset": {"id": b, "artist": "A", "title": "T", "status": "ranked"}}


def _world(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/l.db", tmp_path / "raw")
    rid = store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    t0 = datetime(2026, 9, 1, 12, 0, 0)
    old = [_score(100 + i, i, t0 + timedelta(hours=i)) for i in range(30)]  # 30 passes antigos: dão o perfil
    store.ingest_scores(old, "best", rid)
    with store.engine.begin() as c:
        c.execute(m.users.insert().values(user_id=7, username="Teste", raw={}, first_seen_at=utcnow(), fetched_at=utcnow(), request_id=rid))
    rec = Recommender(store, tmp_path / "i", tmp_path / "m", predictor=StubPredictor(), index=_index(), cf=(None, None), log_predictions=True)
    return store, rid, rec, t0


def test_outcome_counts_attempts_first_try_and_separates_restarts_from_possible_deaths():
    end = datetime(2026, 9, 24, 12, 0)
    plays = [(False, 0.9, {"great": 20, "miss": 1}, "", None, end),                 # 1 miss: reinício certo
             (False, 0.8, {"great": 100, "miss": 12}, "", None, end + timedelta(minutes=1)),  # 12 misses (HP 5): pode ser morte
             (True, 0.95, {}, "", None, end + timedelta(minutes=2))]
    oc = outcome(plays, hp=5.0)
    assert oc["n_attempts"] == 3 and oc["n_lazer_attempts"] == 3 and oc["passed"] is True and oc["first_try_passed"] is False
    assert oc["best_acc"] == 0.95 and oc["n_restarts"] == 1 and oc["n_deaths_possible"] == 1
    legacy_only = outcome([(True, 0.97, {}, "", 55, end)], hp=5.0)  # stable: passou, mas as falhas não existem => sem 1.ª tentativa
    assert legacy_only["passed"] is True and legacy_only["first_try_passed"] is None and legacy_only["n_lazer_attempts"] == 0


def test_shadow_evaluation_compares_what_the_model_would_have_said_before_the_new_plays(tmp_path):
    store, rid, rec, t0 = _world(tmp_path)
    assert evaluate_pending(store, rec)["pairs"] == 0  # 1.ª passagem: só começa a seguir o jogador (não avalia o histórico)
    new = [_score(500, 40, t0 + timedelta(days=5), passed=False, acc=0.7, stats={"great": 50, "miss": 2}),
           _score(501, 40, t0 + timedelta(days=5, minutes=3), passed=True, acc=0.94), _score(502, 45, t0 + timedelta(days=5, minutes=9), passed=False, acc=0.5, stats={"great": 5})]
    store.ingest_scores(new, "recent", rid)
    stats = evaluate_pending(store, rec)
    assert stats["pairs"] == 2 and stats["skipped"] == 0
    with store.engine.connect() as c:
        rows = {r["beatmap_id"]: r for r in c.execute(select(m.prediction_log)).mappings().all()}
    a, b = rows[1040], rows[1045]
    assert a["kind"] == "sombra" and a["asof"] == t0 + timedelta(days=5) and 0 < a["p_pass"] < 1 and a["acc_pass"] > 0.8  # previsão do stub
    assert a["n_attempts"] == 2 and a["passed"] and a["first_try_passed"] is False and a["best_acc"] == pytest.approx(0.94) and a["n_restarts"] == 1
    assert b["passed"] is False and b["best_acc"] is None and b["challenge"] is not None
    assert evaluate_pending(store, rec)["pairs"] == 0  # já avaliado: não duplica


def test_a_recommended_map_played_later_gets_its_outcome_and_the_report_compares(tmp_path):
    store, rid, rec, t0 = _world(tmp_path)
    r = rec.recommend(7, ["speed"], n=5)
    assert r["items"]
    with store.engine.connect() as c:
        logged = c.execute(select(m.prediction_log).where(m.prediction_log.c.kind == "recomendacao")).mappings().all()
    assert len(logged) == len(r["items"]) and all(x["evaluated_at"] is None for x in logged) and logged[0]["p_pass"] == pytest.approx(r["items"][0]["p_pass"])
    bid = r["items"][0]["beatmap_id"] - 1000
    store.ingest_scores([_score(700, bid, utcnow() + timedelta(minutes=5), passed=True, acc=0.93)], "recent", rid)
    out = evaluate_pending(store, rec)
    assert out["linked_recommendations"] == 1
    rep = report(store)
    assert rep["n_rows"] >= 1 and rep["recommended"]["n"] == 1 and rep["recommended"]["passed"] == 1.0
    # log_recommendations aceita listas vazias e devolve 0
    assert log_recommendations(store, 7, [], ["speed"], "x") == 0
