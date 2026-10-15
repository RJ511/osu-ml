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


def _plan_for_reject():
    plays = [play(i, 3, 0.94 + (i % 3) * 0.01, stars=5.8 + i * 0.05, label=f"meu {i}") for i in range(8)] + [play(50, 5, 0.985, stars=4.8, label="aquecimento")]
    stats = wk.nomod_stats(plays, TODAY)
    rec = FakeRec()
    return rec, plays, stats, wk.build_plan(rec, 7, plays, TODAY, [5.8, 6.5], stats)


def test_reject_swaps_a_map_for_one_with_the_same_role_without_repeating_anything():
    rec, plays, stats, plan = _plan_for_reject()
    before = wk._plan_ids(plan)
    old = plan["test"]["maps"][1]["beatmap_id"]
    res = wk.replace_in_plan(rec, 7, plan, old, plays, {old})
    assert res["replaced"] and res["role"] == "test" and res["new"]["beatmap_id"] not in before | {old}
    assert plan["test"]["maps"][1]["beatmap_id"] == res["new"]["beatmap_id"] and old not in wk._plan_ids(plan)
    assert plan["rejected"][0] == {"beatmap_id": old, "role": "test", "replaced_by": res["new"]["beatmap_id"]}
    # sessão C: o substituto é um mapa novo do mesmo papel (prefere o mesmo eixo dominante, se houver)
    c_old = plan["sessions"][2]["maps"][0]
    rc = wk.replace_in_plan(rec, 7, plan, c_old["beatmap_id"], plays, {old, c_old["beatmap_id"]})
    assert rc["replaced"] and rc["role"] == "C" and rc["new"]["beatmap_id"] not in before | {old, c_old["beatmap_id"]}
    # sessão D mantém DT; um mapa que não está no plano não se troca
    d_old = plan["sessions"][3]["maps"][0]
    rd = wk.replace_in_plan(rec, 7, plan, d_old["beatmap_id"], plays, {d_old["beatmap_id"]})
    assert rd["replaced"] and (rd["new"] is None or rd["new"]["mods"] == "DT")
    assert wk.replace_in_plan(rec, 7, plan, 999999, plays, set())["replaced"] is False


def test_reject_does_not_swap_a_map_already_played_and_removes_when_there_is_no_substitute():
    rec, plays, stats, plan = _plan_for_reject()
    a = plan["sessions"][0]["maps"]
    played = plays + [play(a[0]["beatmap_id"], 0, 0.9)]
    res = wk.replace_in_plan(rec, 7, plan, a[0]["beatmap_id"], played, {a[0]["beatmap_id"]})
    assert res == {"replaced": False, "reason": "já jogaste este mapa esta semana"}
    n = len(a)
    for m in list(plan["sessions"][0]["maps"]):  # esgota os substitutos: no fim remove em vez de insistir
        wk.replace_in_plan(rec, 7, plan, m["beatmap_id"], plays, {m["beatmap_id"]})
    assert len(plan["sessions"][0]["maps"]) <= n


def test_rejected_maps_never_enter_a_new_plan_and_planner_persists_the_swap(tmp_path):
    rec, plays, stats, plan = _plan_for_reject()
    banned = {m["beatmap_id"] for m in plan["test"]["maps"][:2]} | {m["beatmap_id"] for m in plan["sessions"][2]["maps"][:2]}
    plan2 = wk.build_plan(rec, 7, plays, TODAY, [5.8, 6.5], stats, rejected=banned)
    assert not (wk._plan_ids(plan2) & banned)
    planner = wk.WeeklyPlanner(FakeRec(), tmp_path)
    for _ in range(200):
        s = planner.state(7, plays, {}, TODAY)
        if s["status"] == "ready":
            break
        time.sleep(0.05)
    bid = s["test"]["maps"][0]["beatmap_id"]
    out = planner.reject(7, bid, plays, {bid}, TODAY)
    assert out["replaced"]
    again = planner.state(7, plays, {}, TODAY, rejected={bid})
    assert bid not in {m["beatmap_id"] for m in again["test"]["maps"]} and again["test"]["maps"][0]["beatmap_id"] == out["new"]["beatmap_id"]
    assert wk.WeeklyPlanner(None, tmp_path).reject(7, bid, plays, set(), TODAY)["replaced"] is False


def test_pick_styled_takes_distinct_growth_styles_first_then_comfort():
    cands = [{"beatmap_id": i, "dominant": "aim"} for i in range(1, 31)]
    maps = {**{i: {"jumps": 1.0} for i in range(1, 21)}, **{21: {"streams": 0.9, "jumps": 0.1}, 22: {"streams": 0.8, "jumps": 0.2},
                                                           23: {"tech": 1.0}, 24: {"alt": 0.6, "jumps": 0.4}, 25: {"flow": 0.45, "jumps": 0.55}}}
    prof = {"primary": "jumps", "z": {"jumps": 0.6, "streams": -0.5, "tech": -0.2, "alt": -0.5, "flow": -0.55, "stamina": -0.6}}
    out = wk._pick_styled(cands, {"maps": maps, "profile": prof}, n=5, growth=3)
    ids = [c["beatmap_id"] for c in out]
    assert len(ids) == 5 and len(set(ids)) == 5
    growth = [i for i in ids if i in (21, 22, 23, 24)]
    assert len(growth) == 3 and len({"streams" if i in (21, 22) else "tech" if i == 23 else "alt" for i in growth}) == 3  # 3 estilos distintos
    assert all(i <= 20 or i in (26, 27, 28, 29, 30) or i in growth for i in ids) and 25 not in ids  # flow com peso < 0,5 não conta como estilo claro
    assert wk._pick_styled([{"beatmap_id": 99, "dominant": "aim"}], {"maps": {}, "profile": prof}, n=5)[0]["beatmap_id"] == 99  # sem tags: cai para a diversidade por eixo


def _complete(plan, plays, keys=("A", "B", "C")):
    """Marca como jogados (com plays fictícias) os mapas das sessões `keys` da última ronda e devolve o plano avaliado."""
    out = []
    for s in plan["sessions"]:
        if s["key"] in keys:
            out += [play(m["beatmap_id"], 0, 0.9, stars=m["stars"], mods=m.get("mods") or "") for m in s["maps"]]
    return wk.evaluate(plan, plays + out)


def test_round_extends_with_new_maps_when_a_b_c_are_done_and_d_is_optional():
    rec, plays, stats, plan = _plan_for_reject()
    assert wk.rounds_done(wk.evaluate(plan, plays)) is False
    done = _complete(plan, plays)
    assert wk.rounds_done(done) is True  # D por jogar não trava
    before = wk._plan_ids(plan)
    assert wk.extend_round(rec, 7, plan, plays, set(), None) is True
    keys = [(s["key"], s.get("round", 1)) for s in plan["sessions"]]
    assert ("A", 2) in keys and ("C", 2) in keys and ("D", 2) not in keys
    new_ids = {m["beatmap_id"] for s in plan["sessions"] if s.get("round") == 2 for m in s["maps"]}
    assert new_ids and not (new_ids & before)  # mapas novos, nenhum repetido nesta semana


def test_session_scale_and_style_pick_with_rival_guides_growth_styles():
    assert wk._scale({"session_minutes": 30}) == 1.5 and wk._scale({}) == 1.0 and wk._scale({"session_minutes": 90}) == 2.0 and wk._scale({"session_minutes": "x"}) == 1.0
    rec, plays, stats, plan = _plan_for_reject()
    big, _ = wk._build_sessions(rec, 7, plays, [5.8, 6.5], stats, 10.1, set(), None, set(), scale=1.5)
    assert big[2]["minutes"] == 30 and len(big[2]["maps"]) == 8 and len(big[1]["maps"]) == 2 and len(big[0]["maps"]) <= 5
    cands = [{"beatmap_id": i, "dominant": "aim"} for i in range(1, 41)]
    maps = {**{i: {"jumps": 1.0} for i in range(1, 21)}, **{i: {"streams": 1.0} for i in (21, 22)}, **{i: {"tech": 1.0} for i in (23, 24)},
            25: {"alt": 1.0}, 26: {"stamina": 1.0}}
    z = {"jumps": 0.6, "streams": -0.5, "tech": -0.2, "alt": -0.5, "flow": -0.55, "stamina": -0.6}
    me = {"primary": "jumps", "z": z, "share": {"jumps": 0.68, "streams": 0.15, "tech": 0.08, "alt": 0.02, "flow": 0.03, "stamina": 0.02}}
    rv = {"share": {"jumps": 0.55, "streams": 0.15, "tech": 0.15, "alt": 0.05, "flow": 0.03, "stamina": 0.02}}
    pick = wk._pick_styled(cands, {"maps": maps, "profile": me, "rival": rv}, n=5, growth=3, rot=0)
    dom_of = lambda i: next(iter(maps[i])) if i in maps else None  # noqa: E731
    styles_picked = [dom_of(c["beatmap_id"]) for c in pick if dom_of(c["beatmap_id"]) != "jumps"]
    assert styles_picked[0] == "tech"  # o rival joga mais tech do que tu: é o 1.º estilo de crescimento
    assert len(set(styles_picked)) == len(styles_picked) == 3


def _write_week(tmp_path, uid, week_start, acc, band=(5.8, 6.5), done=4):
    import json
    wid = wk.week_id(week_start)
    maps = [{"beatmap_id": 1000 + i + int(week_start.strftime("%j")) * 10} for i in range(5)]
    plan = {"week": wid, "week_start": week_start.isoformat(), "stats": {}, "test": {"band": list(band), "maps": maps}, "sessions": []}
    d = tmp_path / "weekly"
    d.mkdir(exist_ok=True)
    (d / f"{uid}_{wid}.json").write_text(json.dumps(plan), encoding="utf-8")
    return [play(m["beatmap_id"], (TODAY - week_start).days - 1, acc, stars=6.0) for m in maps[:done]]


def test_band_rises_after_three_consecutive_weeks_above_the_threshold_and_not_otherwise(tmp_path):
    from datetime import timedelta
    planner = wk.WeeklyPlanner(FakeRec(), tmp_path)
    mon = wk.week_monday(TODAY)
    weeks = [mon - timedelta(days=7 * k) for k in (3, 2, 1)]
    plays = []
    for w in weeks:
        plays += _write_week(tmp_path, 7, w, 0.95)
    band, info = planner.maybe_raise_band(7, plays, TODAY, [5.8, 6.5])
    assert info == {"from": [5.8, 6.5], "to": [6.1, 6.8]} and band == [6.1, 6.8]
    assert wk.json.loads((tmp_path / "weekly" / "7_test_band.json").read_text(encoding="utf-8"))["band"] == [6.1, 6.8]
    # uma semana com accuracy baixa (ou poucos mapas, ou faixa diferente) impede a subida
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    p2 = wk.WeeklyPlanner(FakeRec(), tmp2)
    pl = _write_week(tmp2, 7, weeks[0], 0.95) + _write_week(tmp2, 7, weeks[1], 0.90) + _write_week(tmp2, 7, weeks[2], 0.95)
    assert p2.maybe_raise_band(7, pl, TODAY, [5.8, 6.5])[1] is None
    tmp3 = tmp_path / "c"
    tmp3.mkdir()
    p3 = wk.WeeklyPlanner(FakeRec(), tmp3)
    pl = _write_week(tmp3, 7, weeks[0], 0.95) + _write_week(tmp3, 7, weeks[1], 0.95, done=2) + _write_week(tmp3, 7, weeks[2], 0.95)
    assert p3.maybe_raise_band(7, pl, TODAY, [5.8, 6.5])[1] is None  # uma semana com menos de 3 mapas feitos não conta
    tmp4 = tmp_path / "d"
    tmp4.mkdir()
    p4 = wk.WeeklyPlanner(FakeRec(), tmp4)
    pl = _write_week(tmp4, 7, weeks[0], 0.95, band=(5.5, 6.2)) + _write_week(tmp4, 7, weeks[1], 0.95) + _write_week(tmp4, 7, weeks[2], 0.95)
    assert p4.maybe_raise_band(7, pl, TODAY, [5.8, 6.5])[1] is None  # a faixa mudou a meio: recomeça a contagem
