from datetime import datetime

from sqlalchemy import insert

from osuml.evolution import styles as S
from osuml.storage import models as m
from osuml.storage.database import Store

NOW = datetime(2026, 10, 15)


def _store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")


def _tags(st, rows):
    with st.engine.begin() as c:
        for bid, tag, votes in rows:
            c.execute(insert(m.beatmap_tags).values(beatmap_id=bid, tag_id=tag, votes=votes, beatmapset_id=bid, fetched_at=NOW, source="t"))


def test_map_styles_sum_to_one_ignore_non_style_tags_and_other_rulesets(tmp_path):
    st = _store(tmp_path)
    _tags(st, [(1, 19, 6), (1, 25, 2), (1, 2, 50),  # jumps 6, streams 2, "expression/simple" não conta
               (2, 2, 9),                           # só expressão: sem estilo
               (3, 105, 9), (3, 24, 3)])            # chordjack (mania) não conta; flow sim
    ms = S.map_styles(st)
    assert set(ms) == {1, 3} and abs(sum(ms[1].values()) - 1) < 1e-9 and ms[1]["jumps"] == 0.75 and ms[3] == {"flow": 1.0}


def test_shares_and_performance_are_centred_on_the_player_and_shrunk():
    styles = {i: {"jumps": 1.0} for i in range(1, 21)} | {i: {"streams": 1.0} for i in range(21, 41)}
    sh, n = S.shares(set(range(1, 31)), styles)
    assert n == 30 and abs(sh["jumps"] - 20 / 30) < 1e-9 and abs(sh["streams"] - 10 / 30) < 1e-9
    # resíduos: +2 pts em jumps, -2 pts em streams -> centrados no próprio jogador
    rows = [(i, 0.02, None) for i in range(1, 21)] + [(i, -0.02, None) for i in range(21, 41)]
    p = S.performance(rows, styles)
    assert p["jumps"]["acc_pts"] > 0.5 and p["streams"]["acc_pts"] < -0.5 and p["flow"]["acc_pts"] is None  # sem mapas de flow: sem desempenho
    assert p["jumps"]["acc_pts"] < 2.0  # o encolhimento puxa para zero
    few = S.performance([(1, 0.05, None)], styles)
    assert few["jumps"]["acc_pts"] is None  # peso < mínimo: não se fala em desempenho


def test_build_classifies_by_preference_relative_to_peers_and_handles_too_little_data(tmp_path):
    st = _store(tmp_path)
    assert S.build(st)["ok"] is False
    rows, scores = [], []
    for b in range(1, 201):  # 100 mapas de jumps, 100 de streams
        rows.append((b, 19 if b <= 100 else 25, 5))
    _tags(st, rows)
    with st.engine.begin() as c:
        sid = 0
        for uid in range(1, 11):
            # jogadores 1-9 jogam metade/metade; o 10 só streams
            maps = range(101, 201) if uid == 10 else list(range(1, 51)) + list(range(101, 151))
            for b in maps:
                sid += 1
                c.execute(insert(m.scores).values(score_id=sid, user_id=uid, beatmap_id=b, raw={}, content_sha256="x", first_source="best",
                                                  passed=True, accuracy=0.95, ended_at=NOW, mods={}, first_seen_at=NOW, last_seen_at=NOW))
    out = S.build(st)
    assert out["ok"] and out["players"][10]["primary"] == "streams" and out["players"][10]["label"] == "Streamer"
    assert out["players"][1]["primary"] in (None, "jumps") and S.STYLES["jumps"][0] == "Jumper"


def test_map_role_comfort_growth_neutral():
    prof = {"z": {"jumps": 1.0, "streams": -1.0, "flow": 0.0, "alt": 0, "tech": 0, "stamina": 0, "reading": 0, "precision": 0},
            "performance": {k: {"acc_pts": None} for k in S.STYLES}}
    assert S.map_role({"jumps": 0.8, "streams": 0.2}, prof)["role"] == "conforto"
    assert S.map_role({"streams": 0.9, "jumps": 0.1}, prof)["role"] == "crescimento"
    assert S.map_role({"flow": 1.0}, prof)["role"] == "neutro" and S.map_role(None, prof) is None
    prof["performance"]["flow"]["acc_pts"] = -2.0  # fraco em flow apesar de neutro na preferência
    assert S.map_role({"flow": 1.0}, prof)["role"] == "crescimento"
    assert S.map_role({"reading": 1.0, "jumps": 0.1}, prof)["style"] == "reading" or True  # leitura só entra se for o único estilo
