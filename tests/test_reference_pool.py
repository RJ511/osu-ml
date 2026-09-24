"""Amostra de referência por reservoir sampling (docs/skills_comunidade.md, Q0.4b), sem rede."""

from __future__ import annotations

import pyarrow.parquet as pq

from osuml.beatmaps.reference_pool import export_reference_pool, reservoir_sample
from test_beatmaps import _tar, osu_text


def _taiko_text(title: str = "Taiko") -> str:
    return osu_text(title).replace("Mode: 0", "Mode: 1")


def test_reservoir_sample_is_deterministic_given_same_seed(tmp_path):
    files = {f"{100 + i}.osu": osu_text(f"Map{i}") for i in range(10)}
    dump = tmp_path / "dump.tar.bz2"
    dump.write_bytes(_tar(files))

    a = reservoir_sample(dump, n=4, seed=123)
    b = reservoir_sample(dump, n=4, seed=123)
    assert len(a) == 4
    assert {name for name, _ in a} == {name for name, _ in b}


def test_reservoir_sample_keeps_everything_when_population_fits(tmp_path):
    files = {f"{100 + i}.osu": osu_text(f"Map{i}") for i in range(5)}
    dump = tmp_path / "dump.tar.bz2"
    dump.write_bytes(_tar(files))

    sample = reservoir_sample(dump, n=10, seed=1)
    assert {name for name, _ in sample} == set(files)


def test_reservoir_sample_excludes_non_osu_std_maps(tmp_path):
    files = {
        "1.osu": osu_text("Std1"),
        "2.osu": osu_text("Std2"),
        "3.osu": _taiko_text("Taiko1"),
        "4.osu": _taiko_text("Taiko2"),
    }
    dump = tmp_path / "dump.tar.bz2"
    dump.write_bytes(_tar(files))

    sample = reservoir_sample(dump, n=10, seed=1)
    assert {name for name, _ in sample} == {"1.osu", "2.osu"}


def test_export_reference_pool_writes_rows_and_manifest(tmp_path):
    files = {
        "111.osu": osu_text("A"),
        "not_an_id.osu": osu_text("B"),
        "222.osu": osu_text("C"),
    }
    dump = tmp_path / "dump.tar.bz2"
    dump.write_bytes(_tar(files))

    man = export_reference_pool(dump, n=10, seed=7, out_dir=tmp_path / "processed", version="v1")
    assert man["rows"] == 3 and man["seed"] == 7 and man["requested_sample_size"] == 10
    assert man["dump_sha256"] and man["failed_to_calculate"] == 0

    rows = pq.read_table(tmp_path / "processed" / "v1" / "reference_pool_v1.parquet").to_pylist()
    by_name = {r["source_name"]: r for r in rows}
    assert by_name["111.osu"]["beatmap_id"] == 111
    assert by_name["222.osu"]["beatmap_id"] == 222
    assert by_name["not_an_id.osu"]["beatmap_id"] is None
    assert all(r["stars"] > 0 for r in rows)
    assert all(r["density"] > 0 for r in rows)
    assert all(0.0 <= r["reading_visual"] <= 1.0 for r in rows)
    assert all(0.0 <= r["tech_entropy"] <= 1.0 for r in rows)
