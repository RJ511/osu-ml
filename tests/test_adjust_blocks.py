"""Correção por jogador, recalibração periódica, mapas bloqueados e pesquisa de mapas do catálogo no Explorar."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select

from osuml.explore import Explorer
from osuml.explore.server import actions
from osuml.recommend import Recommender
from osuml.recommend.adjust import (K_ACC, apply_adjustment, compute_adjustments, load_adjustments, recalibrate, refresh_adjustments,
                                    save_adjustments)
from osuml.recommend.core import parse_map_ref
from osuml.storage import models as m
from osuml.storage.database import Store, utcnow

from tests.test_recommend import StubPredictor, _index, _played, _rec, _store_with_player

T0 = datetime(2026, 9, 10, 12, 0, 0)


def _log_rows(store, uid, n, *, acc_err=0.0, passed_frac=1.0, p=0.8, lazer=True):
    """n linhas de avaliação-sombra do jogador: real = prevista − acc_err (acc_err positivo: o modelo é otimista); passou em `passed_frac` dos pares."""
    rows = []
    for i in range(n):
        ok = (i % 10) < round(passed_frac * 10)
        rows.append({"user_id": uid, "beatmap_id": 5000 + i, "kind": "sombra", "created_at": T0, "asof": T0 + timedelta(hours=i), "evaluated_at": T0,
                     "p_pass": p, "p_pass_raw": p, "acc_pass": 0.95, "acc_pass_raw": 0.95, "challenge": 1.0, "n_attempts": 2, "n_lazer_attempts": 2 if lazer else 0,
                     "passed": ok, "best_acc": 0.95 - acc_err if ok else None})
    with store.engine.begin() as c:
        c.execute(m.prediction_log.insert(), rows)


def _empty_store(tmp_path):
    return Store(f"sqlite:///{tmp_path}/a.db", tmp_path / "raw")


# ------------------------------------------------------------------ correção por jogador
def test_player_bias_is_shrunk_toward_zero_and_needs_enough_pairs(tmp_path):
    store = _empty_store(tmp_path)
    _log_rows(store, 1, 40, acc_err=0.05)   # previsto 5 pontos acima do real em 40 pares (só as que passaram têm accuracy: 100 % passam)
    _log_rows(store, 2, 3, acc_err=0.05)    # poucos pares: sem correção
    adj = compute_adjustments(store)
    assert adj[1]["n_acc"] == 40 and adj[1]["acc_bias"] == pytest.approx(40 * 0.05 / (40 + K_ACC), abs=1e-4)  # 0,04 (não 0,05: encolhido)
    assert 2 not in adj
    _log_rows(store, 3, 200, acc_err=0.05)
    assert compute_adjustments(store)[3]["acc_bias"] > adj[1]["acc_bias"]  # com mais dados encolhe menos


def test_pass_offset_is_negative_when_the_player_passes_less_than_predicted_and_positive_otherwise(tmp_path):
    store = _empty_store(tmp_path)
    _log_rows(store, 1, 60, passed_frac=0.4, p=0.8)
    _log_rows(store, 2, 60, passed_frac=1.0, p=0.5)
    _log_rows(store, 3, 60, passed_frac=0.5, p=0.5)
    _log_rows(store, 4, 60, passed_frac=0.2, p=0.8, lazer=False)  # sem tentativas do lazer (o stable não envia falhas): não conta
    adj = compute_adjustments(store)
    assert adj[1]["pass_offset"] < -0.3 and adj[2]["pass_offset"] > 0.3 and abs(adj.get(3, {}).get("pass_offset", 0.0)) < 0.1
    assert adj.get(4, {}).get("pass_offset", 0.0) == 0.0 and adj[1]["n_pass"] == 60


def test_apply_adjustment_lowers_accuracy_and_probability_and_is_identity_without_data():
    p, a = apply_adjustment(np.array([0.8, 0.9]), np.array([0.95, 0.96]), {"acc_bias": 0.03, "pass_offset": -0.5})
    assert np.all(p < np.array([0.8, 0.9])) and a == pytest.approx([0.92, 0.93])
    p0, a0 = apply_adjustment(np.array([0.8]), np.array([0.95]), None)
    assert p0[0] == 0.8 and a0[0] == 0.95
    assert np.all(apply_adjustment(np.array([0.8]), np.array([0.01]), {"acc_bias": 0.5})[1] == 0.0)  # nunca abaixo de 0


def test_adjustments_use_the_current_global_calibration(tmp_path):
    store = _empty_store(tmp_path)
    models = tmp_path / "mdl"
    models.mkdir()
    _log_rows(store, 1, 40, acc_err=0.0)     # acc bruta 0,95 = real 0,95
    assert compute_adjustments(store, models).get(1) is None or compute_adjustments(store, models)[1]["acc_bias"] == 0  # sem viés: nada a corrigir
    (models / "calibration_pass_acc.json").write_text(json.dumps({"pass": {"a": 0.0, "b": 1.0}, "acc_shift": -0.02}), encoding="utf-8")
    assert compute_adjustments(store, models)[1]["acc_bias"] == pytest.approx(-40 * 0.02 / (40 + K_ACC), abs=1e-4)  # a calibração global já tirou 2 pts: sobra −2


def test_the_recommender_applies_the_player_adjustment_but_logs_the_model_values(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = Recommender(store, tmp_path / "idx", tmp_path / "mdl", predictor=StubPredictor(), index=_index(), cf=(None, None), log_predictions=True)
    (tmp_path / "mdl").mkdir()
    base = rec.recommend(7, ["speed"], n=10)
    save_adjustments(tmp_path / "mdl", {7: {"n_acc": 50, "acc_bias": 0.02, "n_pass": 50, "pass_offset": -0.3}})
    adj = rec.recommend(7, ["speed"], n=10)  # o ficheiro mudou: é relido sem reiniciar
    b0 = {it["beatmap_id"]: it for it in base["items"]}
    common = [it for it in adj["items"] if it["beatmap_id"] in b0]
    assert common and adj["player"]["adjust"]["acc_bias_pts"] == pytest.approx(2.0)
    for it in common:
        assert it["acc_pass"] == pytest.approx(b0[it["beatmap_id"]]["acc_pass"] - 0.02, abs=2e-4) and it["p_pass"] < b0[it["beatmap_id"]]["p_pass"]
        assert it["p_pass_model"] == pytest.approx(b0[it["beatmap_id"]]["p_pass"], abs=2e-3)  # valores do modelo, sem a correção
    assert any("correção por jogador" in n for n in adj["notes"]) and base["player"]["adjust"] is None
    with store.engine.connect() as c:  # o registo guarda os valores do modelo (senão a estimativa da correção seria circular)
        rows = c.execute(select(m.prediction_log).where(m.prediction_log.c.kind == "recomendacao")).mappings().all()
    by_id = {it["beatmap_id"]: it for it in adj["items"]}
    checked = [r for r in rows if r["beatmap_id"] in by_id]
    assert checked and all(abs(r["acc_pass"] - by_id[r["beatmap_id"]]["acc_pass_model"]) < 1e-9 for r in checked)
    assert all(by_id[r["beatmap_id"]]["acc_pass"] < r["acc_pass"] for r in checked)  # o que se mostrou é mais baixo que o que se registou


def test_a_big_negative_adjustment_can_empty_the_safe_list(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = _rec(store, tmp_path)
    (tmp_path / "mdl").mkdir()
    save_adjustments(tmp_path / "mdl", {7: {"n_acc": 100, "acc_bias": 0.2, "n_pass": 0, "pass_offset": 0.0}})  # o modelo é 20 pontos otimista neste jogador
    assert rec.recommend(7, ["speed"], n=10)["items"] == []  # nenhum mapa chega a 88 % esperado


def test_refresh_writes_only_when_asked_and_the_pack_only_carries_the_pack_players(tmp_path):
    store = _empty_store(tmp_path)
    _log_rows(store, 1, 40, acc_err=0.05)
    _log_rows(store, 2, 40, acc_err=0.05)
    (tmp_path / "mdl").mkdir()
    assert refresh_adjustments(store, tmp_path / "mdl") == {"players": 2}
    assert set(load_adjustments(tmp_path / "mdl")) == {1, 2}
    save_adjustments(tmp_path / "mdl", compute_adjustments(store), only={2})
    assert set(load_adjustments(tmp_path / "mdl")) == {2}


# ------------------------------------------------------------------ recalibração
def _miscalibrated_log(store, n_users=40, per_user=25, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for u in range(n_users):
        for i in range(per_user):
            praw = float(rng.uniform(0.2, 0.95))
            true = 1 / (1 + np.exp(-(1.2 + 1.0 * np.log(praw / (1 - praw)))))  # o modelo bruto subestima
            ok = bool(rng.random() < true)
            rows.append({"user_id": 100 + u, "beatmap_id": 9000 + i, "kind": "sombra", "created_at": T0, "asof": T0, "evaluated_at": T0, "p_pass": praw, "p_pass_raw": praw,
                         "acc_pass": 0.95, "acc_pass_raw": 0.95, "n_attempts": 1, "n_lazer_attempts": 1, "passed": ok, "best_acc": 0.95 if ok else None})
    with store.engine.begin() as c:
        c.execute(m.prediction_log.insert(), rows)


def test_recalibrate_refuses_with_little_data_and_never_writes_in_simulation(tmp_path):
    store = _empty_store(tmp_path)
    (tmp_path / "mdl").mkdir()
    _log_rows(store, 1, 30)
    out = recalibrate(store, tmp_path / "mdl", apply=True)
    assert out["applied"] is False and "poucos dados" in out["reason"] and not (tmp_path / "mdl" / "calibration_pass_acc.json").exists()


def test_recalibrate_replaces_a_bad_calibration_only_with_apply_and_keeps_a_backup(tmp_path):
    store = _empty_store(tmp_path)
    models = tmp_path / "mdl"
    models.mkdir()
    cal = models / "calibration_pass_acc.json"
    cal.write_text(json.dumps({"pass": {"a": 0.0, "b": 1.0}, "acc_shift": 0.0, "n_pairs": 1}), encoding="utf-8")  # identidade: o modelo bruto subestima
    _miscalibrated_log(store)
    sim = recalibrate(store, models)
    assert sim["improves"]["pass"] is True and sim["applied"] is False and json.loads(cal.read_text(encoding="utf-8"))["pass"]["a"] == 0.0
    done = recalibrate(store, models, apply=True)
    assert done["applied"] is True
    new = json.loads(cal.read_text(encoding="utf-8"))
    assert new["pass"]["a"] == pytest.approx(1.2, abs=0.35) and new["recalibrated_from_log"]["players"] == 40
    assert list(models.glob("calibration_pass_acc.json.bak-*"))
    assert recalibrate(store, models, apply=True)["applied"] is False  # já calibrada: nada a fazer


# ------------------------------------------------------------------ mapas bloqueados
def _first_set(res):
    return res["items"][0]["beatmapset_id"]


def test_blocking_a_map_removes_all_its_difficulties_but_a_difficulty_block_only_that_one(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = _rec(store, tmp_path)
    before = rec.recommend(7, ["speed"], n=15)
    first = before["items"][0]
    sibling = {it["beatmap_id"] for it in before["items"] if it["beatmapset_id"] == first["beatmapset_id"]}
    out = rec.block_map(7, beatmap_id=first["beatmap_id"], scope="set")
    assert out["ok"] and out["scope"] == "set" and out["beatmapset_id"] == first["beatmapset_id"] and not out["already"]
    after = rec.recommend(7, ["speed"], n=15)
    assert not sibling & {it["beatmap_id"] for it in after["items"]} and after["player"]["n_blocked"] >= len(sibling)
    assert {it["beatmap_id"] for it in before["items"]} - sibling <= {it["beatmap_id"] for it in after["items"]}  # o resto da lista mantém-se
    # só a dificuldade
    second = after["items"][0]
    rec.block_map(7, beatmap_id=second["beatmap_id"], scope="diff")
    third = rec.recommend(7, ["speed"], n=15)
    assert second["beatmap_id"] not in {it["beatmap_id"] for it in third["items"]}


def test_blocks_are_per_player_deduplicated_listed_and_can_be_removed_and_are_written_to_the_text_file(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = Recommender(store, tmp_path / "idx", tmp_path / "mdl", predictor=StubPredictor(), index=_index(), cf=(None, None), feedback_file=tmp_path / "fb.txt")
    first = rec.recommend(7, ["speed"], n=5)["items"][0]
    a = rec.block_map(7, beatmap_id=first["beatmap_id"])
    assert rec.block_map(7, beatmap_id=first["beatmap_id"])["already"] is True and len(rec.blocks(7)) == 1
    b = rec.blocks(7)[0]
    assert b["scope"] == "set" and b["url"].endswith(f"beatmapsets/{a['beatmapset_id']}") and "Musica" in b["label"]
    assert rec.blocks(999) == [] and rec.unblock(999, b["id"])["error"]  # outro jogador não vê nem apaga
    assert rec.unblock(7, b["id"])["ok"] and rec.blocks(7) == []
    assert first["beatmap_id"] in {it["beatmap_id"] for it in rec.recommend(7, ["speed"], n=15)["items"]}
    lines = (tmp_path / "fb.txt").read_text(encoding="utf-8").splitlines()
    cols = [ln.split(chr(9)) for ln in lines[1:]]
    assert [c[6] for c in cols] == ["bloquear_mapa", "desbloquear"] and cols[0][4] == str(a["beatmapset_id"]) and cols[0][11].startswith("https://osu.ppy.sh/beatmapsets/")


def test_blocking_by_set_id_only_or_with_bad_input(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = _rec(store, tmp_path)
    first = rec.recommend(7, ["speed"], n=5)["items"][0]
    assert rec.block_map(7, beatmapset_id=first["beatmapset_id"])["ok"]
    assert first["beatmap_id"] not in {it["beatmap_id"] for it in rec.recommend(7, ["speed"], n=15)["items"]}
    assert "indica o mapa" in rec.block_map(7)["error"] and "âmbito" in rec.block_map(7, beatmap_id=1, scope="x")["error"]
    assert "id dessa dificuldade" in rec.block_map(7, beatmapset_id=5, scope="diff")["error"]
    assert rec.block_map(7, beatmap_id=424242)["scope"] == "diff"  # set desconhecido: só a dificuldade


def test_map_references_are_parsed_from_links_and_ids():
    assert parse_map_ref("https://osu.ppy.sh/beatmapsets/12#osu/34") == (34, 12)
    assert parse_map_ref("https://osu.ppy.sh/beatmapsets/12") == (None, 12)
    assert parse_map_ref("osu.ppy.sh/b/99") == (99, None) and parse_map_ref("https://osu.ppy.sh/beatmaps/7") == (7, None)
    assert parse_map_ref(" 777 ") == (777, None) and parse_map_ref("banana") == (None, None) and parse_map_ref(None) == (None, None)


def test_block_actions_work_from_the_explore_panel_and_the_local_app(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = _rec(store, tmp_path)
    act = actions(Explorer(store), None, rec)
    first = act["/api/recommend"]({"id": 7, "skills": ["speed"]})["items"][0]
    res = act["/api/rec_block"]({"id": 7, "ref": f"https://osu.ppy.sh/beatmapsets/{first['beatmapset_id']}#osu/{first['beatmap_id']}", "scope": "set"})
    assert res["ok"] and act["/api/rec_blocks"]({"id": 7})["items"][0]["beatmapset_id"] == first["beatmapset_id"]
    assert "não percebi" in act["/api/rec_block"]({"id": 7, "ref": "banana"})["error"] and act["/api/rec_block"]({"id": None})["error"]
    assert act["/api/rec_unblock"]({"id": 7, "block_id": act["/api/rec_blocks"]({"id": 7})["items"][0]["id"]})["ok"]
    assert act["/api/rec_blocks"]({"id": 7})["items"] == []

    from osuml.panel import server as ps
    from osuml.recommend import app as appmod

    captured = {}
    orig = ps.make_generic_server
    ps.make_generic_server = lambda page, state, a, port: captured.update(actions=a) or (None, "t")
    try:
        appmod.build_app(rec, 1)
    finally:
        ps.make_generic_server = orig
    A = captured["actions"]
    r = A["/api/block"]({"player": "teste", "beatmap_id": first["beatmap_id"], "beatmapset_id": first["beatmapset_id"], "scope": "diff"})
    assert r["ok"] and r["scope"] == "diff" and A["/api/blocks"]({"player": "Teste"})["items"][0]["beatmap_id"] == first["beatmap_id"]
    assert "não encontrado" in A["/api/block"]({"player": "x", "ref": "5"})["error"]
    assert A["/api/unblock"]({"player": "Teste", "id": A["/api/blocks"]({"player": "Teste"})["items"][0]["id"]})["ok"]


# ------------------------------------------------------------------ Explorar: catálogo completo
def test_explore_searches_and_opens_maps_from_the_whole_catalog_not_only_the_played_ones(tmp_path):
    store = _store_with_player(tmp_path, _played())
    rec = _rec(store, tmp_path)
    ex = Explorer(store, catalog=rec.catalog)
    played = {1000 + i for i in range(30)}
    res = ex.search_maps("", limit=500)
    assert res["catalog"] is True and res["catalog_size"] == len(_index()["ids"]) and res["total"] == res["catalog_size"]  # BD ∪ catálogo, sem contar duas vezes
    only = [it for it in res["items"] if it.get("catalog_only")]
    assert only and not played & {it["beatmap_id"] for it in only} and {it["beatmap_id"] for it in res["items"]} >= played
    assert not any(it.get("catalog_only") for it in res["items"][:30])  # os mapas com plays vêm primeiro
    one = ex.search_maps("Musica 40")  # por texto, só existe no catálogo
    assert [i["beatmap_id"] for i in one["items"]] == [1040] and one["items"][0]["catalog_only"] and one["items"][0]["n_plays"] == 0
    assert [i["beatmap_id"] for i in ex.search_maps("1041")["items"]] == [1041]
    assert {i["beatmap_id"] for i in ex.search_maps("5020")["items"]} == {1040, 1041}  # o id de um set encontra as suas dificuldades
    assert ex.search_maps("nada-disto-existe")["total"] == 0
    d = ex.map(1040)
    assert d["status"] == "catálogo" and d["label"] == "Artista – Musica 40 [Dif40]" and d["plays"] == [] and "catálogo" in d["note"] and d["url"].endswith("beatmapsets/5020#osu/1040")
    assert ex.map(424242) is None and Explorer(store).map(1040) is None  # sem catálogo ligado, como antes
    assert Explorer(store).search_maps("")["total"] == 30 and "catalog" in Explorer(store).search_maps("")
    assert ex.map(1000)["plays"]  # mapa jogado: continua com os plays da BD
