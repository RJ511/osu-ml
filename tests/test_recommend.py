"""Recomendador: curva pp×accuracy, accuracy provável, elegibilidade (novos / repetir / tentar de novo), skills e feedback (sem rede)."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from osuml.analysis import pass_model as pm
from osuml.api.http import HttpResult
from osuml.explore import Explorer
from osuml.explore.server import actions
from osuml.recommend import Recommender, core, median_accuracy, pp_factor
from osuml.storage import models as m
from osuml.storage.database import Store, utcnow

N = 80


def test_pp_factor_is_monotonic_and_matches_the_measured_curve():
    assert pp_factor(1.0) == 1.0 and pp_factor(0.90) == pytest.approx(0.524) and pp_factor(0.99) == pytest.approx(0.893)
    xs = np.linspace(0.8, 1.0, 50)
    assert (np.diff(pp_factor(xs)) > 0).all()
    assert pp_factor(0.95) / pp_factor(0.93) - 1 == pytest.approx(0.107, abs=0.01)  # 93 → 95 % ≈ +11 % de pp


def test_median_accuracy_interpolates_where_probability_crosses_one_half():
    thr = core.THRESHOLDS
    assert median_accuracy(np.ones((1, len(thr))))[0] == pytest.approx(1.0)
    assert median_accuracy(np.zeros((1, len(thr))))[0] == pytest.approx(0.70)
    p = np.array([[0.9, 0.8, 0.6, 0.4, 0.2, 0.1]])  # cruza 0,5 entre 0.90 e 0.93
    assert median_accuracy(p)[0] == pytest.approx(0.90 + 0.03 * (0.6 - 0.5) / (0.6 - 0.4))
    assert core.monotone(np.array([[0.3, 0.5, 0.4]]))[0].tolist() == [0.3, 0.3, 0.3]  # não pode subir com o limiar


class StubPredictor:
    """P(≥ t) decresce com a distância ao nível do jogador (gap_stars) e com o limiar t."""

    def predict(self, x):
        gap = x[:, len(pm.MAP_FEATS) + len(pm.PROFILE_FEATS)]
        return np.column_stack([1 / (1 + np.exp(2 * (gap + (t - 0.85) * 30 - 2.5))) for t in core.THRESHOLDS])


def _index():
    ids = np.arange(1000, 1000 + N, dtype=np.int64)
    x = np.zeros((N, len(pm.MAP_FEATS)), dtype=np.float32)
    x[:, 0] = np.linspace(2, 9, N)          # stars
    x[:, 1] = np.linspace(1, 4.5, N)        # aim
    x[:, 2] = np.linspace(1, 4.5, N)        # speed
    x[:, 3] = np.log1p(np.linspace(0.5, 3, N))
    x[:, 4] = np.linspace(2, 6, N)
    x[:, 5:] = [9.0, 4.0, 8.0, 5.0, 400.0, 90.0]
    axis = np.tile(np.linspace(30, 110, N)[:, None], (1, 5)).astype(np.float32)  # todos os eixos sobem com o índice
    labels = {int(b): {"artist": "Artista", "title": f"Musica {i}", "version": f"Dif{i}", "creator": "Mapper", "set_id": 5000 + i // 2}
              for i, b in enumerate(ids)}
    return {"ids": ids, "x": x, "axis": axis, "labels": labels}


def _store_with_player(tmp_path, played, uid=7):
    """`played`: lista de (índice do mapa, passou, accuracy, pp)."""
    store = Store(f"sqlite:///{tmp_path}/r.db", tmp_path / "raw")
    rid = store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    objs = []
    for k, (i, passed, acc, pp) in enumerate(played):
        b = 1000 + i
        objs.append({"id": 9000 + k, "user_id": uid, "beatmap_id": b, "passed": passed, "accuracy": acc, "pp": pp, "ended_at": "2026-09-20T10:00:00Z",
                     "mods": [], "beatmap": {"id": b, "beatmapset_id": b, "version": "V", "status": "ranked",
                                             "checksum": hashlib.md5(str(b).encode()).hexdigest()},
                     "beatmapset": {"id": b, "artist": "A", "title": "T", "status": "ranked"}})
    store.ingest_scores(objs, "best", rid)
    with store.engine.begin() as c:
        c.execute(m.users.insert().values(user_id=uid, username="Teste", raw={"username": "Teste"}, first_seen_at=utcnow(), fetched_at=utcnow(),
                                          request_id=rid))
    return store


def _rec(store, tmp_path):
    return Recommender(store, tmp_path / "idx", tmp_path / "mdl", predictor=StubPredictor(), index=_index(), cf=(None, None))


def _played(extra=()):
    skip = {i for i, *_ in extra}
    return [(i, True, 0.96, 100.0 + 4 * i) for i in range(30) if i not in skip] + list(extra)


def test_recommends_new_maps_above_the_players_level_in_the_chosen_skill(tmp_path):
    r = _rec(_store_with_player(tmp_path, _played()), tmp_path).recommend(7, ["speed"], n=15)
    assert "error" not in r and r["skills"] == ["speed"] and r["items"]
    played_ids = {1000 + i for i in range(30)}
    for it in r["items"]:
        assert it["kind"] == "novo" and it["beatmap_id"] not in played_ids
        assert it["delta"]["speed"] >= core.MIN_DELTA_NEW and it["p88"] >= core.MIN_REACH_NEW
        assert "Musica" in it["title"] and it["url"].endswith(str(it["beatmap_id"])) and "≥88 %" in it["why"]
    assert len({it["beatmap_id"] for it in r["items"]}) == len(r["items"])
    assert r["player"]["levels"]["speed"] > 50


def test_a_map_tried_and_never_passed_can_come_back_as_try_again(tmp_path):
    r = _rec(_store_with_player(tmp_path, _played([(36, False, 0.6, None)])), tmp_path).recommend(7, ["speed"], n=80)
    kinds = {it["beatmap_id"]: it["kind"] for it in r["items"]}
    assert kinds.get(1036) == "tentar_de_novo"


def test_played_maps_return_only_when_the_predicted_accuracy_is_well_above_the_current_one(tmp_path):
    extra = [(24, True, 0.80, 60.0), (26, True, 0.985, 300.0)]  # 24: má accuracy num mapa alcançável; 26: já quase perfeito
    r = _rec(_store_with_player(tmp_path, _played(extra)), tmp_path).recommend(7, ["speed"], n=80)
    by_id = {it["beatmap_id"]: it for it in r["items"]}
    assert by_id[1024]["kind"] == "rejogar" and by_id[1024]["acc_cur"] == pytest.approx(0.80, abs=1e-3)
    assert by_id[1024]["pp_gain_pct"] >= core.MIN_PP_GAIN_PCT and "já jogaste" in by_id[1024]["why"]
    assert 1026 not in by_id  # sem margem de melhoria previsível: não é repetido
    for x in r["items"]:
        if x["kind"] == "rejogar":
            assert x["acc_pred"] - x["acc_cur"] >= core.MIN_ACC_GAIN and x["pp_gain_pct"] >= core.MIN_PP_GAIN_PCT
    assert r["counts"]["rejogar"] >= 1


def test_invalid_requests_and_missing_data_return_clear_errors(tmp_path):
    rec = _rec(_store_with_player(tmp_path, _played()), tmp_path)
    assert "skill" in rec.recommend(7, [], n=5)["error"] and "skill" in rec.recommend(7, ["stars", "xyz"], n=5)["error"]
    assert "sem scores" in rec.recommend(999, ["speed"], n=5)["error"]
    few = _rec(_store_with_player(tmp_path / "b", [(i, True, 0.95, 50.0) for i in range(5)]), tmp_path)
    assert "poucos passes" in few.recommend(7, ["aim"])["error"]
    not_ready = Recommender(_store_with_player(tmp_path / "c", _played()), tmp_path / "nada", tmp_path / "nada2")
    assert "índice" in not_ready.recommend(7, ["aim"])["error"]


def test_feedback_is_saved_and_exposed_through_the_explore_actions(tmp_path):
    from sqlalchemy import select

    store = _store_with_player(tmp_path, _played())
    act = actions(Explorer(store), None, _rec(store, tmp_path))
    r = act["/api/recommend"]({"id": 7, "skills": ["speed", "reading"]})
    assert r["items"] and set(r["skills"]) == {"speed", "reading"}
    first = r["items"][0]
    assert act["/api/rec_feedback"]({"id": 7, "beatmap_id": first["beatmap_id"], "verdict": "serve", "skills": ["speed"], "kind": first["kind"],
                                     "score": first["score"]})["ok"]
    assert act["/api/rec_feedback"]({"id": 7, "beatmap_id": first["beatmap_id"], "verdict": "talvez"})["error"]
    with store.engine.connect() as c:
        rows = c.execute(select(m.recommendation_feedback)).mappings().all()
    assert len(rows) == 1 and rows[0]["verdict"] == "serve" and rows[0]["skills"] == "speed"
    assert actions(Explorer(store))["/api/recommend"]({"id": 7, "skills": ["speed"]})["error"].startswith("recomendador indisponível")


def test_feedback_is_also_written_to_a_readable_text_file(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = Recommender(store, tmp_path / "idx", tmp_path / "mdl", predictor=StubPredictor(), index=_index(), cf=(None, None),
                      feedback_file=tmp_path / "fb" / "feedback.txt")
    assert rec.feedback(7, 1040, "serve", ["speed", "aim"], "novo", 0.8123, "boa\tnota\ncom quebras")["ok"]
    assert rec.feedback(7, 1041, "nao_serve", ["speed"], "rejogar", None)["ok"]
    lines = (tmp_path / "fb" / "feedback.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].split(chr(9)) == list(Recommender.FEEDBACK_HEADER)  # cabeçalho, uma só vez
    a, b = (ln.split(chr(9)) for ln in lines[1:])
    assert len(lines) == 3 and len(a) == len(Recommender.FEEDBACK_HEADER)  # tabs/quebras nos textos não partem as colunas
    assert a[1] == "Teste" and a[3] == "1040" and a[4] == "5020" and a[5] == "Artista - Musica 40 [Dif40]" and a[6] == "serve" and a[8] == "speed,aim" and a[9] == "0.8123"
    assert a[10] == "boa nota com quebras" and a[11] == "https://osu.ppy.sh/beatmapsets/5020#osu/1040" and b[6] == "nao_serve" and b[9] == ""


def test_players_can_be_found_by_name_ignoring_case_or_by_id(tmp_path):
    rec = _rec(_store_with_player(tmp_path, _played()), tmp_path)
    assert [p["username"] for p in rec.players()] == ["Teste"] and rec.players()[0]["n_scores"] == 30
    assert rec.find_player("teste") == (7, "Teste") and rec.find_player("7") == (7, "Teste") and rec.find_player("outro") is None


def test_the_local_app_recommends_by_player_name_and_saves_feedback(tmp_path):
    from osuml.recommend.app import build_app

    store = _store_with_player(tmp_path, _played())
    rec = Recommender(store, tmp_path / "idx", tmp_path / "mdl", predictor=StubPredictor(), index=_index(), cf=(None, None),
                      feedback_file=tmp_path / "fb.txt")
    server, _ = build_app(rec, 0)
    try:
        acts = server.RequestHandlerClass  # o servidor liga-se a uma porta livre; testamos a lógica pelo Recommender, não pela rede
    finally:
        server.server_close()
    assert acts is not None
    from osuml.recommend import app as appmod

    # a lógica das ações vive em closures: exercitamos-a por uma segunda instância só para obter as ações
    from osuml.panel import server as ps

    captured = {}
    orig = ps.make_generic_server
    ps.make_generic_server = lambda page, state, actions, port: captured.update(state=state, actions=actions) or (None, "t")
    try:
        appmod.build_app(rec, 1)
    finally:
        ps.make_generic_server = orig
    st = captured["state"]()
    assert st["players"][0]["username"] == "Teste" and st["feedback_file"].endswith("fb.txt")
    r = captured["actions"]["/api/recommend"]({"player": "TESTE", "skills": ["speed"]})
    assert r["items"] and r["player"]["username"] == "Teste"
    assert "não encontrado" in captured["actions"]["/api/recommend"]({"player": "x", "skills": ["speed"]})["error"]
    first = r["items"][0]
    assert captured["actions"]["/api/feedback"]({"player": "Teste", "beatmap_id": first["beatmap_id"], "verdict": "serve", "skills": ["speed"],
                                                 "kind": first["kind"], "score": first["score"]})["ok"]
    assert (tmp_path / "fb.txt").read_text(encoding="utf-8").count(chr(10)) == 2


def test_pack_contains_only_the_requested_players_and_minimal_columns(tmp_path):
    import zipfile

    from sqlalchemy import select

    from osuml.recommend.pack import build_pack

    store = _store_with_player(tmp_path, _played(), uid=7)
    idx, mdl = tmp_path / "idx", tmp_path / "mdl"
    idx.mkdir(), mdl.mkdir()
    for f in ("index.npz", "meta.json", "cf_matrix.npz", "cf_users.npy", "labels.parquet"):
        (idx / f).write_bytes(b"x")
    for t in core.THRESHOLDS:
        (mdl / f"reach_acc{int(round(t * 100))}_A.txt").write_text("m")
    out = build_pack(store, idx, mdl, tmp_path / "dist" / "pack.zip", ["teste"])
    assert out["players"] == {"Teste": 30}
    with zipfile.ZipFile(out["zip"]) as z:
        names = set(z.namelist())
        assert {"pack/players.db", "pack/index/index.npz", "pack/models/reach_acc88_A.txt", "pack/LEIA-ME.txt", "pack/manifest.json"} <= names
        assert not any("raw" in n for n in names)
        z.extract("pack/players.db", tmp_path / "x")
    from osuml.storage.database import Store

    s2 = Store(f"sqlite:///{(tmp_path / 'x' / 'pack' / 'players.db').as_posix()}", tmp_path / "x" / "raw")
    with s2.engine.connect() as c:
        rows = c.execute(select(m.scores)).mappings().all()
        assert len(rows) == 30 and all(r["raw"] == {} and r["content_sha256"] == "" for r in rows)
        assert c.execute(select(m.users.c.raw)).scalar() == {"username": "Teste"}
    import pytest as _pt

    with _pt.raises(ValueError, match="não estão na base"):
        build_pack(store, idx, mdl, tmp_path / "dist" / "p2.zip", ["ninguem"])


def test_model_info_identifies_the_loaded_model_and_pack_records_the_training(tmp_path):
    import json

    from osuml.recommend.pack import build_pack

    store = _store_with_player(tmp_path, _played())
    idx, mdl = tmp_path / "idx", tmp_path / "mdl"
    idx.mkdir(), mdl.mkdir()
    for f in ("index.npz", "meta.json"):
        (idx / f).write_bytes(b"x")
    for t in core.THRESHOLDS:
        (mdl / f"reach_acc{int(round(t * 100))}_A.txt").write_text("modelo-v1")
    res = tmp_path / "results.json"
    res.write_text(json.dumps({"created_at": "2026-09-24", "data": {"players": 61000, "pairs_with_catalog": 5, "train_rows": 4, "test_rows": 3, "test_players": 2},
                               "results": {"acc88": {"A": {"all": {"auc": 0.81}}}}}), encoding="utf-8")
    out = build_pack(store, idx, mdl, tmp_path / "p.zip", ["Teste"], training_results=res)
    assert out["training"]["players"] == 61000 and out["training"]["auc_by_threshold"] == {"acc88": 0.81}
    rec = Recommender(store, idx, mdl, predictor=StubPredictor(), index=_index(), cf=(None, None))
    a = rec.model_info()["fingerprint"]
    (mdl / "reach_acc88_A.txt").write_text("modelo-v2")  # outro modelo => outra impressão digital
    assert rec.model_info()["fingerprint"] != a and rec.model_info()["n_models"] == 6


def test_recommendation_links_point_to_the_specific_difficulty_and_carry_the_ids(tmp_path):
    assert core.beatmap_url(4834695, 2269930) == "https://osu.ppy.sh/beatmapsets/2269930#osu/4834695"
    assert core.beatmap_url(4834695) == "https://osu.ppy.sh/beatmaps/4834695" and core.beatmap_url(7, 0) == "https://osu.ppy.sh/beatmaps/7"
    r = _rec(_store_with_player(tmp_path, _played()), tmp_path).recommend(7, ["speed"], n=5)
    for it in r["items"]:
        assert it["url"] == f"https://osu.ppy.sh/beatmapsets/{it['beatmapset_id']}#osu/{it['beatmap_id']}"
