"""Dificuldade local por mapa e combinação de mods (rosu-pp-py), sem rede."""

from __future__ import annotations

import hashlib

import pyarrow.parquet as pq

from osuml.beatmaps.acquire import import_from_path, wanted_beatmaps
from osuml.beatmaps.difficulty import calc_difficulty, export_difficulty, mods_by_beatmap
from osuml.storage.database import Store
from test_beatmaps import osu_text


def _fake_result():
    from osuml.api.http import HttpResult

    return HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1)


def _store_with_scores(tmp_path, scores: list[dict]) -> tuple[Store, str]:
    """`scores`: lista de {"beatmap_id": int, "mods": [acronyms...]}, todos apontando para o
    mesmo `.osu` de fixture (o checksum é o mesmo para todos os beatmap_id)."""
    store = Store(f"sqlite:///{tmp_path}/d.db", tmp_path / "raw")
    text = osu_text("A")
    checksum = hashlib.md5(text.encode()).hexdigest()
    objs = []
    for i, sc in enumerate(scores):
        bid = sc["beatmap_id"]
        objs.append({
            "id": 2000 + i, "user_id": 7, "beatmap_id": bid, "passed": True,
            "ended_at": "2026-09-22T20:00:00Z",
            "mods": [{"acronym": a} for a in sc.get("mods", [])],
            "beatmap": {"id": bid, "beatmapset_id": bid, "status": "ranked", "checksum": checksum},
        })
    rid = store.record_request("run", "osu", _fake_result())
    store.ingest_scores(objs, "best", rid)
    return store, text


def test_mods_by_beatmap_dedups_and_strips_cl(tmp_path):
    store, _ = _store_with_scores(tmp_path, [
        {"beatmap_id": 1, "mods": []},
        {"beatmap_id": 1, "mods": ["DT", "HD"]},
        {"beatmap_id": 1, "mods": ["CL"]},  # CL não é escolha do jogador -> mods_effective ""
        {"beatmap_id": 2, "mods": ["HR"]},
    ])
    combos = mods_by_beatmap(store, 7)
    assert combos == {1: {"", "DT,HD"}, 2: {"", "HR"}}


def test_calc_difficulty_dt_increases_aim_and_speed_nomod_has_no_flashlight():
    text = osu_text("A")
    nomod = calc_difficulty(text, "")
    dt = calc_difficulty(text, "DT")
    assert dt["stars"] > nomod["stars"]
    assert dt["speed"] > nomod["speed"]
    assert nomod["flashlight"] == 0.0 and dt["flashlight"] == 0.0  # sem o mod FL


def test_export_difficulty_writes_one_row_per_played_combo(tmp_path):
    store, text = _store_with_scores(tmp_path, [
        {"beatmap_id": 1, "mods": []},
        {"beatmap_id": 1, "mods": ["DT"]},
    ])
    folder = tmp_path / "songs"
    folder.mkdir()
    (folder / "a.osu").write_bytes(text.encode("utf-8"))
    import_from_path(store, folder, wanted_beatmaps(store), "folder")

    man = export_difficulty(store, 7, tmp_path / "processed", "v0.2")
    assert man["rows"] == 2 and man["beatmaps"] == 1 and man["missing_beatmap_files"] == []
    rows = pq.read_table(tmp_path / "processed" / "v0.2" / "difficulty_7.parquet").to_pylist()
    by_mods = {r["mods"]: r for r in rows}
    assert set(by_mods) == {"", "DT"}
    assert by_mods["DT"]["stars"] > by_mods[""]["stars"]


def test_export_difficulty_reports_beatmaps_without_file(tmp_path):
    store, _ = _store_with_scores(tmp_path, [{"beatmap_id": 5, "mods": []}])
    man = export_difficulty(store, 7, tmp_path / "processed", "v0.2")
    assert man["rows"] == 0 and man["missing_beatmap_files"] == [5]
