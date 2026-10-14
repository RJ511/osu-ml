import json
from datetime import date, datetime, timedelta

from osuml.evolution.core import MIN_EVIDENCE, Evolution

TODAY = date(2026, 10, 14)  # quarta-feira


def play(days_ago, score, acc=0.95, passed=True):
    d = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time())
    return {"ended_at": d, "passed": passed, "accuracy": acc,
            "axes": {a: {"score": score, "grade": "A", "pct": 10} for a in ("aim", "speed", "stamina", "reading", "stars")}}


def test_level_needs_enough_evidence_and_ignores_fails_and_low_accuracy():
    plays = [play(1, 60) for _ in range(MIN_EVIDENCE - 1)] + [play(1, 90, passed=False), play(1, 90, acc=0.8)]
    assert Evolution.levels_by_week(plays, TODAY)["aim"][-1]["level"] is None  # poucos passes válidos
    plays.append(play(1, 60))
    last = Evolution.levels_by_week(plays, TODAY)["aim"][-1]
    assert last["level"] == 60 and last["n"] == MIN_EVIDENCE


def test_trend_is_stable_below_threshold_and_directional_above():
    base = [{"week": str(i), "n": 12, "level": 50.0} for i in range(10)]
    assert Evolution.trend(base)["direction"] == "estavel"
    up = [dict(p) for p in base]
    up[-1]["level"] = 55.0
    assert Evolution.trend(up) == {"direction": "sobe", "delta": 5.0, "low_confidence": False}
    up[-1]["level"] = 44.0
    assert Evolution.trend(up)["direction"] == "desce"
    up[-1]["level"] = None
    assert Evolution.trend(up)["direction"] == "sem_dados"


def test_consistency_counts_distinct_days_and_streak():
    # semana atual (seg 12/10): joga terça e quarta; semana anterior: 3 dias; antes: 1 dia
    plays = [play(1, 50), play(2 - 1, 50), play(0, 50)]
    plays += [play(k, 50) for k in (4, 5, 6)]  # 8, 9 e 10 de out. = semana anterior, 3 dias distintos
    plays += [play(14 + 1, 50)]
    c = Evolution.consistency(plays, TODAY, weekly_days=3)
    assert c["this_week_days"] == 2 and c["days_since_last"] == 0
    assert c["weeks"][-2]["days"] == 3 and c["weeks"][-2]["met"]
    assert c["streak_weeks"] == 1  # a semana atual ainda não cumpriu a meta


def test_goal_file_missing_or_invalid_gives_empty_goal(tmp_path):
    ev = Evolution(None, tmp_path / "nao_existe.json")
    assert ev.goal() == {} and "error" in ev.state()
    (tmp_path / "g.json").write_text("{nao json", encoding="utf-8")
    assert Evolution(None, tmp_path / "g.json").goal() == {}
    (tmp_path / "ok.json").write_text(json.dumps({"user_id": 1}), encoding="utf-8")
    assert Evolution(None, tmp_path / "ok.json").goal() == {"user_id": 1}


def test_current_level_survives_a_pause_and_reports_its_age():
    pts = [{"week": str(i), "n": 8, "level": 70.0} for i in range(3)] + [{"week": "3", "n": 0, "level": None}] * 2
    levels, age = Evolution.current_levels({"aim": pts, "speed": [{"week": "0", "n": 0, "level": None}]})
    assert levels == {"aim": 70.0, "speed": None} and age == {"aim": 2, "speed": None}


def test_rival_detail_picks_unplayed_nomod_maps_in_reach_sorted_by_pp(tmp_path):
    def rp(bid, pp, aim, mods="", acc=0.96, passed=True):
        ax = {a: {"score": aim, "grade": "A", "pct": 5} for a in ("aim", "speed", "stamina", "reading", "stars")}
        return {"beatmap_id": bid, "map": f"m{bid}", "pp": pp, "accuracy": acc, "passed": passed, "ended_at": datetime(2026, 10, 1),
                "mods_key": mods, "axes": ax, "raw": {"stars": 5.0}}

    class FakeExplorer:
        store = None

        def player(self, uid):
            return {"plays": [rp(1, 200, 72), rp(2, 150, 71), rp(3, 300, 95), rp(4, 250, 72, mods="DT"), rp(5, 260, 72, acc=0.85),
                              rp(6, 220, 72)], "player": {}}

        def _profiles(self):
            return []

    ev = Evolution(FakeExplorer(), tmp_path / "g.json")
    ev._user_raw = lambda uid: {}
    d = ev.rival_detail(9, {"aim": 70.0, "speed": 70.0, "stamina": 70.0, "reading": 70.0}, [{"beatmap_id": 6}], date(2026, 10, 14))
    assert [m["beatmap_id"] for m in d["maps"]] == [1, 2]  # 3 demasiado difícil, 4 com DT, 5 acc baixa, 6 já jogado
    assert d["maps"][0]["gaps"]["aim"] == 2.0 and d["maps_total"] == 3
