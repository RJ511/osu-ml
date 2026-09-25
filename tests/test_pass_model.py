"""Modelo P(passar alguma vez | jogador, mapa): dataset sem fuga de informação, métricas e avaliação (dados sintéticos)."""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osuml.analysis import pass_model as pm

N_MAPS, N_PLAYERS = 120, 60


def _world(tmp_path, seed=3):
    """Mundo sintético: cada jogador tem 'skill'; passa o mapa se skill > dificuldade (com ruído)."""
    rng = np.random.default_rng(seed)
    stars = np.linspace(2, 9, N_MAPS)
    cat = [dict(beatmap_id=1000 + i, mods="", stars=float(stars[i]), aim=float(stars[i] * .5), speed=float(stars[i] * .4), reading=float(stars[i] * .2),
                density=float(2 + stars[i] * .3), ar=9.0, cs=4.0, od=8.0, hp=5.0, n_objects=300 + i, length_ms=90000 + 100 * i) for i in range(N_MAPS)]
    cat_path = tmp_path / "map_attributes.parquet"
    pq.write_table(pa.Table.from_pylist(cat), cat_path)
    pc, sc = [], []
    for u in range(1, N_PLAYERS + 1):
        skill = rng.uniform(3, 8)
        tried = rng.choice(N_MAPS, size=90, replace=False)
        for i in tried:
            p_pass = 1 / (1 + np.exp(2.5 * (stars[i] - skill)))
            passed = rng.random() < p_pass
            pc.append(dict(user_id=u, beatmap_id=1000 + int(i), playcount=int(rng.integers(1, 6))))
            if passed:
                sc.append(dict(user_id=u, beatmap_id=1000 + int(i), pp=float(30 * stars[i]), accuracy=0.95, mods_effective=rng.choice(["", "DT", "HD"])))
    (tmp_path / "in").mkdir()
    pq.write_table(pa.Table.from_pylist(pc), tmp_path / "in" / "osu_user_beatmap_playcount_2026_09_01_random_10000.parquet")
    pq.write_table(pa.Table.from_pylist(sc), tmp_path / "in" / "dump_scores_2026_09_01_random_10000.parquet")
    pq.write_table(pa.Table.from_pylist(cat), tmp_path / "in" / "map_attributes.parquet")
    return cat_path


def test_metrics_auc_and_calibration_are_correct():
    y = np.array([0, 0, 1, 1])
    assert pm.auc(y, np.array([.1, .2, .8, .9])) == 1.0 and pm.auc(y, np.array([.9, .8, .2, .1])) == 0.0
    assert pm.auc(np.array([1, 1]), np.array([.1, .2])) is None
    assert pm.auc(y, np.array([.5, .5, .5, .5])) == 0.5  # empates
    cal = pm.calibration(np.array([0, 1] * 50), np.linspace(0, 1, 100))
    assert 0 <= cal["ece"] <= 1 and cal["bins"]
    m = pm.metrics(y, np.array([.1, .2, .8, .9]))
    assert m["auc"] == 1.0 and m["base_rate"] == 0.5 and m["brier"] < 0.05


def test_profile_uses_only_the_given_passes_and_gap_is_relative_to_p90():
    attrs = np.column_stack([np.linspace(2, 6, 30)] * 5).astype(np.float32)
    prof, bx = pm.profile_vector(attrs, np.linspace(10, 300, 30).astype(np.float32), np.full(30, .95, np.float32),
                                 np.zeros(30, np.uint8), 60, 1.2)
    assert len(prof) == len(pm.PROFILE_FEATS) and bx[0] == pytest.approx(0.5)
    p90 = prof[pm.PROFILE_FEATS.index("p_p90_stars")]
    assert 5.5 < p90 < 6.0
    assert pm.profile_vector(attrs[:5], np.ones(5, np.float32), np.ones(5, np.float32), np.zeros(5, np.uint8), 5, 1.0) is None  # poucos passes
    mx = np.zeros((1, len(pm.MAP_FEATS)), np.float32)
    mx[0, pm.MAP_FEATS.index("stars")] = 7.0
    g = pm.gaps(mx, prof)
    assert g[0, 0] == pytest.approx(7.0 - p90, abs=1e-4)


def test_rows_never_put_the_target_pair_in_the_player_profile(tmp_path):
    _world(tmp_path)
    d = tmp_path / "in"
    cat_ids, cat_x = pm.load_catalog(d / "map_attributes.parquet")
    users, bids, att, top = pm.load_pairs(sorted(d.glob("osu_user_beatmap_playcount_*.parquet")))
    pk, ppp, pacc, pfl = pm.load_passes(sorted(d.glob("dump_scores_*.parquet")))
    rows = pm.build_rows(users, bids, att, top, pk, ppp, pacc, pfl, cat_ids, cat_x, seed=42, cap_rows=1000, min_pairs=20)
    assert rows["x"].shape[1] == len(MAP := pm.MAP_FEATS) + len(pm.PROFILE_FEATS) + len(pm.GAP_FEATS)
    assert set(np.unique(rows["y"])) == {0, 1} and rows["player_views"] > N_PLAYERS  # 2 vistas por jogador
    # o alvo de cada linha está na metade oposta àquela que gerou o perfil => hash do mapa da linha != da vista do perfil (garantido por construção);
    # verificação indireta: um jogador com perfil calculado sem o mapa não tem p_n_pass maior do que o nº total de passes dele
    n_pass = {u: int(((pk >> pm.BM_BITS) == u).sum()) for u in np.unique(rows["user"])}
    idx = pm.PROFILE_FEATS.index("p_n_pass") + len(MAP)
    assert all(rows["x"][i, idx] <= n_pass[rows["user"][i]] for i in range(0, len(rows["y"]), 97))
    # há sinal: mapas mais difíceis que o P90 do jogador passam-se menos
    gap = rows["x"][:, len(MAP) + len(pm.PROFILE_FEATS)]
    assert rows["y"][gap > 1].mean() < rows["y"][gap < -1].mean()


def test_map_features_are_leave_one_out_for_train_rows_and_full_stats_for_others():
    bids = np.array([1, 1, 1, 2, 2, 1, 2, 3])
    y = np.array([1, 0, 1, 0, 0, 1, 1, 1], dtype=np.int8)
    groups = np.array([0, 0, 0, 0, 0, 2, 2, 2])
    feats, stats = pm.add_map_features({"bid": bids, "y": y}, groups, prior=0.5, alpha=1.0)
    # mapa 1 no treino: 3 linhas, 2 passes. 1.ª linha (y=1): LOO = (2-1+0.5)/(2+1) = 0.5 ; 2.ª (y=0): (2-0+0.5)/3
    assert feats[0, 0] == pytest.approx(0.5) and feats[1, 0] == pytest.approx(2.5 / 3)
    # linha de teste no mapa 1: estatística completa (2 passes em 3) => (2+0.5)/(3+1)
    assert feats[5, 0] == pytest.approx(2.5 / 4)
    assert feats[7, 0] == pytest.approx(0.5) and feats[7, 1] == 0.0  # mapa 3 nunca visto no treino: só o prior


def test_end_to_end_training_beats_baselines_and_shuffled_labels_are_chance(tmp_path):
    _world(tmp_path)
    out = pm.run_pass_model(tmp_path / "in", tmp_path / "out", "v1", seeds=(1, 2), rounds=40, threads=2, cap_rows=1000,
                            progress_path=tmp_path / "p.json")
    r = out["results"]
    assert out["data"]["players"] >= 40 and out["data"]["test_players"] >= 5
    assert r["A"]["all"]["auc"] > 0.75 and r["A"]["all"]["auc"] > r["M"]["all"]["auc"] - 0.02
    assert r["shuffled_labels_A"]["auc"] < 0.6
    assert len(r["repeats_A"]["runs"]) == 2 and r["repeats_A"]["auc_mean"] > 0.7
    assert {"map_loo_rate", "player_pass_rate", "constant_train_rate"} <= set(r["baselines"])
    assert (tmp_path / "out" / "v1" / "results.json").exists() and (tmp_path / "out" / "v1" / "pass_model_A.txt").exists()
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "done"


def test_evaluate_api_handles_api_only_players_without_using_the_target_map_in_the_profile(tmp_path):
    _world(tmp_path)
    d = tmp_path / "in"
    api = []
    for i in range(N_MAPS):  # jogador só da API: passa mapas fáceis, falha difíceis
        b = 1000 + i
        api.append(dict(user_id=999, username="Solo", beatmap_id=b, passed=i < 60, ended_at=datetime(2026, 9, 22), mods_effective="", pp=float(30 + i),
                        accuracy=0.95, first_source="best" if i < 40 else "recent"))
    p = tmp_path / "api.parquet"
    pq.write_table(pa.Table.from_pylist(api), p)
    out = pm.run_pass_model(d, tmp_path / "out", "v1", seeds=(1,), rounds=30, threads=2, cap_rows=1000, api_plays=p, players={"Solo": 999})
    solo = out["api"]["api_only"]["Solo"]
    assert solo["n_passes_used_for_profile"] == 60 and "pair" in solo
    assert solo["pair"]["A"]["auc"] > 0.8  # mapas difíceis (índice alto) têm menor P(passar) do que os fáceis
    assert "lowest_predictions_on_never_passed" in solo


def test_hash_seeds_are_independent_so_sampling_then_splitting_keeps_all_groups():
    users = np.arange(1, 200_001, dtype=np.int64)
    sampled = users[pm._hash01(users, 99, 100) < 3]           # 3 % dos jogadores
    g = pm.split_groups(sampled, 42)
    assert len(sampled) > 4000 and {0, 1, 2} <= set(g.tolist())
    assert 0.65 < (g == 0).mean() < 0.75 and 0.06 < (g == 1).mean() < 0.14 and 0.16 < (g == 2).mean() < 0.24
    h = pm._hash01(np.arange(50_000), 42, 2)
    assert 0.48 < h.mean() < 0.52



def test_reach_model_confidence_table_is_calibrated_on_a_world_where_accuracy_depends_on_skill(tmp_path):
    from osuml.analysis import reach_model as rm

    rng = np.random.default_rng(5)
    stars = np.linspace(2, 9, N_MAPS)
    cat = [dict(beatmap_id=1000 + i, mods="", stars=float(stars[i]), aim=float(stars[i] * .5), speed=float(stars[i] * .4), reading=float(stars[i] * .2),
                density=float(2 + stars[i] * .3), ar=9.0, cs=4.0, od=8.0, hp=5.0, n_objects=300 + i, length_ms=90000 + 100 * i) for i in range(N_MAPS)]
    pc, sc = [], []
    for u in range(1, 81):
        skill = rng.uniform(3, 8)
        for i in rng.choice(N_MAPS, size=90, replace=False):
            acc = float(np.clip(0.99 - 0.03 * (stars[i] - skill) + rng.normal(0, 0.01), 0.6, 1.0))
            attempts = int(rng.integers(1, 6))
            pc.append(dict(user_id=u, beatmap_id=1000 + int(i), playcount=attempts))
            if acc > 0.8:  # abaixo disto "não passou"
                sc.append(dict(user_id=u, beatmap_id=1000 + int(i), pp=float(30 * stars[i]), accuracy=acc, mods_effective=""))
    d = tmp_path / "in"
    d.mkdir()
    pq.write_table(pa.Table.from_pylist(pc), d / "osu_user_beatmap_playcount_2026_09_01_random_10000.parquet")
    pq.write_table(pa.Table.from_pylist(sc), d / "dump_scores_2026_09_01_random_10000.parquet")
    pq.write_table(pa.Table.from_pylist(cat), d / "map_attributes.parquet")
    out = rm.run_reach_model(d, tmp_path / "out", "v1", rounds=40, threads=2, cap_rows=1000, progress_path=tmp_path / "p.json")
    r88, r93 = out["results"]["acc88"], out["results"]["acc93"]
    assert r88["base_rate"] > r93["base_rate"]  # é mais difícil chegar a 93 % do que a 88 %
    assert r88["A"]["all"]["auc"] > 0.8 and r93["A"]["all"]["auc"] > 0.8
    conf = r88["A"]["confidence"]
    assert conf and all(c["observed_rate"] >= c["predicted_at_least"] - 0.25 for c in conf)  # a confiança alta não é enganadora
    assert conf[-1]["observed_rate"] > conf[0]["observed_rate"]  # e sobe com o nível de confiança
    assert r88["A"]["by_gap_to_player_p90_stars"]
    assert (tmp_path / "out" / "v1" / "reach_acc88_A.txt").exists()



def test_similarity_methods_beat_popularity_when_players_have_distinct_tastes(tmp_path):
    from osuml.analysis import similarity as sim

    rng = np.random.default_rng(9)
    n_maps = 400
    stars = np.concatenate([np.linspace(2, 4, n_maps // 2), np.linspace(6, 8, n_maps // 2)])  # dois grupos de mapas: fáceis e difíceis
    cat = [dict(beatmap_id=1000 + i, mods="", stars=float(stars[i]), aim=float(stars[i] * .5), speed=float(stars[i] * .4), reading=float(stars[i] * .2),
                density=float(2 + stars[i] * .3), ar=9.0, cs=4.0, od=8.0, hp=5.0, n_objects=300 + i, length_ms=90000 + 100 * i) for i in range(n_maps)]
    pc = []
    for u in range(1, 241):
        group = u % 2  # 0 gosta dos fáceis, 1 dos difíceis
        pool = np.arange(n_maps // 2) + group * (n_maps // 2)
        for i in rng.choice(pool, size=90, replace=False):
            pc.append(dict(user_id=u, beatmap_id=1000 + int(i), playcount=int(rng.integers(1, 5))))
    d = tmp_path / "in"
    d.mkdir()
    pq.write_table(pa.Table.from_pylist(pc), d / "osu_user_beatmap_playcount_2026_09_01_random_10000.parquet")
    pq.write_table(pa.Table.from_pylist(cat), d / "map_attributes.parquet")
    out = sim.run_similarity_eval(d, tmp_path / "out", "v1", n_test_users=40, k_neighbors=10, top_items=300, min_pairs=50, chunk=20,
                                  progress_path=tmp_path / "p.json")
    r = out["results"]
    assert set(r) == set(sim.METHODS) and out["data"]["test_players"] == 40
    for m in ("content_profile", "user_cf", "item_cf", "hybrid_user_content"):
        assert r[m]["recall@50"] > r["popularity"]["recall@50"] + 0.05  # personalizar ganha à popularidade quando há gostos distintos
    assert (tmp_path / "out" / "v1" / "results.json").exists()


def test_acc_model_predicts_the_accuracy_of_passes_and_saves_a_loadable_model(tmp_path):
    """Mundo sintético: a accuracy dos passes depende da folga skill-dificuldade; o modelo tem de bater a mediana global."""
    import lightgbm as lgb

    from osuml.analysis.acc_model import band_table, reg_metrics, run_acc_model

    rng = np.random.default_rng(5)
    _world(tmp_path)
    sc = pq.read_table(tmp_path / "in" / "dump_scores_2026_09_01_random_10000.parquet").to_pylist()
    stars = {1000 + i: s for i, s in enumerate(np.linspace(2, 9, N_MAPS))}
    for r in sc:  # accuracy sobe quando o mapa é mais fácil (menos estrelas) + ruído
        r["accuracy"] = float(np.clip(1.02 - 0.05 * stars[r["beatmap_id"]] + rng.normal(0, 0.01), 0.7, 1.0))
    pq.write_table(pa.Table.from_pylist(sc), tmp_path / "in" / "dump_scores_2026_09_01_random_10000.parquet")
    out = run_acc_model(tmp_path / "in", tmp_path / "out", "t", rounds=60, threads=2, cap_rows=100)
    assert out["data"]["passed_pairs"] > 500 and out["results"]["model"]["mae"] < out["results"]["baselines"]["global_median"]["mae"]
    model = lgb.Booster(model_file=str(tmp_path / "out" / "t" / "acc_pass_A.txt"))
    assert model.num_feature() == len(pm.FEATURE_SETS["A"]) and (tmp_path / "out" / "t" / "results.json").exists()
    m = reg_metrics(np.array([0.9, 0.95]), np.array([0.92, 0.93]))
    assert m["mae"] == pytest.approx(0.02) and m["bias"] == pytest.approx(0.0)
    bands = band_table(np.full(100, 0.94), np.full(100, 0.91))
    assert bands[0]["predicted_range"] == [0.90, 0.93] and bands[0]["share_actual_ge_88"] == 1.0 and bands[0]["share_actual_ge_93"] == 1.0
