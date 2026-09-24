"""Escala aberta (50 + 20·z) por skill contra a pool de referência (Reading oficial + sem teto)."""

from __future__ import annotations

import math

import pyarrow as pa
import pyarrow.parquet as pq

from osuml.beatmaps.skills import ReferenceScale, SkillScorer, export_skill_scales, grade


def _write_parquet(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _lognormal_pool(n=2000):
    # quantis de uma log-normal (mu=0, sigma=0.5): a escala deve dar ~50 na mediana e ~70 a +1 sigma.
    from statistics import NormalDist

    nd = NormalDist()
    return [math.exp(0.5 * nd.inv_cdf((i + 0.5) / n)) for i in range(n)]


def test_grade_boundaries():
    assert grade(112) == "X" and grade(111.8) == "X"
    assert grade(100) == "SSS" and grade(96.6) == "SSS"
    assert grade(90) == "SS" and grade(81.0) == "S+"
    assert grade(76) == "S" and grade(66.8) == "A"
    assert grade(50) == "B-" and grade(49.9) == "C+"
    assert grade(0) == "F" and grade(-30) == "F"


def test_scale_is_open_median_is_50_and_sigma_is_20(tmp_path):
    vals = _lognormal_pool()
    path = tmp_path / "ref.parquet"
    _write_parquet(path, [{"stars": v, "aim": v, "speed": v, "reading": v, "density": v} for v in vals])
    scale = ReferenceScale.from_parquet(path)
    assert abs(scale.score("aim", 1.0) - 50) < 0.5  # mediana
    assert abs(scale.score("aim", math.exp(0.5)) - 70) < 0.7  # +1 sigma
    # sem teto: muito acima do máximo da pool passa de 100 e nunca satura
    assert scale.score("aim", max(vals) * 10) > 100 and scale.score("aim", max(vals) * 100) > scale.score("aim", max(vals) * 10)
    # sem chão a 0 e valores <= 0 não rebentam
    assert scale.score("reading", 0.0) < 10


def test_percentile_is_kept_as_secondary_top_x(tmp_path):
    path = tmp_path / "ref.parquet"
    _write_parquet(path, [{"stars": i, "aim": i, "speed": i, "reading": i, "density": i} for i in range(1, 11)])
    scale = ReferenceScale.from_parquet(path)
    assert scale.percentile("aim", 11) == 100.0 and scale.percentile("aim", 0) == 0.0
    assert scale.percentile("aim", 1) == 5.0 and scale.percentile("aim", 10) == 95.0


def test_skill_scorer_scores_all_four_axes(tmp_path):
    vals = _lognormal_pool(500)
    path = tmp_path / "ref.parquet"
    _write_parquet(path, [{"stars": v, "aim": v, "speed": v, "reading": v, "density": v} for v in vals])
    out = SkillScorer(ReferenceScale.from_parquet(path)).score(
        {"stars": 1.0, "aim": 3.0, "speed": 1.1, "reading": 0.4, "density": 1.0})
    assert out["aim_score"] > 90 and out["aim_grade"] in ("SS", "SSS", "X", "S+")
    assert 50 <= out["speed_score"] < 55 and out["speed_grade"] == "B-"
    assert out["reading_score"] < 40 and out["stamina_score"] > 0 and "stamina_pct" in out


def test_export_skill_scales_writes_scores_and_grades(tmp_path):
    ref_path = tmp_path / "ref" / "reference_pool_v3.parquet"
    vals = _lognormal_pool(300)
    _write_parquet(ref_path, [{"stars": v, "aim": v, "speed": v, "reading": v, "density": v} for v in vals])
    diff_path = tmp_path / "diff" / "difficulty_7.parquet"
    _write_parquet(diff_path, [
        {"beatmap_id": 1, "mods": "", "stars": 3.0, "aim": 3.0, "speed": 0.3, "reading": 0.5},
        {"beatmap_id": 1, "mods": "DT", "stars": 3.0, "aim": 0.3, "speed": 3.0, "reading": 2.0},
    ])
    maps_path = tmp_path / "diff" / "beatmaps_7.parquet"
    _write_parquet(maps_path, [{"beatmap_id": 1, "density": 3.0}])

    man = export_skill_scales(diff_path, maps_path, ref_path, tmp_path / "processed", "v0.2", user_id=7)
    assert man["rows"] == 2 and man["skills_covered"] == ["aim", "speed", "stamina", "reading"]
    rows = {r["mods"]: r for r in pq.read_table(tmp_path / "processed" / "v0.2" / "skills_7.parquet").to_pylist()}
    assert rows[""]["aim_score"] > 90 and rows[""]["aim_grade"] == grade(rows[""]["aim_score"])
    assert rows[""]["speed_score"] < 10 and rows["DT"]["speed_score"] > 90
    assert rows["DT"]["reading_score"] > rows[""]["reading_score"]  # Reading varia com os mods
    assert rows[""]["stamina_score"] == rows["DT"]["stamina_score"] > 90  # stamina é por mapa (nomod)
