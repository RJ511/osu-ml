"""Catálogo de mapas: normalização de mods, escolha dos pares (mapa, mods) e cálculo a partir de um dump pequeno."""

from __future__ import annotations

import io
import tarfile

import pyarrow as pa
import pyarrow.parquet as pq

from osuml.beatmaps.catalog import attr_mods, build_catalog, choose_pairs
from test_beatmaps import osu_text


def test_attr_mods_keeps_only_difficulty_relevant_mods_and_aliases():
    assert attr_mods("CL,HD,NC") == "DT,HD" and attr_mods("DC") == "HT" and attr_mods("") == "" and attr_mods(None) == ""
    assert attr_mods("NF,SD,PF,CL") == "" and attr_mods("HR,DT") == "DT,HR" and attr_mods("DT,HT") == "DT"


def _scores(tmp_path):
    rows = ([{"beatmap_id": 1, "mods_effective": ""}] * 6 + [{"beatmap_id": 1, "mods_effective": "HD,NC"}] * 3
            + [{"beatmap_id": 1, "mods_effective": "HR"}] * 1 + [{"beatmap_id": 2, "mods_effective": "NF"}] * 4
            + [{"beatmap_id": 3, "mods_effective": ""}] * 1 + [{"beatmap_id": 99, "mods_effective": ""}] * 2)
    p = tmp_path / "s.parquet"
    pq.write_table(pa.Table.from_pylist(rows), p)
    return p


def test_choose_pairs_top_n_nomod_always_and_min_plays_for_combos(tmp_path):
    got = choose_pairs([_scores(tmp_path)], top_n=3, min_plays=3)
    assert set(got) == {1, 2, 99}  # os 3 mais jogados; o 3 (1 play) fica de fora
    assert got[1] == ["", "DT,HD"] and got[2] == [""] and got[99] == [""]  # HR só 1 play < 3; NF não conta como mod


def test_build_catalog_from_small_dump(tmp_path):
    dump = tmp_path / "dump.tar.bz2"
    with tarfile.open(dump, "w:bz2") as tf:
        for bid, title in ((1, "Um"), (2, "Dois"), (7, "Fora")):
            data = osu_text(title).encode()
            info = tarfile.TarInfo(f"osu_files/{bid}.osu")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    man = build_catalog(dump, [_scores(tmp_path)], tmp_path / "out", "v1", top_n=3, min_plays=3, workers=1)
    assert man["maps_in_plan"] == 3 and man["maps"] == 2 and man["maps_missing_vs_plan"] == 1
    assert man["run"]["not_found_in_source"] == 1
    rows = pq.read_table(tmp_path / "out" / "v1" / "map_attributes.parquet").to_pylist()
    by = {(r["beatmap_id"], r["mods"]): r for r in rows}
    assert set(by) == {(1, ""), (1, "DT,HD"), (2, "")}
    assert by[(1, "DT,HD")]["stars"] > by[(1, "")]["stars"] and by[(1, "DT,HD")]["reading"] >= by[(1, "")]["reading"]
    assert by[(1, "")]["n_objects"] > 0 and "density" in by[(1, "")]


# ---------------------------------------------- fluxo em várias máquinas (plano, bundle, shard, retoma, merge)
import pytest

from osuml.beatmaps.catalog import bundle_osu, in_shard, load_plan, merge_catalog, parse_shard, run_catalog, save_plan


def _dump(tmp_path, ids=(1, 2, 3, 4, 7)):
    dump = tmp_path / "dump.tar.bz2"
    with tarfile.open(dump, "w:bz2") as tf:
        for bid in ids:
            data = osu_text(f"M{bid}").encode()
            info = tarfile.TarInfo(f"osu_files/{bid}.osu")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return dump


PLAN = {1: ["", "DT"], 2: [""], 3: [""], 4: ["", "HD"], 9: [""]}  # 9 não existe no dump


def test_plan_roundtrip_and_shards(tmp_path):
    save_plan(PLAN, tmp_path / "p" / "plan.json", {"top_n": 5})
    assert load_plan(tmp_path / "p" / "plan.json") == PLAN
    assert parse_shard("1/3") == (1, 3) and parse_shard(None) is None
    with pytest.raises(ValueError):
        parse_shard("3/3")
    assert [b for b in PLAN if in_shard(b, (0, 2))] == [2, 4] and [b for b in PLAN if in_shard(b, (1, 2))] == [1, 3, 9]


def test_bundle_contains_only_planned_maps_and_can_be_used_as_source(tmp_path):
    info = bundle_osu(_dump(tmp_path), PLAN, tmp_path / "b" / "subset.tar.gz")
    assert info["maps_in_bundle"] == 4 and info["maps_not_in_dump"] == 1
    with tarfile.open(tmp_path / "b" / "subset.tar.gz") as tf:
        assert sorted(tf.getnames()) == ["1.osu", "2.osu", "3.osu", "4.osu"]  # o 7 não estava no plano
    out = run_catalog(tmp_path / "b" / "subset.tar.gz", PLAN, tmp_path / "o", "v1", workers=1)
    assert out["computed_now"] == 4 and out["not_found_in_source"] == 1


def test_shards_are_disjoint_and_merge_recovers_everything(tmp_path):
    dump = _dump(tmp_path)
    a = run_catalog(dump, PLAN, tmp_path / "pod0", "v1", shard=(0, 2), workers=1)
    b = run_catalog(dump, PLAN, tmp_path / "pod1", "v1", shard=(1, 2), workers=1)
    assert a["computed_now"] == 2 and b["computed_now"] == 2  # {2,4} e {1,3}; 9 nunca aparece
    (tmp_path / "all" / "v1" / "parts").mkdir(parents=True)  # o utilizador junta as partes numa só pasta
    for d in ("pod0", "pod1"):
        for f in (tmp_path / d / "v1" / "parts").glob("part_*.parquet"):
            (tmp_path / "all" / "v1" / "parts" / f.name).write_bytes(f.read_bytes())
    man = merge_catalog(tmp_path / "all", "v1", PLAN)
    assert man["maps"] == 4 and man["rows"] == 6 and man["maps_in_plan"] == 5 and man["maps_missing_vs_plan"] == 1
    rows = pq.read_table(tmp_path / "all" / "v1" / "map_attributes.parquet").to_pylist()
    assert {(r["beatmap_id"], r["mods"]) for r in rows} == {(1, ""), (1, "DT"), (2, ""), (3, ""), (4, ""), (4, "HD")}


def test_rerun_resumes_and_skips_finished_maps(tmp_path):
    dump = _dump(tmp_path)
    first = run_catalog(dump, PLAN, tmp_path / "o", "v1", workers=1, max_maps=2)
    assert first["computed_now"] == 2
    second = run_catalog(dump, PLAN, tmp_path / "o", "v1", workers=1)
    assert second["already_done"] == 2 and second["computed_now"] == 2  # só os que faltavam
    third = run_catalog(dump, PLAN, tmp_path / "o", "v1", workers=1)
    assert third["computed_now"] == 0 and third["already_done"] == 4
    assert merge_catalog(tmp_path / "o", "v1")["maps"] == 4


def test_progress_file_is_written_and_marked_done(tmp_path):
    import json

    run_catalog(_dump(tmp_path), PLAN, tmp_path / "o", "v1", workers=1)
    pr = json.loads((tmp_path / "o" / "v1" / "progress_all.json").read_text(encoding="utf-8"))
    assert pr["status"] == "done" and pr["done"] == 4 and pr["total"] == 5 and pr["unit"] == "mapas" and pr["updated_at"] > 0


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"), reason="o limite por mapa usa SIGALRM (só POSIX)")
def test_a_slow_map_is_abandoned_instead_of_blocking(tmp_path):
    from osuml.beatmaps.catalog import compute_map

    lines = ["osu file format v14", "", "[General]", "Mode: 0", "", "[Difficulty]", "HPDrainRate:5", "CircleSize:4",
             "OverallDifficulty:8", "ApproachRate:9", "SliderMultiplier:1.4", "SliderTickRate:1", "", "[TimingPoints]",
             "0,333.33,4,2,0,100,1,0", "", "[HitObjects]"] + [f"{(i * 7) % 500},{(i * 13) % 380},{1000 + i // 40},1,0,0:0:0:0:" for i in range(60000)]
    rows, err = compute_map((5, "\n".join(lines).encode(), ["", "DT", "HD"]), budget_s=0.3)
    assert rows == [] and err == 5


def test_maps_only_attempted_in_playcount_enter_the_plan_as_nomod(tmp_path):
    pc = tmp_path / "pc.parquet"
    pq.write_table(pa.Table.from_pylist([{"beatmap_id": 500, "playcount": 3}] * 4 + [{"beatmap_id": 1, "playcount": 2}] * 2), pc)
    got = choose_pairs([_scores(tmp_path)], top_n=10, min_plays=3, map_files=[pc])
    assert got[500] == [""]  # mapa sem nenhum passe nos scores (tentado 4 vezes): entra, só nomod
    assert got[1] == ["", "DT,HD"]  # os scores continuam a decidir as combinações de mods
    assert 500 not in choose_pairs([_scores(tmp_path)], top_n=10, min_plays=3)
