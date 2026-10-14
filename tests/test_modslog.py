import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import select

from osuml.recommend import log as rlog
from osuml.storage import models as m
from osuml.storage.database import Store

T0 = datetime(2026, 10, 10, 12, 0)


def _store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")


def _play(store, sid, uid, bid, passed, acc, mods, pp=None, when=None, legacy=None):
    with store.engine.begin() as c:
        c.execute(m.scores.insert().values(score_id=sid, user_id=uid, beatmap_id=bid, passed=passed, accuracy=acc, mod_acronyms=mods, pp=pp, raw={},
                                            content_sha256="x", first_source="recent", first_seen_at=T0, last_seen_at=T0, ended_at=when or T0 + timedelta(hours=1),
                                            legacy_score_id=legacy, statistics={}))


class FakeRec:
    _index = {"ids": [], "x": []}


def test_results_only_count_plays_with_exactly_the_recommended_mods(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(rlog, "_hp_of", lambda rec, bid: None)
    items = [{"beatmap_id": 10, "mods": "DT", "tier": "seguro", "p_pass": 0.6, "acc_pass": 0.92, "delta": {"aim": 3.0}, "pp_est": 150.0, "pp_gain": 2.0},
             {"beatmap_id": 20, "mods": "", "tier": "seguro", "p_pass": 0.8, "acc_pass": 0.95, "delta": {"aim": 2.0}},
             {"beatmap_id": 30, "mods": "HD", "tier": "arriscado", "p_pass": 0.5, "acc_pass": 0.9, "delta": {"aim": 1.0}}]
    assert rlog.log_recommendations(s, 1, items, ["aim"], "fp", when=T0, focus="pp") == 3
    _play(s, 1, 1, 10, False, 0.7, "DT")             # tentativa DT: falhou
    _play(s, 2, 1, 10, True, 0.93, "DT", pp=161.0)   # passou com DT
    _play(s, 3, 1, 10, True, 0.99, "", pp=120.0)     # sem mods: NÃO conta para a sugestão DT
    _play(s, 4, 1, 20, True, 0.97, "DT", pp=300.0)   # a sugestão era sem mods: a jogada DT NÃO conta
    assert rlog.link_recommendations(s, FakeRec()) == 1  # só o mapa 10 (DT) tem jogadas com os mods certos
    pl = m.prediction_log
    with s.engine.connect() as c:
        r = {row["beatmap_id"]: row for row in c.execute(select(pl)).mappings()}
    assert r[10]["mods"] == "DT" and r[10]["focus"] == "pp" and r[10]["pp_est"] == 150.0
    assert r[10]["n_attempts"] == 2 and r[10]["passed"] and r[10]["best_acc"] == 0.93 and r[10]["best_pp"] == 161.0  # o passe sem mods (99 %) ficou de fora
    assert r[20]["evaluated_at"] is None and r[30]["evaluated_at"] is None
    h = rlog.history(s, 1)
    assert {x["beatmap_id"]: x["played"] for x in h} == {10: True, 20: False, 30: False}
    rep = rlog.report_mods(s, 1)
    assert rep["DT"]["recommended"] == 1 and rep["DT"]["played"] == 1 and rep["DT"]["passed"] == (1, 1) and rep["DT"]["pp_real_vs_est"] == [161.0, 150.0]
    assert rep["NM"]["played"] == 0 and rep["HD"]["recommended"] == 1


def test_calibration_report_ignores_non_nomod_recommendations(tmp_path):
    s = _store(tmp_path)
    pl = m.prediction_log
    with s.engine.begin() as c:
        for i in range(30):
            c.execute(pl.insert().values(user_id=1, beatmap_id=i, kind="recomendacao", created_at=T0, asof=T0, evaluated_at=T0, p_pass=0.9, acc_pass=0.95,
                                          n_attempts=1, n_lazer_attempts=1, passed=False, best_acc=None, mods="DT"))
    assert rlog.report(s)["n_rows"] == 0  # 30 sugestões DT avaliadas não entram na calibração do modelo nomod


def test_existing_databases_get_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE prediction_log (id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, beatmap_id BIGINT NOT NULL, kind VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, asof DATETIME NOT NULL)")
    con.execute("INSERT INTO prediction_log (user_id, beatmap_id, kind, created_at, asof) VALUES (1, 2, 'sombra', '2026-10-01 00:00:00', '2026-10-01 00:00:00')")
    con.commit()
    con.close()
    Store(f"sqlite:///{db}", tmp_path / "raw")  # abre a base antiga: tem de acrescentar as colunas em falta sem perder linhas
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(prediction_log)")}
    assert {"mods", "focus", "pp_est", "pp_gain", "best_pp", "p_pass", "best_acc"} <= cols
    assert sqlite3.connect(db).execute("select count(*) from prediction_log").fetchone()[0] == 1
