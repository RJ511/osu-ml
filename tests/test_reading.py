"""Port do Reading oficial do lazer: propriedades esperadas (monotonia com AR, mods, repetição de ângulos)."""

from __future__ import annotations

import random

from osuml.beatmaps.parser import parse_osu
from osuml.beatmaps.reading import reading_difficulty_value, reading_rating, smootherstep


def _osu(objects: list[tuple[int, int, int]], ar: float = 9, cs: float = 4, od: float = 8) -> str:
    lines = ["osu file format v14", "", "[General]", "Mode: 0", "", "[Difficulty]", "HPDrainRate:5",
             f"CircleSize:{cs}", f"OverallDifficulty:{od}", f"ApproachRate:{ar}", "SliderMultiplier:1.4",
             "SliderTickRate:1", "", "[TimingPoints]", "0,333.33,4,2,0,100,1,0", "", "[HitObjects]"]
    lines += [f"{x},{y},{t},1,0,0:0:0:0:" for t, x, y in objects]
    return "\n".join(lines) + "\n"


def _map(seed=1, n=400, gap=180, ar=9.0, jitter=True):
    rng = random.Random(seed)
    objs = []
    for i in range(n):
        x, y = (rng.randint(30, 480), rng.randint(30, 350)) if jitter else (100 + (i % 2) * 300, 200)
        objs.append((1000 + i * gap, x, y))
    return parse_osu(_osu(objs, ar=ar))


def test_smootherstep_endpoints():
    assert smootherstep(0, 0, 1) == 0 and smootherstep(1, 0, 1) == 1 and smootherstep(2, 0, 1) == 1


def test_empty_and_tiny_maps_have_zero_reading():
    assert reading_rating(parse_osu(_osu([]))) == 0.0
    assert reading_rating(parse_osu(_osu([(1000, 100, 100)]))) == 0.0


def test_preempt_term_is_monotonic_in_ar_on_sparse_maps():
    # mapa esparso (poucos objetos visíveis): a densidade quase não conta, só o tempo de reação (preempt)
    vals = [reading_rating(_map(ar=a, gap=600, n=200)) for a in (8.0, 9.0, 10.0, 11.0)]
    assert vals == sorted(vals) and vals[-1] > vals[0]


def test_low_ar_on_dense_maps_is_not_free():
    # no Reading oficial o AR baixo também pesa (muitos objetos visíveis em simultâneo)
    assert reading_rating(_map(ar=6.0, gap=150)) > reading_rating(_map(ar=9.0, gap=150))


def test_dt_and_hd_raise_reading():
    pb = _map(ar=9.0)
    base = reading_rating(pb)
    assert reading_rating(pb, ["DT"]) > base
    assert reading_rating(pb, ["HD"]) > base
    assert reading_rating(pb, ["HD", "DT"]) > reading_rating(pb, ["DT"])
    assert reading_rating(pb, ["HT"]) <= base


def test_repeated_angle_pattern_reads_easier_than_random_jumps():
    rand = reading_difficulty_value(_map(ar=10.0, gap=140))
    steady = reading_difficulty_value(_map(ar=10.0, gap=140, jitter=False))
    assert steady < rand


def test_faster_maps_read_harder():
    assert reading_rating(_map(gap=110, ar=9.5)) > reading_rating(_map(gap=300, ar=9.5))
