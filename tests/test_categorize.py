"""Categorização de mapas e jogadores: cache de mapas repetidos, no_file, rating e eventos (sem rede)."""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osuml.api.http import HttpResult
from osuml.beatmaps.acquire import import_from_path, wanted_beatmaps
from osuml.beatmaps.skills import ReferenceScale, SkillScorer
from osuml.categorize import core as C
from osuml.categorize.core import CategorizeController, MapCategorizer, quantile, rate_player, spearman
from osuml.storage.database import Store
from test_beatmaps import osu_text


def _ref(tmp_path):
    rows = [{"stars": i / 10, "aim": i / 10, "speed": i / 10, "density": i / 10,
             "reading": i / 100} for i in range(1, 201)]
    p = tmp_path / "ref.parquet"
    pq.write_table(pa.Table.from_pylist(rows), p)
    return SkillScorer(ReferenceScale.from_parquet(p))


def _world(tmp_path, plays):
    """`plays`: (user_id, beatmap_id, mods, accuracy, passed). Só os beatmaps 1 e 2 têm .osu."""
    store = Store(f"sqlite:///{tmp_path}/c.db", tmp_path / "raw")
    text = {1: osu_text("Um"), 2: osu_text("Dois")}
    rid = store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    objs = []
    for i, (uid, bid, mods, acc, passed) in enumerate(plays):
        objs.append({"id": 1000 + i, "user_id": uid, "beatmap_id": bid, "passed": passed, "accuracy": acc,
                     "ended_at": "2026-09-20T10:00:00Z", "mods": [{"acronym": a} for a in mods],
                     "beatmap": {"id": bid, "beatmapset_id": bid, "version": f"V{bid}", "status": "ranked",
                                 "checksum": hashlib.md5(text.get(bid, b"").encode() if bid in text else b"x").hexdigest()},
                     "beatmapset": {"id": bid, "artist": "Art", "title": f"Titulo{bid}", "status": "ranked"}})
    store.ingest_scores(objs, "best", rid)
    for uid, name, pp, rank in {(p[0], f"nome{p[0]}", 5000.0 + p[0], 100 + p[0]) for p in plays}:
        with store.engine.begin() as c:
            from osuml.storage import models as m
            c.execute(m.users.insert().values(user_id=uid, username=name, raw={"username": name, "statistics": {
                "pp": pp, "global_rank": rank}}, first_seen_at=C.utcnow(), fetched_at=C.utcnow(), request_id=rid))
    folder = tmp_path / "songs"
    folder.mkdir()
    for bid, t in text.items():
        (folder / f"{bid}.osu").write_bytes(t.encode("utf-8"))
    import_from_path(store, folder, wanted_beatmaps(store), "folder")
    return store


def test_repeated_maps_are_categorized_once_and_reused_across_players(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True), (10, 2, [], 0.96, True),
                              (11, 1, [], 0.97, True), (11, 2, ["DT"], 0.92, True), (11, 1, ["CL"], 0.9, True)])
    cat = MapCategorizer(store, _ref(tmp_path))
    ctrl = CategorizeController(store, cat)
    ctrl.start()
    ctrl.wait(30)
    # pares únicos: (1,""), (2,""), (2,"DT")  — `CL` não conta como mod
    assert ctrl.pairs_total == 3 and ctrl.counts["new"] == 3
    # visitas por jogador (pares únicos de cada um): jogador 10 -> 2, jogador 11 -> 2 ((1,"") e (2,"DT"),
    # o `CL` colapsa em (1,"")); só (1,"") já estava categorizado quando o jogador 11 o visitou.
    assert ctrl.counts["visits"] == 4 and ctrl.counts["cached"] == 1
    assert ctrl.status == "done"


def test_second_run_recomputes_nothing(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True)])
    cat = MapCategorizer(store, _ref(tmp_path))
    first = CategorizeController(store, cat)
    first.start(); first.wait(30)
    second = CategorizeController(store, cat)
    second.start(); second.wait(30)
    assert first.counts["new"] == 1 and second.counts["new"] == 0 and second.counts["cached"] == 1


def test_map_without_file_is_marked_no_file_and_not_invented(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True), (10, 999, [], 0.97, True)])
    ctrl = CategorizeController(store, MapCategorizer(store, _ref(tmp_path)))
    ctrl.start(); ctrl.wait(30)
    assert ctrl.counts["no_file"] == 1
    p = ctrl.players[0]["profile"]
    assert p["n_missing"] == 1 and p["n_scores"] == 2
    bad = [e for e in ctrl.events if e["beatmap_id"] == 999][0]
    assert bad["status"] == "no_file" and bad["scores"] is None


def test_events_carry_player_name_pp_and_map_label(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True), (11, 2, [], 0.95, True)])
    ctrl = CategorizeController(store, MapCategorizer(store, _ref(tmp_path)))
    ctrl.start(); ctrl.wait(30)
    ev = {e["user_id"]: e for e in ctrl.events}
    assert ev[10]["username"] == "nome10" and ev[10]["pp"] == 5010.0 and ev[10]["global_rank"] == 110
    assert ev[10]["title"] == "Titulo1" and ev[10]["version"] == "V1" and ev[10]["artist"] == "Art"
    assert [p["user_id"] for p in ctrl.players] == [11, 10]  # ordenados por pp (desc)


def test_player_rating_uses_passed_high_accuracy_plays_only():
    hi = {"aim_score": 90.0, "stars_score": 80.0}
    lo = {"aim_score": 20.0, "stars_score": 10.0}
    plays = [{"passed": True, "accuracy": 0.95, "scores": hi}] * 10 + [
        {"passed": False, "accuracy": 0.99, "scores": lo},   # fail: ignorado
        {"passed": True, "accuracy": 0.80, "scores": lo},    # acc baixa: ignorada
        {"passed": True, "accuracy": 0.97, "scores": None}]  # sem categoria: ignorada
    r = rate_player(plays)
    assert r["n_evidence"] == 10 and r["ratings"]["aim_rating"] == 90.0 and r["ratings"]["confident"] is True
    assert rate_player(plays[:3])["ratings"]["confident"] is False


def test_pause_and_cancel_stop_the_worker(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True), (10, 2, [], 0.95, True)])
    ctrl = CategorizeController(store, MapCategorizer(store, _ref(tmp_path)))
    ctrl.start(delay_ms=200)
    ctrl.cancel()
    ctrl.wait(30)
    assert ctrl.status == "idle" and ctrl.players[0]["profile"] is None


def test_quantile_and_spearman_helpers():
    assert quantile([1, 2, 3, 4, 5], 0.5) == 3 and quantile([1, 2, 3, 4, 5], 0.9) == pytest.approx(4.6)
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0 and spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_export_writes_parquet_and_manifest(tmp_path):
    store = _world(tmp_path, [(10, 1, [], 0.95, True)])
    ctrl = CategorizeController(store, MapCategorizer(store, _ref(tmp_path)))
    ctrl.start(); ctrl.wait(30)
    out = tmp_path / "processed" / "categories" / "v2"
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert man["files"]["map_categories.parquet"]["rows"] == 1 and man["files"]["player_profiles.parquet"]["rows"] == 1
    prof = pq.read_table(out / "player_profiles.parquet").to_pylist()[0]
    assert prof["username"] == "nome10" and prof["pp"] == 5010.0 and "aim_rating" in prof
