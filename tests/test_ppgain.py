from datetime import datetime

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osuml.analysis.pp_model import FEATURES, MAP_COLS, mods_key, split_users
from osuml.recommend.ppgain import PpPredictor, marginal_gain, player_best_pp, replace_gain, total_pp
from osuml.storage import models as m
from osuml.storage.database import Store


def test_marginal_gain_matches_brute_force_and_is_zero_below_nothing():
    rng = np.random.default_rng(1)
    for _ in range(20):
        p = np.sort(rng.uniform(50, 400, size=int(rng.integers(1, 120))))[::-1]
        for x in rng.uniform(20, 500, size=6):
            assert marginal_gain(p, x) == pytest.approx(total_pp(np.append(p, x)) - total_pp(p), rel=1e-9, abs=1e-9)
    assert marginal_gain(np.array([]), 100.0) == pytest.approx(100.0)  # lista vazia: o score entra com peso 1


def test_gain_grows_with_pp_and_a_new_top_play_is_worth_more_than_a_bottom_one():
    p = np.sort(np.random.default_rng(2).uniform(100, 300, size=100))[::-1]
    xs = np.array([100.0, 200.0, 300.0, 400.0])
    g = marginal_gain(p, xs)
    assert np.all(np.diff(g) > 0)
    assert marginal_gain(p, 400.0) > marginal_gain(p, 150.0) * 3  # no topo conta ~peso 1; em baixo, empurra pouco mas vale pouco


def test_replace_gain_matches_brute_force_and_ignores_downgrades():
    p = np.array([300.0, 250.0, 220.0, 180.0, 150.0, 120.0])
    new = np.array([300.0, 250.0, 220.0, 210.0, 150.0, 120.0])  # 180 → 210
    assert replace_gain(p, 180.0, 210.0) == pytest.approx(total_pp(new) - total_pp(p))
    assert replace_gain(p, 180.0, 170.0) == 0.0 and replace_gain(p, 180.0, 180.0) == 0.0


def test_mods_key_normalises_nc_cl_and_rejects_unknown_mods():
    assert mods_key("") == "" and mods_key("CL") == "" and mods_key("HD,NC") == "DT,HD" and mods_key("HR,DT,HD") == "DT,HD,HR"
    assert mods_key("NF") is None and mods_key("DT,SO") is None and mods_key(None) == ""


def test_user_split_is_deterministic_and_separates_players():
    users = np.arange(1, 5001)
    g = split_users(users)
    assert np.array_equal(g, split_users(users)) and set(g.tolist()) == {0, 1, 2}
    assert 0.6 < (g == 0).mean() < 0.8 and 0.1 < (g == 2).mean() < 0.3
    assert np.array_equal(split_users(np.array([7, 7, 7])), np.full(3, split_users(np.array([7]))[0]))  # o mesmo jogador cai sempre no mesmo grupo


def test_player_best_pp_keeps_the_best_pass_per_map(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")
    now = datetime(2026, 10, 1)
    with s.engine.begin() as c:
        for i, (uid, bid, pp, passed) in enumerate(((1, 10, 120.0, True), (1, 10, 150.0, True), (1, 11, 99.0, False), (1, 12, 80.0, True), (2, 10, 500.0, True))):
            c.execute(m.scores.insert().values(score_id=i + 1, user_id=uid, beatmap_id=bid, pp=pp, passed=passed, raw={}, content_sha256="x",
                                                first_source="best", first_seen_at=now, last_seen_at=now))
    assert player_best_pp(s, 1) == {10: 150.0, 12: 80.0}


def test_predictor_returns_nan_for_unknown_maps_and_rises_with_accuracy(tmp_path):
    import lightgbm as lgb

    rng = np.random.default_rng(3)
    n = 400
    cat = {"beatmap_id": np.arange(1, n + 1), "mods": [""] * n}
    for c in MAP_COLS:
        cat[c] = rng.uniform(1, 10, size=n)
    pq.write_table(pa.table(cat), tmp_path / "map_attributes.parquet")
    x = np.column_stack([rng.uniform(0.8, 1.0, size=n * 5), np.zeros(n * 5), np.zeros((n * 5, 6)), np.tile(np.column_stack([cat[c] for c in MAP_COLS]), (5, 1))])
    y = np.log(50 + 400 * (x[:, 0] - 0.8) * x[:, FEATURES.index("stars")])
    lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 15, "min_data_in_leaf": 5}, lgb.Dataset(x, y, feature_name=FEATURES),
              num_boost_round=60).save_model(str(tmp_path / "pp_official.txt"))
    pred = PpPredictor(tmp_path, tmp_path / "map_attributes.parquet")
    assert pred.available()
    out = pred.predict_nomod(np.array([5, 5, 999_999]), np.array([0.90, 0.98, 0.95]))
    assert np.isnan(out[2]) and out[1] > out[0] > 0
    assert not PpPredictor(tmp_path / "nada", tmp_path / "map_attributes.parquet").available()
