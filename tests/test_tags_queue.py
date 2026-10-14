import json
from datetime import datetime, timedelta

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from osuml.storage import models as m
from osuml.storage.database import Store, utcnow
from osuml.tags import queue as tq
from osuml.tags.collect import GUARD_CHECK_EVERY, TagCollector
from osuml.tags.monitor import GUARD_COOLDOWN_S, TagsMonitor, snapshot, stress_level


def _store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")


def _fetched(store, sid, when=None, status="ok"):
    with store.engine.begin() as c:
        c.execute(m.tag_fetches.insert().values(beatmapset_id=sid, status=status, source="hinai", fetched_at=when or utcnow()))


class Mirror:
    def __init__(self, ids, pain=0, pressure=False):
        self.ids, self.pain, self.pressure, self.calls = set(ids), pain, pressure, []

    def __call__(self, req):
        self.calls.append(req.url.path)
        if req.url.path.endswith("/guard"):
            return httpx.Response(200, json={"pressure_active": self.pressure, "pain_last_60s": self.pain,
                                             "thresholds": {"pain_60s": 5, "retry_after_s": 120}})
        sid = int(req.url.path.split("/s/")[1].split("/")[0])
        return httpx.Response(200, json={"id": sid, "beatmaps": [{"id": sid * 10, "top_tag_ids": [{"tag_id": 1, "count": 9}]}]}) \
            if sid in self.ids else httpx.Response(404, json={})


def _col(store, mirror):
    sleeps, t = [], {"now": 0.0}

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    return TagCollector(store, transport=httpx.MockTransport(mirror), sleep=sleep, clock=lambda: t["now"]), sleeps


def test_enqueue_is_idempotent_and_skips_done_and_orders_by_phase(tmp_path):
    s = _store(tmp_path)
    _fetched(s, 5)
    assert tq.enqueue(s, [5, 6, 7], "A") == {"added": 2, "already": 1}  # 5 já recolhido
    assert tq.enqueue(s, [7, 8], "B") == {"added": 1, "already": 1}  # 7 já na fila (fica na fase A)
    assert tq.next_batch(s, 10) == [8, 6, 7]  # B primeiro; depois A por ordem de entrada
    assert tq.queue_counts(s) == {"A": {"total": 2, "done": 0, "todo": 2}, "B": {"total": 1, "done": 0, "todo": 1}}
    _fetched(s, 8)
    assert tq.next_batch(s, 10) == [6, 7] and tq.queue_counts(s)["B"]["done"] == 1


def test_similar_players_and_neighbour_sets_only_good_statuses(tmp_path):
    s = _store(tmp_path)
    now = datetime(2026, 10, 1)

    def prof(uid, base, confident=True):
        r = {f"{a}_rating": base for a in tq.AXES} | {"confident": confident}
        with s.engine.begin() as c:
            c.execute(m.player_profiles.insert().values(user_id=uid, username=str(uid), scheme="x", n_scores=1, n_evidence=1,
                                                         n_missing=0, ratings=r, computed_at=now))

    prof(1, 70.0); prof(2, 71.0); prof(3, 90.0); prof(4, 70.5, confident=False)
    assert tq.similar_players(s, 1, k=2) == [2, 3]  # 4 não é fiável; 2 é o mais próximo
    with s.engine.begin() as c:
        for bid, sid, st in ((20, 200, "ranked"), (21, 201, "graveyard"), (30, 300, "loved")):
            c.execute(m.beatmaps.insert().values(beatmap_id=bid, beatmapset_id=sid, status=st, raw={}, fetched_at=now))
        for i, (uid, bid) in enumerate(((2, 20), (2, 21), (3, 30), (4, 20))):
            c.execute(m.scores.insert().values(score_id=i + 1, user_id=uid, beatmap_id=bid, raw={}, content_sha256="x", first_source="best",
                                                first_seen_at=now, last_seen_at=now))
    assert tq.plan_neighbors(s, 1, k=2) == [200, 300]  # graveyard fora


def test_sample_is_deterministic_for_a_seed(tmp_path):
    f = tmp_path / "labels.parquet"
    pq.write_table(pa.table({"set_id": list(range(1, 201)) + [0, None]}), f)
    a, b, c = tq.plan_sample(f, 20, 42), tq.plan_sample(f, 20, 42), tq.plan_sample(f, 20, 7)
    assert a == b and a != c and len(set(a)) == 20 and 0 not in a


def test_run_queue_respects_daily_cap_pause_and_empty_queue(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, range(1, 11), "B")
    mirror = Mirror(range(1, 11))
    col, _ = _col(s, mirror)
    assert col.run_queue(max_sets=100, daily_cap=1000, paused=True)["skipped"].startswith("recolha pausada")
    for sid in (101, 102, 103):  # 3 já feitos hoje
        _fetched(s, sid)
    out = col.run_queue(max_sets=100, daily_cap=3)  # teto já atingido
    assert out["skipped"].startswith("teto diário") and mirror.calls == []  # nem o guard foi consultado
    out = col.run_queue(max_sets=100, daily_cap=5)  # restam 2
    assert out["ok"] == 2 and out["planned"] == 2
    with s.engine.connect() as c:
        runs = c.execute(m.tag_runs.select().order_by(m.tag_runs.c.run_id)).mappings().all()
    assert len(runs) == 3 and runs[2]["ok"] == 2 and runs[2]["status_counts"] == {"200": 2} and runs[2]["avg_ms"] is not None
    assert tq.next_batch(s, 100) == [3, 4, 5, 6, 7, 8, 9, 10]
    empty, _ = _col(_store(tmp_path / "x"), Mirror([]))
    assert empty.run_queue()["skipped"] == "fila vazia"


def test_yesterdays_fetches_do_not_count_for_today(tmp_path):
    s = _store(tmp_path)
    for sid in range(1, 6):
        _fetched(s, sid, when=utcnow() - timedelta(days=1, hours=1))
    assert tq.tags_today(s) == 0
    _fetched(s, 6)
    assert tq.tags_today(s) == 1


def test_guard_pain_halves_the_rate_and_is_logged(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2, 3], "B")
    col, sleeps = _col(s, Mirror([1, 2, 3], pain=3))
    col.run_queue(max_sets=3)
    assert min(sleeps) >= 1.99  # dor 3/5 ⇒ ≥ 2 s entre pedidos
    with s.engine.connect() as c:
        g = c.execute(m.tag_guard_log.select()).mappings().all()
    assert g and g[0]["pain_60s"] == 3 and g[0]["pain_limit"] == 5 and not g[0]["pressure"]


def test_guard_is_checked_every_25_requests(tmp_path):
    assert GUARD_CHECK_EVERY == 25
    s = _store(tmp_path)
    ids = list(range(1, 52))
    tq.enqueue(s, ids, "A")
    col, _ = _col(s, Mirror(ids))
    out = col.run_queue(max_sets=51)
    assert out["ok"] == 51 and col.guard_checks == 3  # início + a cada 25 sets (25 e 50)


def test_stress_levels_and_reasons():
    now = datetime(2026, 10, 14, 12, 0)
    calm = {"at": now - timedelta(minutes=5), "pressure": False, "pain_60s": 0, "thresholds": {"pain_60s": 5}}
    assert stress_level(calm, None, 0, now)["level"] == "green"
    assert stress_level(None, None, 0, now)["level"] == "unknown"
    assert stress_level({**calm, "pressure": True}, None, 0, now)["level"] == "amber"  # pressão com dor baixa: continua devagar (1 pedido/3 s)
    assert stress_level({**calm, "pressure": True, "pain_60s": 4}, None, 0, now)["level"] == "red"  # dor alta: parado
    assert stress_level({**calm, "pain_60s": 3}, None, 0, now)["level"] == "amber"
    assert stress_level(calm, None, 1, now)["level"] == "amber" and stress_level(calm, None, 3, now)["level"] == "red"
    assert stress_level({**calm, "at": now - timedelta(hours=7)}, None, 0, now)["level"] == "amber"
    assert stress_level(calm, {"aborted": "falhas", "avg_ms": 10}, 0, now)["level"] == "amber"
    assert stress_level(calm, {"aborted": None, "avg_ms": 2500}, 0, now)["level"] == "amber"


def test_snapshot_and_monitor_actions(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2, 3, 4], "B")
    _fetched(s, 1)
    snap = snapshot(s, daily_cap=1000)
    assert snap["queue"]["B"] == {"total": 4, "done": 1, "todo": 3} and snap["today"] == {"used": 1, "cap": 1000, "remaining": 999}
    assert snap["stress"]["level"] == "unknown" and snap["sets"]["queue_todo"] == 3 and snap["eta_days"] == 0.0
    json.dumps(snap)  # serializável para a página

    t = {"now": 100.0}
    made = []

    def factory():
        col, _ = _col(s, Mirror([]))
        made.append(col)
        return col

    mon = TagsMonitor(s, tmp_path / "control", collector_factory=factory, clock=lambda: t["now"])
    assert mon.guard_now()["ok"] and len(made) == 1
    assert "espera" in mon.guard_now()["error"] and len(made) == 1  # intervalo mínimo entre verificações manuais
    t["now"] += GUARD_COOLDOWN_S + 1
    assert mon.guard_now()["ok"] and len(made) == 2
    assert mon.state()["paused"] is False
    mon.pause()
    assert mon.state()["paused"] is True and (tmp_path / "control" / "TAGS_PAUSE").exists()
    mon.resume()
    assert mon.state()["paused"] is False and mon.state()["guard"]["pressure"] is False


def test_session_runs_until_queue_empty_in_guard_checked_batches(tmp_path):
    s = _store(tmp_path)
    ids = list(range(1, 8))
    tq.enqueue(s, ids, "B")
    col, _ = _col(s, Mirror(ids))
    out = col.run_session(daily_cap=1000, batch=3)
    assert out["ok"] == 7 and out["skipped"] == "fila vazia" and out["aborted"] is None
    assert col.guard_checks == 3  # um guard antes de cada lote (3+3+1)
    assert tq.next_batch(s, 10) == []
    with s.engine.connect() as c:
        assert c.execute(m.tag_runs.select()).mappings().all()[-1]["ok"] == 7


def test_session_stops_at_daily_cap_pause_and_time_limit(tmp_path):
    s = _store(tmp_path)
    ids = list(range(1, 30))
    tq.enqueue(s, ids, "B")
    col, _ = _col(s, Mirror(ids))
    out = col.run_session(daily_cap=5, batch=2)
    assert out["ok"] == 5 and out["skipped"].startswith("teto diário")
    assert col.run_session(daily_cap=1000, paused_fn=lambda: True)["skipped"] == "recolha pausada (TAGS_PAUSE)"
    col2, _ = _col(s, Mirror(ids))
    assert col2.run_session(daily_cap=1000, max_minutes=0)["skipped"] == "tempo máximo da sessão"


def test_session_waits_in_red_then_continues_in_green_and_slows_in_amber(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2], "B")
    seq = iter([(True, 4), (False, 2), (False, 0)])  # vermelho (dor alta) → âmbar → verde

    class M(Mirror):
        def __call__(self, req):
            if req.url.path.endswith("/guard"):
                self.pressure, self.pain = next(seq, (False, 0))
            return super().__call__(req)

    col, sleeps = _col(s, M([1, 2]))
    out = col.run_session(daily_cap=1000, batch=1)
    assert out["ok"] == 2 and out["aborted"] is None
    assert 120 in sleeps                      # esperou em vermelho
    assert any(sl >= 1.99 for sl in sleeps)   # e abrandou em âmbar
    assert col.last_light == "green"


def test_session_skips_a_set_that_keeps_failing(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2, 3], "B")
    # o 2 devolve 500: é saltado ao fim de 2 falhas e a sessão acaba sem ficar presa
    class M2(Mirror):
        def __call__(self, req):
            if req.url.path.endswith("/s/2"):
                self.calls.append(req.url.path)
                return httpx.Response(500, json={})
            return super().__call__(req)

    m2 = M2([1, 3])
    col, _ = _col(s, m2)
    out = col.run_session(daily_cap=1000, batch=1)
    assert out["ok"] == 2 and out["skipped"] == "fila vazia" and col.failures[2] == 2
    assert sum(1 for p in m2.calls if p.endswith("/s/2")) == 2


def test_user_agent_identifies_project_and_discord_but_never_an_email():
    from osuml.tags.collect import USER_AGENT

    assert USER_AGENT.startswith("osuml/") and "discord" in USER_AGENT and "@" not in USER_AGENT


def test_pressure_with_low_pain_keeps_going_at_one_request_every_three_seconds(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2, 3], "B")
    col, sleeps = _col(s, Mirror([1, 2, 3], pain=1, pressure=True))
    out = col.run_queue(max_sets=3)
    assert out["ok"] == 3 and out["aborted"] is None and col.last_light == "amber"
    assert 120 not in sleeps and min(sleeps) >= 2.99  # não esperou 120 s: seguiu, com ≥ 3 s entre pedidos


def test_high_pain_stops_even_without_pressure_flag(tmp_path):
    s = _store(tmp_path)
    tq.enqueue(s, [1, 2], "B")
    mirror = Mirror([1, 2], pain=4, pressure=False)
    col, sleeps = _col(s, mirror)
    out = col.run_queue(max_sets=2, max_wait_guard_s=100)
    assert out["aborted"] and not any("/s/" in p for p in mirror.calls) and col.last_light == "red"
