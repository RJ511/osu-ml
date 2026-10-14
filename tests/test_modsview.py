from datetime import datetime

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osuml.analysis.dt_check import _auc, apply_platt, ece, fit_platt
from osuml.recommend import modsview
from osuml.storage import models as m
from osuml.storage.database import Store


def test_effective_od_shrinks_hit_windows_by_the_clock_rate():
    assert modsview.effective_od(9.0, 1.0) == pytest.approx(9.0)
    assert modsview.effective_od(9.0, 1.5) == pytest.approx(10.4444, abs=1e-3)  # janela de 300: 26 ms -> 17,3 ms
    assert modsview.effective_od(0.0, 1.5) == pytest.approx((80 - 80 / 1.5) / 6)


class FakeScale:
    def score(self, field, value):
        return float(value) * 10.0  # monótona e simples: o que interessa é com que valores é chamada


def test_dt_view_scales_density_length_od_and_aligns_with_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr("osuml.beatmaps.skills.ReferenceScale.from_parquet", classmethod(lambda cls, p: FakeScale()))
    cols = ["stars", "aim", "speed", "reading", "density", "ar", "ar_mod", "cs", "od", "hp", "hp_mod", "n_objects", "length_ms"]
    rows = {"beatmap_id": [10, 10, 20], "mods": ["", "DT", ""]}
    nomod = dict(stars=5.0, aim=3.0, speed=2.0, reading=1.0, density=4.0, ar=9.0, ar_mod=9.0, cs=4.0, od=9.0, hp=5.0, hp_mod=5.0, n_objects=300, length_ms=90_000)
    dt = dict(stars=7.5, aim=4.5, speed=3.0, reading=2.0, density=4.0, ar=9.0, ar_mod=10.5, cs=4.0, od=9.0, hp=5.0, hp_mod=5.0, n_objects=300, length_ms=90_000)
    for c in cols:
        rows[c] = [nomod[c], dt[c], nomod[c]]
    pq.write_table(pa.table(rows), tmp_path / "cat.parquet")
    ids = np.array([5, 10, 20, 30], dtype=np.int64)
    v = modsview.build_view(tmp_path / "cat.parquet", ids, tmp_path / "pool.parquet", "DT")
    assert v["valid"].tolist() == [False, True, False, False]  # só o mapa 10 tem linha DT
    names = ["stars", "aim", "speed", "log_reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_s"]
    x = dict(zip(names, v["x"][1]))
    assert x["stars"] == pytest.approx(7.5) and x["density"] == pytest.approx(6.0)  # 4 objetos/s x 1,5
    assert x["ar"] == pytest.approx(10.5) and x["od"] == pytest.approx(10.4444, abs=1e-3) and x["length_s"] == pytest.approx(60.0)  # 90 s / 1,5
    assert x["cs"] == pytest.approx(4.0) and x["n_objects"] == pytest.approx(300.0)
    assert v["axis"][1].tolist() == pytest.approx([45.0, 30.0, 60.0, 20.0, 75.0])  # aim, speed, stamina (densidade x1,5), reading, stars


def test_mods_state_only_counts_plays_with_exactly_those_mods(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")
    now = datetime(2026, 10, 1)
    with s.engine.begin() as c:
        for i, (bid, passed, acc, mods) in enumerate(((10, True, 0.95, "DT"), (10, False, 0.80, "DT"), (20, True, 0.97, ""), (30, True, 0.90, "DT,HD"),
                                                       (40, True, 0.91, "NC"), (50, False, 0.60, "DT"))):
            c.execute(m.scores.insert().values(score_id=i + 1, user_id=1, beatmap_id=bid, passed=passed, accuracy=acc, mod_acronyms=mods, raw={},
                                                content_sha256="x", first_source="best", first_seen_at=now, last_seen_at=now))
    ids = np.array([10, 20, 30, 40, 50], dtype=np.int64)
    played, best = modsview.mods_state(s, 1, ids, "DT")
    assert played == {0, 3, 4}  # 10 (DT), 40 (NC=DT), 50 (DT, só falhou); 20 sem mods e 30 com HD ficam de fora
    assert best == {0: 0.95, 3: 0.91}


def test_platt_fit_recovers_a_known_miscalibration_and_auc_ece_behave():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, size=20_000)
    z = np.log(p / (1 - p))
    true = 1 / (1 + np.exp(-(-0.3 + 0.4 * z)))
    y = (rng.uniform(size=len(p)) < true).astype(float)
    a, b = fit_platt(p, y)
    assert a == pytest.approx(-0.3, abs=0.08) and b == pytest.approx(0.4, abs=0.06)
    assert ece(apply_platt(p, a, b), y) < ece(p, y)
    assert _auc(y, p) > 0.6 and _auc(np.array([1, 1, 1]), np.array([0.1, 0.2, 0.3])) is None
    assert _auc(np.array([0, 1, 0, 1]), np.array([0.1, 0.9, 0.2, 0.8])) == pytest.approx(1.0)


def test_normalize_mods_accepts_combinations_and_rejects_invalid_ones():
    n = modsview.normalize_mods
    assert n("") == "" and n("NM") == "" and n(["NM"]) == "" and n(None) == ""
    assert n("dt") == "DT" and n("HD+DT") == "DT,HD" and n(["HD", "NC"]) == "DT,HD" and n("hd, hr") == "HD,HR"
    assert n("FL") is None and n("DT,HT") is None and n("HR,EZ") is None and n("DT,XX") is None
    assert modsview.clock_of("DT,HD") == 1.5 and modsview.clock_of("HT") == 0.75 and modsview.clock_of("HD") == 1.0
    assert modsview.stable_key("NC,CL,NF,HD") == "DT,HD" and modsview.stable_key("SO") == ""


def test_hr_view_caps_cs_and_od_and_player_limits_come_from_demonstrated_passes(tmp_path, monkeypatch):
    monkeypatch.setattr("osuml.beatmaps.skills.ReferenceScale.from_parquet", classmethod(lambda cls, p: FakeScale()))
    cols = ["stars", "aim", "speed", "reading", "density", "ar", "ar_mod", "cs", "od", "hp", "hp_mod", "n_objects", "length_ms"]
    base = dict(stars=5.0, aim=3.0, speed=2.0, reading=1.0, density=4.0, ar=9.0, ar_mod=9.0, cs=4.0, od=8.0, hp=5.0, hp_mod=5.0, n_objects=300, length_ms=90_000)
    hr = {**base, "ar_mod": 10.0, "hp_mod": 7.0, "stars": 5.4}
    dt = {**base, "ar_mod": 10.4, "stars": 7.5}
    rows = {"beatmap_id": [10, 10, 10, 20, 20], "mods": ["", "HR", "DT", "", "DT"]}
    vals = [base, hr, dt, {**base, "stars": 6.0}, {**dt, "stars": 8.6, "ar_mod": 11.0}]
    for c in cols:
        rows[c] = [v[c] for v in vals]
    pq.write_table(pa.table(rows), tmp_path / "cat.parquet")
    v = modsview.build_view(tmp_path / "cat.parquet", np.array([10, 20], dtype=np.int64), tmp_path / "pool.parquet", "HR")
    names = ["stars", "aim", "speed", "log_reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_s"]
    x = dict(zip(names, v["x"][0]))
    assert x["cs"] == pytest.approx(5.2) and x["od"] == pytest.approx(10.0) and x["ar"] == pytest.approx(10.0) and x["hp"] == pytest.approx(7.0)  # HR: CS x1,3; OD x1,4 (máx. 10)
    assert v["valid"].tolist() == [True, False]

    s = Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")
    now = datetime(2026, 10, 1)
    with s.engine.begin() as c:
        for i, (bid, mods, acc, passed) in enumerate(((10, "", 0.95, True), (20, "", 0.94, True), (10, "DT", 0.92, True), (20, "DT", 0.80, True), (10, "NC", 0.93, True))):
            c.execute(m.scores.insert().values(score_id=i + 1, user_id=1, beatmap_id=bid, passed=passed, accuracy=acc, mod_acronyms=mods, raw={}, content_sha256="x",
                                                first_source="best", first_seen_at=now, last_seen_at=now))
    lk = modsview.CatalogLookup(tmp_path / "cat.parquet")
    lim = modsview.player_limits(s, 1, lk, "DT")
    assert lim["ar_cap"] == pytest.approx(10.4 + modsview.AR_MARGIN)  # o passe a 80 % não conta; AR efetivo máx. dos passes ≥ 90 %: 10,4 (DT no mapa 10)
    assert lim["n_passes_same_mods"] == 2 and lim["stars_ceiling"] == pytest.approx(7.5 + modsview.STARS_MARGIN)  # DT e NC contam como DT
    cautious = modsview.player_limits(s, 1, lk, "HD")  # nunca passou com HD: tecto cauteloso (melhor passe nomod − 1★)
    assert cautious["n_passes_same_mods"] == 0 and cautious["stars_ceiling"] == pytest.approx(6.0 - 1.0)


def test_recommend_many_merges_the_best_of_each_mods_selection_and_keeps_errors(monkeypatch):
    from osuml.recommend.core import Recommender

    rec = Recommender.__new__(Recommender)
    rec.log_predictions = False

    def fake(user_id, skills, n=20, focus="equilibrado", mods="", ar_cap=None, _log=True):
        if mods == "HR":
            return {"error": "sem vista HR"}
        items = [{"beatmap_id": i, "score": 1.0 - i / 100 - (0.05 if mods else 0), "mods": mods} for i in range(1, 6)]
        return {"items": items, "counts": {"novo": 5}, "limits": {"ar_cap_used": 10.1}, "mods_calibration": None, "notes": ["n"], "skills": skills, "mods": mods}

    rec.recommend = fake
    out = rec.recommend_many(1, ["aim"], 6, mods_list=["", "DT", "HR"])
    assert out["mods"] == ["", "DT"] and len(out["items"]) == 6 and {it["mods"] for it in out["items"]} == {"", "DT"}
    assert [it["score"] for it in out["items"]] == sorted((it["score"] for it in out["items"]), reverse=True)
    assert out["errors"] == {"HR": "sem vista HR"} and set(out["by_mods"]) == {"NM", "DT"}
    only = rec.recommend_many(1, ["aim"], 3, mods_list=["HR"])
    assert only == {"error": "sem vista HR"}
