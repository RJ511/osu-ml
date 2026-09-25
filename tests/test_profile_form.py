"""Perfil de forma atual: pesos por recência e esforço (pp), só passes, sem deixar jogadas fáceis mexerem muito no perfil."""

from __future__ import annotations

import numpy as np
import pytest

from osuml.analysis import pass_model as pm
from osuml.analysis import profile_form as pf


def _passes(n_recent=40, n_old=30, seed=0):
    """Recentes (0-10 dias): pp 80-200, mapas de ~5 estrelas; antigos (400 dias): pp 220-260, mapas de ~7 estrelas (o pico de antes da pausa)."""
    rng = np.random.default_rng(seed)
    pp = np.concatenate([rng.uniform(80, 200, n_recent), rng.uniform(220, 260, n_old)])
    age = np.concatenate([rng.uniform(0, 10, n_recent), np.full(n_old, 400.0)])
    stars = np.concatenate([rng.normal(5.0, 0.3, n_recent), rng.normal(7.0, 0.3, n_old)])
    attrs = np.column_stack([stars, stars * .5, stars * .4, np.log1p(stars), stars * .3]).astype(np.float32)
    axis = np.column_stack([50 + 10 * (stars - 5)] * 5).astype(np.float32)
    acc = np.full(len(pp), 0.95, dtype=np.float32)
    fl = np.zeros(len(pp), dtype=np.uint8)
    return attrs, axis, pp.astype(np.float32), acc, fl, age


def test_weighted_quantile_follows_the_weights():
    v = np.array([1.0, 2.0, 3.0, 4.0])
    assert pf.weighted_quantile(v, np.ones(4), 0.5) == pytest.approx(2.5)
    assert pf.weighted_quantile(v, np.array([0.01, 0.01, 0.01, 1.0]), 0.5) > 3.5  # o peso manda: quase tudo no 4


def test_recent_hard_passes_weigh_most_easy_ones_less_and_old_ones_have_a_floor():
    attrs, axis, pp, acc, fl, age = _passes()
    w, info = pf.form_weights(pp, age)
    assert info["mode"] == "form" and 95 <= info["pp_floor"] <= 125 and 180 <= info["pp_ceiling"] <= 200
    recent = age < 11
    hard, easy = recent & (pp >= info["pp_mid"]), recent & (pp <= info["pp_floor"])
    assert w[hard].min() > 0.8 and w[easy].max() < 0.7 and w[easy].max() > pf.EASY_W * 0.8  # fáceis contam menos, mas não zero
    assert 0.24 <= w[~recent].min() and w[~recent].max() <= 0.26  # antigos: piso de recência x esforço 1 (pp acima do meio)
    assert w[hard].min() > 3 * w[~recent].max()


def test_few_passes_or_no_pp_range_fall_back_to_no_adjustment():
    pp = np.full(10, 150.0)
    w, info = pf.form_weights(pp, np.zeros(10))
    assert info["mode"] == "base" and (w == 1).all()  # < MIN_RECENT passes
    w2, info2 = pf.form_weights(np.full(40, 150.0), np.zeros(40))
    assert info2["mode"] == "recency"  # sem gama de pp (todos iguais): só recência, sem porta por esforço


def test_base_mode_is_the_training_profile_and_form_moves_towards_the_current_maps():
    attrs, axis, pp, acc, fl, age = _passes()
    base, bx_b, lv_b, _ = pf.build_profile(attrs, axis, pp, acc, fl, age, len(pp), 1.0, mode="base")
    ref, bx_r = pm.profile_vector(attrs, pp, acc, fl, len(pp), 1.0)
    assert np.allclose(base, ref) and np.allclose(bx_b, bx_r)
    form, _, lv_f, info = pf.build_profile(attrs, axis, pp, acc, fl, age, len(pp), 1.0, mode="form")
    i50, i90 = pm.PROFILE_FEATS.index("p_p50_stars"), pm.PROFILE_FEATS.index("p_p90_stars")
    assert form[i50] < base[i50] - 0.1 and form[i90] < base[i90]  # o perfil deixa de ser o pico antigo (7 estrelas) e aproxima-se do recente (5)
    assert lv_f[4] < lv_b[4] and form[pm.PROFILE_FEATS.index("p_n_pass")] == len(pp)  # nível desce; k = nº de passes, como no treino


def test_easy_recent_passes_move_the_form_profile_less_than_plain_recency():
    attrs, axis, pp, acc, fl, age = _passes()
    n_easy = 30
    e_attrs = np.column_stack([np.full(n_easy, 3.0)] * 5).astype(np.float32) * np.array([1, .5, .4, 0.3, .3], dtype=np.float32)
    attrs2 = np.vstack([attrs, e_attrs]); axis2 = np.vstack([axis, np.full((n_easy, 5), 20.0, dtype=np.float32)])
    pp2 = np.concatenate([pp, np.full(n_easy, 95.0, dtype=np.float32)])   # ~100 pp: o esperado, fáceis
    acc2 = np.concatenate([acc, np.full(n_easy, 0.97, dtype=np.float32)]); fl2 = np.zeros(len(pp2), dtype=np.uint8)
    age2 = np.concatenate([age, np.full(n_easy, 1.0)])
    i50 = pm.PROFILE_FEATS.index("p_p50_stars")

    def shift(mode):
        before = pf.build_profile(attrs, axis, pp, acc, fl, age, len(pp), 1.0, mode=mode)[0][i50]
        after = pf.build_profile(attrs2, axis2, pp2, acc2, fl2, age2, len(pp2), 1.0, mode=mode)[0][i50]
        return abs(after - before)

    assert shift("form") < shift("recency")  # a porta por esforço limita o efeito das jogadas fáceis


def test_best_pass_per_map_and_ages_since_the_players_last_pass():
    ids = np.array([1, 1, 2, 3, 3])
    pp = np.array([50.0, 90.0, 70.0, np.nan, 10.0], dtype=np.float32)
    mask = np.array([True, True, True, True, False])
    assert pf.best_pass_per_map(ids, pp, mask).tolist() == [1, 2, 3]  # mapa 1: o de 90 pp; mapa 3 (loved, sem pp) conta com o único passe
    ended = np.array(["2026-09-10", "2026-09-20", "2026-08-31", "NaT", "2026-09-19"], dtype="datetime64[s]")
    ages = pf.ages_in_days(ended, np.array([1, 2, 3]))
    assert ages.tolist() == [0.0, 20.0, 0.0]  # referência = último passe do jogador (20/09); NaT conta como idade 0
