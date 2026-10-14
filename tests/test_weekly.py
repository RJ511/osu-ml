import time
from datetime import date, datetime, timedelta

from osuml.evolution import weekly as wk

TODAY = date(2026, 10, 14)  # quarta; a semana começa a 12/10


def play(bid, days_ago, acc, passed=True, stars=6.0, mods="", label=None):
    d = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time())
    return {"beatmap_id": bid, "map": label or f"mapa {bid}", "passed": passed, "accuracy": acc, "ended_at": d, "mods": mods, "mods_key": mods, "raw": {"stars": stars}}


def test_week_helpers_and_nomod_stats():
    assert wk.week_monday(TODAY) == date(2026, 10, 12) and wk.week_id(TODAY) == "2026-W42"
    assert wk.week_id(date(2026, 1, 1)) == "2026-W01" and wk.week_id(date(2025, 12, 29)) == "2026-W01"  # a semana ISO atravessa o ano
    plays = [play(i, 3 + i, 0.95, stars=5.0 + i * 0.1) for i in range(10)] + [play(99, 2, 0.92, stars=6.4), play(98, 2, 0.99, mods="DT", stars=8.0),
                                                                              play(97, 2, 0.5, passed=False, stars=7.5)]
    st = wk.nomod_stats(plays, TODAY)
    assert st["s_max"] == 6.4 and 5.7 <= st["s90"] <= 6.0  # DT e falhas não contam; s_max conta passes >= 90 %
    assert wk.default_band({"s90": 6.0}) == [5.8, 6.5] and wk.default_band({"s90": None}) is None
    assert wk.nomod_stats([], TODAY)["s90"] is None


def test_first_time_series_only_counts_first_ever_plays_nomod_in_the_band():
    plays = [play(1, 20, 0.90), play(1, 2, 0.99),                                   # mapa 1: a 1.ª jogada foi há 20 dias
             play(2, 2, 0.94, stars=6.1), play(3, 1, 0.80, passed=False, stars=6.3),  # novos esta semana na faixa: 1 passe, 1 falha
             play(4, 1, 0.97, stars=7.5),                                             # fora da faixa
             play(5, 1, 0.97, stars=6.0, mods="DT")]                                  # com DT: não conta
    s = wk.first_time_series(plays, [5.8, 6.5], TODAY, weeks=2)
    assert s[-1]["n"] == 2 and s[-1]["n_pass"] == 1 and s[-1]["acc_median"] == 0.94 and s[-1]["pass_rate"] == 0.5
    assert s[0]["n"] == 0 and s[0]["acc_median"] is None  # a semana anterior (o mapa 1 foi há 20 dias)


class FakeRec:
    def __init__(self):
        self.calls = []

    def _limits(self, uid, mods):
        return {"ar_cap": 10.1, "stars_ceiling": 6.37 if mods == "DT" else 7.0}

    def band_candidates(self, uid, lo, hi, *, mods="", min_p=0.0, max_ar=None, unseen=True, n=20, order="p", seed=0):
        self.calls.append((lo, hi, mods, order, seed))
        dom = ["aim", "speed", "stamina", "reading"]
        return [{"beatmap_id": 1000 + i + (500 if mods else 0) + int(lo * 10) * 7, "label": f"cand {mods or 'NM'} {i}", "url": "u", "stars": round(lo + 0.05 * i, 2),
                 "ar": 9.6, "p_pass": 0.6, "acc_pass": 0.93, "dominant": dom[i % 4], "mods": mods} for i in range(min(n, 12))]


def test_build_plan_is_deterministic_per_week_and_has_the_session_structure():
    plays = [play(i, 3, 0.94 + (i % 3) * 0.01, stars=5.8 + i * 0.05, label=f"meu {i}") for i in range(6)] + [play(50, 5, 0.985, stars=4.8, label="aquecimento")]
    stats = wk.nomod_stats(plays, TODAY)
    rec1, rec2 = FakeRec(), FakeRec()
    p1 = wk.build_plan(rec1, 7, plays, TODAY, [5.8, 6.5], stats)
    p2 = wk.build_plan(rec2, 7, plays, TODAY, [5.8, 6.5], stats)
    assert [m["beatmap_id"] for m in p1["test"]["maps"]] == [m["beatmap_id"] for m in p2["test"]["maps"]] and len(p1["test"]["maps"]) == wk.TEST_MAPS
    assert rec1.calls[0][3] == "random" and rec1.calls[0][4] == int("20261012") + 7  # semente = segunda-feira da semana + jogador
    assert [s["key"] for s in p1["sessions"]] == ["A", "B", "C", "D"] and all(len(s["blocks"]) == 3 and s["rules"] for s in p1["sessions"])
    a = p1["sessions"][0]["maps"]
    assert 1 <= len(a) <= 3 and all(0.92 <= m["best_acc"] < 0.97 and m["target"] > m["best_acc"] for m in a)
    assert p1["sessions"][1]["maps"][0]["stars"] >= stats["s_max"] + 0.1  # esticar: acima do tecto
    c_maps = p1["sessions"][2]["maps"]
    assert len({m["dominant"] for m in c_maps}) >= 3 and not ({m["beatmap_id"] for m in c_maps} & {m["beatmap_id"] for m in p1["test"]["maps"]})
    d = p1["sessions"][3]["maps"]
    assert d and all(m["mods"] == "DT" and m["ar"] >= 9.4 for m in d) and len(d) <= 3
    assert p1["sessions"][0]["blocks"][0]["name"] == "Aquecimento" and p1["sessions"][0]["blocks"][0]["maps"]  # aquecimento com os teus mapas fáceis


def test_evaluate_uses_the_first_play_of_the_week_and_matching_mods():
    plan = {"week": "2026-W42", "week_start": "2026-10-12",
            "test": {"band": [5.8, 6.5], "maps": [{"beatmap_id": 1}, {"beatmap_id": 2}, {"beatmap_id": 3}]},
            "sessions": [{"key": "D", "maps": [{"beatmap_id": 9, "mods": "DT"}], "progress": {}}]}
    plays = [play(1, 1, 0.80, passed=False), play(1, 0, 0.95),   # 1.ª jogada falhou, depois passou
             play(2, 1, 0.93),                                   # passou à 1.ª
             play(9, 1, 0.99),                                   # mapa da sessão DT jogado SEM DT: não conta
             play(3, 10, 0.97)]                                  # semana passada: não conta
    out = wk.evaluate(plan, plays)
    assert out["test"]["summary"] == {"done": 2, "total": 3, "first_pass": 1, "first_acc_median": 0.93}
    assert out["test"]["maps"][0]["status"]["first_passed"] is False and out["test"]["maps"][0]["status"]["best_acc"] == 0.95
    assert out["sessions"][0]["progress"] == {"done": 0, "total": 1}
    plays.append(play(9, 0, 0.9, mods="DT"))
    assert wk.evaluate(plan, plays)["sessions"][0]["progress"] == {"done": 1, "total": 1}


def test_planner_builds_in_background_then_serves_the_saved_plan_and_keeps_the_band_fixed(tmp_path):
    plays = [play(i, 3, 0.94, stars=5.8 + i * 0.05) for i in range(8)]
    planner = wk.WeeklyPlanner(FakeRec(), tmp_path)
    s1 = planner.state(7, plays, {}, TODAY)
    assert s1["status"] == "building" and s1["band"] == wk.default_band(wk.nomod_stats(plays, TODAY))
    s2 = s1
    for _ in range(200):
        s2 = planner.state(7, plays, {}, TODAY)
        if s2["status"] == "ready":
            break
        time.sleep(0.05)
    assert s2["status"] == "ready" and s2["test"]["summary"]["total"] == wk.TEST_MAPS and (tmp_path / "weekly" / "7_2026-W42.json").exists()
    better = [play(i, 3, 0.97, stars=7.0 + i * 0.05) for i in range(8)]  # o nível sobe, mas a faixa fica fixa (semanas comparáveis)
    assert planner.state(7, better, {}, TODAY)["band"] == s1["band"]
    assert planner.state(7, better, {"test_band": [6.0, 6.6]}, TODAY)["band"] == [6.0, 6.6]  # goal.json tem prioridade
    nxt = planner.state(7, plays, {}, TODAY + timedelta(days=7))  # semana seguinte: o histórico traz o teste desta
    assert [h["week"] for h in nxt["history"]] == ["2026-W42"]


def test_planner_without_recommender_or_data_degrades_gracefully(tmp_path):
    s = wk.WeeklyPlanner(None, tmp_path).state(7, [play(1, 3, 0.94)], {}, TODAY)
    assert s["status"] == "unavailable" and s["band"] is None and "faixa" in s["message"]
    s2 = wk.WeeklyPlanner(None, tmp_path).state(7, [play(i, 3, 0.94, stars=6.0) for i in range(8)], {}, TODAY)
    assert s2["status"] == "unavailable" and s2["message"].startswith("recomendador indisponível")
