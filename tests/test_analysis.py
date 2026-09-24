"""Validação do pp e baseline de accuracy (dados sintéticos, sem rede)."""

from __future__ import annotations

import io
import json
import random
import tarfile
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osuml.analysis.accuracy_baseline import run_baseline
from osuml.analysis.ppcheck import run_pp_check
from osuml.beatmaps.catalog import save_plan
from osuml.progress import Progress
from test_beatmaps import osu_text

SCORE_COLS = ["score_id", "user_id", "beatmap_id", "accuracy", "pp", "is_legacy", "mods_effective", "speed_change", "ended_at",
              "n_great", "n_ok", "n_meh", "n_miss", "max_combo"]


def _scores_file(tmp_path, rows, name="s.parquet"):
    full = [{c: r.get(c) for c in SCORE_COLS} for r in rows]
    p = tmp_path / name
    pq.write_table(pa.Table.from_pylist(full, schema=pa.schema([
        ("score_id", pa.int64()), ("user_id", pa.int64()), ("beatmap_id", pa.int64()), ("accuracy", pa.float64()),
        ("pp", pa.float64()), ("is_legacy", pa.bool_()), ("mods_effective", pa.string()), ("speed_change", pa.float64()),
        ("ended_at", pa.timestamp("us")), ("n_great", pa.int64()), ("n_ok", pa.int64()), ("n_meh", pa.int64()),
        ("n_miss", pa.int64()), ("max_combo", pa.int64())])), p)
    return p


def test_progress_file_is_written_atomically_and_finishes(tmp_path):
    path = tmp_path / "p" / "progress.json"
    p = Progress(path, "Teste", 10, "passos", min_interval=0)
    p.update(add=4)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "running" and data["done"] == 4 and data["total"] == 10 and data["label"] == "Teste"
    p.finish()
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "done" and json.loads(path.read_text(encoding="utf-8"))["done"] == 10


def test_pp_check_reproduces_official_pp_when_it_matches(tmp_path):
    import rosu_pp_py as rosu

    text = osu_text("PP")
    bm = rosu.Beatmap(content=text)
    n_obj = len([1 for l in text.splitlines() if l and l[0].isdigit() and l.count(",") >= 4])
    rows = []
    for i in range(1, 9):
        mods = ["", "DT", "HD"][i % 3]
        combo = 5
        pp = rosu.Performance(mods=[m for m in mods.split(",") if m], n300=n_obj - 1, n100=1, n50=0, misses=0, combo=combo,
                              lazer=False).calculate(bm).pp
        rows.append({"score_id": 250 * i, "user_id": 1, "beatmap_id": 1, "accuracy": 0.99, "pp": pp * (1.0 if i % 2 else 1.02),
                     "is_legacy": True, "mods_effective": mods, "ended_at": datetime(2024, 1, 1), "n_great": n_obj - 1,
                     "n_ok": 1, "n_meh": 0, "n_miss": 0, "max_combo": combo})
    rows.append({**rows[0], "score_id": 250 * 20, "mods_effective": "RX"})  # RX é excluído
    rows.append({**rows[0], "score_id": 251, "pp": 10.0})  # fora da amostragem (score_id % 250 != 0)
    scores = _scores_file(tmp_path, rows)
    save_plan({1: [""]}, tmp_path / "plan.json")
    bundle = tmp_path / "b.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        data = text.encode()
        info = tarfile.TarInfo("1.osu")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    man = run_pp_check([scores], tmp_path / "plan.json", bundle, tmp_path / "out", "v1", n_scores=100,
                       progress_path=tmp_path / "prog.json")
    s = man["summary"]
    assert man["sampled"] == 8 and s["n"] == 8  # RX e o score fora da amostra ficaram de fora
    assert s["within_1pct"] == 0.5 and s["within_5pct"] == 1.0  # metade igual, metade +2 %
    assert s["pearson_r"] > 0.99 and "nomod" in s["by_mods"] and "DT/NC" in s["by_mods"]
    assert json.loads((tmp_path / "prog.json").read_text(encoding="utf-8"))["status"] == "done"


def test_holdout_players_are_a_deterministic_fraction():
    import numpy as np

    from osuml.analysis.accuracy_baseline import is_holdout

    ids = np.arange(1, 5001, dtype=np.int64)
    h = is_holdout(ids, 20, 42)
    assert 0.15 < h.mean() < 0.25 and (h == is_holdout(ids, 20, 42)).all()


def test_accuracy_baseline_beats_naive_baselines_and_uses_only_the_past(tmp_path):
    rng = random.Random(1)
    maps = {b: dict(beatmap_id=b, mods="", stars=2 + b * 0.1, aim=1 + b * 0.05, speed=1 + b * 0.04, reading=0.5 + b * 0.03,
                    density=2 + b * 0.05, ar=9.0, cs=4.0, od=8.0, hp=5.0, n_objects=300 + b, length_ms=90000 + b) for b in range(1, 41)}
    cat = tmp_path / "cat.parquet"
    pq.write_table(pa.Table.from_pylist(list(maps.values())), cat)
    rows, sid = [], 0
    for u in range(1, 41):
        skill = rng.uniform(0.0, 1.0)
        for period, n in ((datetime(2024, 3, 1), 30), (datetime(2025, 2, 1), 12), (datetime(2025, 9, 1), 12)):
            for _ in range(n):
                b = rng.randint(1, 40)
                sid += 1
                acc = min(0.999, max(0.6, 0.90 + 0.06 * skill - 0.004 * (2 + b * 0.1) + rng.gauss(0, 0.004)))
                rows.append({"score_id": sid, "user_id": u, "beatmap_id": b, "accuracy": acc, "pp": 100.0, "is_legacy": False,
                             "mods_effective": "", "ended_at": period + timedelta(days=rng.randint(0, 20))})
    scores = _scores_file(tmp_path, rows)
    out = run_baseline([scores], cat, tmp_path / "out", "v1", per_player=50, min_hist=20, rounds=30,
                       progress_path=tmp_path / "prog.json")
    seen, unseen = out["results"]["seen_players"], out["results"]["unseen_players"]
    assert out["sizes"]["train"] > 200 and out["sizes"]["test"] > 400 and out["sizes"]["players_with_history"] == 40
    assert seen["players"] + unseen["players"] == 40 and unseen["players"] >= 3  # hold-out de jogadores
    for g in (seen, unseen):
        assert g["lgbm_full"]["mae"] < g["player_mean"]["mae"] < g["global_mean"]["mae"]
        assert {"lgbm_map_only", "lgbm_no_reading", "lgbm_no_gap"} <= set(g)
    assert out["improvement_vs_player_mean_mae_pct"]["unseen_players"]["lgbm_full"] > 0
    assert (tmp_path / "out" / "v1" / "results.json").exists() and (tmp_path / "out" / "v1" / "lgbm_full.txt").exists()
    assert json.loads((tmp_path / "prog.json").read_text(encoding="utf-8"))["status"] == "done"


def test_baseline_needs_history_and_raises_a_clear_error_otherwise(tmp_path):
    cat = tmp_path / "cat.parquet"
    pq.write_table(pa.Table.from_pylist([dict(beatmap_id=1, mods="", stars=3.0, aim=1.0, speed=1.0, reading=1.0, density=2.0, ar=9.0,
                                              cs=4.0, od=8.0, hp=5.0, n_objects=300, length_ms=90000)]), cat)
    scores = _scores_file(tmp_path, [{"score_id": 1, "user_id": 1, "beatmap_id": 1, "accuracy": 0.9, "pp": 1.0, "is_legacy": False,
                                      "mods_effective": "", "ended_at": datetime(2025, 8, 1)}])
    with pytest.raises(RuntimeError, match="poucos dados"):
        run_baseline([scores], cat, tmp_path / "out", "v1", rounds=5, progress_path=tmp_path / "p.json")
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "error"


def test_playcount_check_estimates_extra_attempts_and_never_passed_pairs(tmp_path):
    from osuml.analysis.playcount_check import run_playcount_check

    scores = _scores_file(tmp_path, [
        {"score_id": 1, "user_id": 1, "beatmap_id": 10, "accuracy": .9, "ended_at": datetime(2024, 1, 1)},
        {"score_id": 2, "user_id": 1, "beatmap_id": 10, "accuracy": .9, "ended_at": datetime(2024, 1, 2)},   # 2 passes no mesmo mapa
        {"score_id": 3, "user_id": 1, "beatmap_id": 11, "accuracy": .9, "ended_at": datetime(2024, 1, 3)},
        {"score_id": 4, "user_id": 2, "beatmap_id": 10, "accuracy": .9, "ended_at": datetime(2024, 1, 4)}])
    pc = tmp_path / "pc.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"user_id": 1, "beatmap_id": 10, "playcount": 5},   # 5 tentativas, 2 passes -> +3
        {"user_id": 1, "beatmap_id": 11, "playcount": 1},   # 1 tentativa, 1 pass -> +0
        {"user_id": 1, "beatmap_id": 12, "playcount": 4},   # nunca passou
        {"user_id": 2, "beatmap_id": 10, "playcount": 1},
        {"user_id": 99, "beatmap_id": 10, "playcount": 9}]), pc)  # jogador sem scores: ignorado
    man = run_playcount_check([scores], [pc], tmp_path / "out", "v1", progress_path=tmp_path / "p.json")
    r = man["result"]
    assert r["pairs_matched"] == 3 and r["pairs_only_in_playcount"] == 1 and r["players"] == 2
    assert r["matched"]["playcount_ge_passes_share"] == 1.0 and r["matched"]["extra_attempts_share"] == round(1 / 3, 4)
    assert r["matched"]["pairs_with_multiple_scores_share"] == round(1 / 3, 4)
    assert r["never_passed_pairs"]["n"] == 1 and r["never_passed_pairs"]["mean_attempts"] == 4.0
    assert r["estimated_fails_total"] == 3 + 4  # +3 do par (1,10) e as 4 do par nunca passado


def test_progress_survives_a_locked_file(tmp_path, monkeypatch):
    import os

    path = tmp_path / "prog.json"
    p = Progress(path, "t", 10, "x", min_interval=0)
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("em uso")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    p.update(add=3)  # falha 2 vezes e à 3.ª escreve
    assert json.loads(path.read_text(encoding="utf-8"))["done"] == 3
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("sempre")))
    p.update(add=2)  # nunca deve lançar
