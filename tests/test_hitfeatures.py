"""Stamina/Reading/Tech a partir de hit objects crus (docs/skills_comunidade.md, Q0.3b/Q2.2/Q3.1/Q4.1)."""

from __future__ import annotations

from osuml.beatmaps.hitfeatures import (
    circle_radius, compute_from_parsed, density, preempt_ms, reading_visual, tech_entropy,
)
from osuml.beatmaps.parser import parse_osu
from test_beatmaps import osu_text


def test_preempt_ms_matches_official_formula():
    assert preempt_ms(5) == 1200.0
    assert preempt_ms(0) == 1800.0  # AR mínimo: preempt máximo
    assert preempt_ms(10) == 450.0  # AR máximo: preempt mínimo
    assert preempt_ms(9) == 600.0


def test_circle_radius_shrinks_with_cs():
    assert circle_radius(4) > circle_radius(7)


def test_density_objects_per_second():
    assert density(10, 0, 5000) == 2.0  # 10 objetos em 5s
    assert density(0, 0, 5000) == 0.0
    assert density(10, 1000, 1000) == 0.0  # duração 0


def test_reading_visual_detects_overlap_within_preempt_window():
    # dois objetos na mesma posição, 100ms de intervalo, AR baixo (preempt longo) -> sobrepõem-se.
    objs = [(0, 100, 100), (100, 100, 100)]
    assert reading_visual(objs, ar=0, cs=4) == 1.0

    # mesmos objetos mas afastados no tempo além do preempt -> sem sobreposição.
    objs_far_in_time = [(0, 100, 100), (5000, 100, 100)]
    assert reading_visual(objs_far_in_time, ar=9, cs=4) == 0.0

    # dentro do preempt mas espacialmente muito afastados -> sem sobreposição.
    objs_far_in_space = [(0, 100, 100), (100, 400, 300)]
    assert reading_visual(objs_far_in_space, ar=0, cs=4) == 0.0


def test_tech_entropy_low_for_uniform_stream_high_for_mixed_snaps():
    beat_length = 500.0
    uniform = [(i * 125, beat_length) for i in range(10)]  # sempre 1/4
    assert tech_entropy(uniform) == 0.0

    mixed_times = [0, 125, 250, 500, 625, 1000, 1166.67, 1333.33]  # 1/4, 1/4, 1/2, 1/4, 1/2, 1/3, 1/3
    mixed = [(t, beat_length) for t in mixed_times]
    assert tech_entropy(mixed) > 0.0


def test_compute_from_parsed_returns_all_keys_in_range():
    pb = parse_osu(osu_text())
    out = compute_from_parsed(pb)
    assert set(out) == {"density", "reading_visual", "tech_entropy"}
    assert out["density"] > 0
    assert 0.0 <= out["reading_visual"] <= 1.0
    assert 0.0 <= out["tech_entropy"] <= 1.0
