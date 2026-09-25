"""Pontos de falha: progresso pelas contagens, teste de morte pelo HP do lazer e trecho do mapa (mapa sintético)."""

from __future__ import annotations

import io
import tarfile

import numpy as np
import pyarrow.parquet as pq
import pytest

from osuml.analysis import fail_points as fp
from osuml.api.http import HttpResult
from osuml.storage.database import Store


def _osu(hp=5.0, n_sparse=100, n_stream=50):
    """Mapa sintético: `n_sparse` círculos de 1 em 1 s e depois `n_stream` a 100 ms (um trecho muito mais intenso)."""
    times = [1000 * (i + 1) for i in range(n_sparse)]
    t0 = times[-1] + 1000
    times += [t0 + 100 * i for i in range(n_stream)]
    objs = "\n".join(f"{(i * 37) % 500 + 6},{(i * 53) % 350 + 6},{t},1,0,0:0:0:0:" for i, t in enumerate(times))
    return ("osu file format v14\n\n[General]\nMode: 0\n\n[Difficulty]\n"
            f"HPDrainRate:{hp}\nCircleSize:4\nOverallDifficulty:8\nApproachRate:9\nSliderMultiplier:1.4\nSliderTickRate:1\n\n"
            "[TimingPoints]\n0,500,4,1,0,100,1,0\n\n[HitObjects]\n" + objs + "\n"), times


def _bundle(tmp_path, bid=555, **kw):
    text, times = _osu(**kw)
    path = tmp_path / "b.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        data = text.encode()
        info = tarfile.TarInfo(f"{bid}.osu")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return path, times


def test_hp_targets_damage_and_effective_hp_follow_the_lazer_constants():
    assert (fp.hp_range(0, *fp.HP_TARGET), fp.hp_range(5, *fp.HP_TARGET), fp.hp_range(10, *fp.HP_TARGET)) == pytest.approx((0.99, 0.90, 0.40))
    assert fp.hp_range(7.5, *fp.HP_TARGET) == pytest.approx(0.65) and fp.hp_range(20, *fp.MISS_PEN) == pytest.approx(0.20)
    assert fp.effective_hp(8, "HR") == 10 and fp.effective_hp(5, "HR,DT") == pytest.approx(7.0) and fp.effective_hp(6, "EZ") == pytest.approx(3.0)
    assert fp.damage({"miss": 1}, 5) == pytest.approx(0.155) and fp.damage({"meh": 1}, 5) == pytest.approx(0.028)
    assert fp.damage({"ok": 1}, 5) == pytest.approx(0.019) and fp.damage({"large_tick_miss": 1}, 5) == pytest.approx(0.09)


def test_only_enough_damage_can_be_a_death_and_the_rest_are_certain_restarts():
    assert fp.classify({"miss": 5}, 5, "")[0] == "reinicio_certo"          # 5 x 0,155 = 0,775 < 0,90: não dá para morrer
    k6, r6 = fp.classify({"miss": 6}, 5, "")
    assert k6 == "pode_ser_morte" and r6 == pytest.approx(0.93 / 0.9)
    assert fp.classify({"miss": 10}, 5, "")[0] == "morte_provavel"
    assert fp.classify({"miss": 2}, 8, "")[0] == "reinicio_certo" and fp.classify({"miss": 3}, 8, "")[0] == "pode_ser_morte"  # HP alto: menos erros bastam
    assert fp.classify({"miss": 20}, 5, "EZ")[0] == "excluida" and fp.classify({"meh": 1}, 5, "PF")[0] == "sd_pf"
    assert fp.classify({"meh": 1}, 5, "SD")[0] == "reinicio_certo"  # SD só morre com miss


def test_progress_is_judged_objects_over_the_maximum():
    assert fp.progress({"great": 10, "ok": 6, "meh": 1, "miss": 2}, {"great": 515}) == pytest.approx(19 / 515)
    assert fp.progress({"great": 600}, {"great": 515}) == 1.0 and fp.progress({}, {}) is None


def test_the_fail_window_is_more_intense_in_the_stream_than_in_the_sparse_part(tmp_path):
    path, times = _bundle(tmp_path)
    mp = fp.load_maps(path, {555})[555]
    assert len(mp["t"]) == 150 and mp["hp"] == 5.0
    sparse, stream = fp.window_features(mp, 50_000), fp.window_features(mp, times[-1])
    assert stream["density"] > 4 * sparse["density"] and stream["stream_share"] > 0.85 and sparse["stream_share"] == 0.0
    assert fp.window_percentile(mp, times[-1]) > 0.95 and fp.window_percentile(mp, 50_000) < fp.window_percentile(mp, times[-1]) - 0.2


def test_end_to_end_classifies_a_restart_and_a_possible_death_and_locates_them(tmp_path):
    from datetime import datetime, timedelta

    path, times = _bundle(tmp_path)
    store = Store(f"sqlite:///{tmp_path}/f.db", tmp_path / "raw")
    rid = store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    base = datetime(2026, 9, 24, 12, 0, 0)

    def score(sid, stats, ended):
        return {"id": sid, "user_id": 7, "beatmap_id": 555, "passed": False, "rank": "F", "accuracy": 0.9, "ended_at": ended.isoformat() + "Z",
                "started_at": (ended - timedelta(seconds=40)).isoformat() + "Z", "mods": [], "statistics": stats, "maximum_statistics": {"great": 150},
                "beatmap": {"id": 555, "beatmapset_id": 1, "version": "V", "status": "ranked", "checksum": "c"},
                "beatmapset": {"id": 1, "artist": "A", "title": "T", "status": "ranked"}}

    # 1: 10 objetos julgados, 1 miss => reinício certo (no início); 2: 145 julgados (o trecho intenso), 6 misses => pode ser morte
    store.ingest_scores([score(1, {"great": 9, "miss": 1}, base), score(2, {"great": 137, "ok": 2, "miss": 6}, base + timedelta(minutes=5))], "recent", rid)
    out = fp.analyze(store, path, tmp_path / "out", "t")
    assert out["input"]["analysed"] == 2 and out["classes"] == {"pode_ser_morte": 1, "reinicio_certo": 1}
    rows = {r["score_id"]: r for r in pq.read_table(tmp_path / "out" / "t" / "fail_points.parquet").to_pylist()}
    assert rows[1]["klass"] == "reinicio_certo" and rows[1]["progress"] == pytest.approx(10 / 150) and rows[1]["t_fail_ms"] == times[9]
    assert rows[2]["klass"] == "pode_ser_morte" and rows[2]["t_fail_ms"] == times[144] and rows[2]["pct_intensity"] > rows[1]["pct_intensity"]
    assert rows[2]["w_stream_share"] > 0.5 and rows[1]["w_stream_share"] == 0.0
    assert out["validation"]["pct_intensity_mean_death"] > out["validation"]["pct_intensity_mean_restart"]
