import gzip
import json
from datetime import datetime

import httpx
import pytest

from osuml.storage import models as m
from osuml.storage.database import Store
from osuml.tags.collect import GUARD_CHECK_EVERY, MIN_INTERVAL_S, USER_AGENT, TagCollector, summary


def _store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")


def _seed(store, rows):
    """rows: (user_id, beatmap_id, set_id, status)"""
    now = datetime(2026, 10, 1)
    with store.engine.begin() as c:
        for i, (uid, bid, sid, status) in enumerate(rows):
            if not c.execute(m.beatmaps.select().where(m.beatmaps.c.beatmap_id == bid)).first():
                c.execute(m.beatmaps.insert().values(beatmap_id=bid, beatmapset_id=sid, status=status, raw={}, fetched_at=now))
            c.execute(m.scores.insert().values(score_id=1000 + i, user_id=uid, beatmap_id=bid, raw={}, content_sha256="x", first_source="best",
                                               first_seen_at=now, last_seen_at=now))


def _set_json(sid, maps):
    return {"id": sid, "beatmaps": [{"id": b, "top_tag_ids": t} for b, t in maps]}


class Mirror:
    """Mirror falso: guard configurável + sets."""

    def __init__(self, sets, pressure=(False,)):
        self.sets, self.pressure, self.calls, self.uas = sets, list(pressure), [], []
        self._g = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        self.uas.append(req.headers["user-agent"])
        if req.url.path.endswith("/guard"):
            p = self.pressure[min(self._g, len(self.pressure) - 1)]
            self._g += 1
            return httpx.Response(200, json={"pressure_active": p, "pain_last_60s": 4 if p else 0, "thresholds": {"retry_after_s": 120},
                                             "credential": {"pressure_active": False}})
        sid = int(req.url.path.split("/s/")[1].split("/")[0])
        handler = self.sets.get(sid)
        if callable(handler):
            return handler()
        if handler is None:
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json=handler)


def _collector(store, mirror, **kw):
    sleeps = []
    t = {"now": 0.0}

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    col = TagCollector(store, transport=httpx.MockTransport(mirror), sleep=sleep, clock=lambda: t["now"], **kw)
    return col, sleeps


def test_plan_only_ranked_loved_sets_in_priority_order_and_skips_done(tmp_path):
    s = _store(tmp_path)
    _seed(s, [(1, 11, 100, "ranked"), (1, 12, 100, "ranked"), (1, 13, 101, "graveyard"), (2, 21, 200, "loved"),
              (2, 22, 100, "ranked"), (2, 23, 201, "pending")])
    col, _ = _collector(s, Mirror({}))
    assert col.plan([1, 2]) == [100, 200]  # graveyard/pending fora; o set 100 só uma vez; jogador 1 primeiro
    with s.engine.begin() as c:
        c.execute(m.tag_fetches.insert().values(beatmapset_id=100, status="ok", source="hinai", fetched_at=datetime(2026, 10, 1)))
    assert col.plan([1, 2]) == [200]  # retomável


def test_collects_tags_stores_rows_raw_and_identifies_without_personal_data(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({100: _set_json(100, [(11, [{"tag_id": 5, "count": 9}, {"tag_id": 7, "count": 2}]), (12, [])]), 200: None})
    col, sleeps = _collector(s, mirror, raw_dir=tmp_path / "rawtags")
    out = col.run([100, 200])
    assert out == {"planned": 2, "ok": 1, "not_found": 1, "tags_rows": 2, "aborted": None, "requests": 3, "guard_checks": 1}
    assert set(mirror.uas) == {USER_AGENT} and "@" not in USER_AGENT
    assert summary(s)["tag_rows"] == 2 and summary(s)["sets_not_found"] == 1
    assert json.loads(gzip.decompress((tmp_path / "rawtags" / "100.local.json.gz").read_bytes()))["id"] == 100
    assert all(sl >= 0 for sl in sleeps)
    # reexecutar o mesmo set substitui (sem duplicar)
    col.run([100])
    assert summary(s)["tag_rows"] == 2


def test_never_faster_than_one_request_per_second(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({i: _set_json(i, []) for i in range(1, 6)})
    col, sleeps = _collector(s, mirror, min_interval=0.1)  # tentar abaixo do mínimo é ignorado
    assert col.min_interval == MIN_INTERVAL_S
    col.run([1, 2, 3, 4, 5])
    assert min(sleeps) >= 0.99  # entre pedidos consecutivos esperou ~1 s cada


def test_pressure_at_start_means_no_set_request_is_made(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({1: _set_json(1, [])}, pressure=(True,))
    col, sleeps = _collector(s, mirror)
    out = col.run([1], max_wait_guard_s=300)
    assert out["aborted"] and out["ok"] == 0
    assert not any("/details" in p for p in mirror.calls)  # só consultou o guard
    assert 120 in sleeps  # esperou o retry_after publicado antes de desistir


def test_waits_for_pressure_to_clear_then_proceeds(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({1: _set_json(1, [(10, [{"tag_id": 1, "count": 1}])])}, pressure=(True, True, False))
    col, sleeps = _collector(s, mirror)
    out = col.run([1], max_wait_guard_s=900)
    assert out["aborted"] is None and out["ok"] == 1 and sleeps.count(120) == 2


def test_guard_is_rechecked_every_n_requests_and_stops_if_pressure_returns(tmp_path):
    s = _store(tmp_path)
    ids = list(range(1, GUARD_CHECK_EVERY + 5))
    mirror = Mirror({i: _set_json(i, []) for i in ids}, pressure=(False, True))
    col, _ = _collector(s, mirror)
    out = col.run(ids, max_wait_guard_s=100)  # 100 s < retry_after: não espera, pára
    assert out["aborted"] and out["ok"] == GUARD_CHECK_EVERY


def test_429_honours_retry_after_and_three_failures_in_a_row_abort(tmp_path):
    s = _store(tmp_path)
    shed = lambda: httpx.Response(429, headers={"Retry-After": "77"}, json={"code": "osu_api_relay_shed"})
    mirror = Mirror({1: shed, 2: shed, 3: shed, 4: _set_json(4, [])})
    col, sleeps = _collector(s, mirror)
    out = col.run([1, 2, 3, 4])
    assert out["aborted"] and "3 falhas seguidas" in out["aborted"] and out["ok"] == 0
    assert sleeps.count(77) == 2  # esperou o Retry-After entre falhas; à 3.ª parou
    assert not any("/s/4" in p for p in mirror.calls)


def test_a_success_resets_the_failure_counter(tmp_path):
    s = _store(tmp_path)
    shed = lambda: httpx.Response(503, headers={"Retry-After": "5"}, json={})
    mirror = Mirror({1: shed, 2: shed, 3: _set_json(3, []), 4: shed, 5: shed, 6: _set_json(6, [])})
    col, _ = _collector(s, mirror)
    assert col.run([1, 2, 3, 4, 5, 6])["aborted"] is None


def test_max_sets_caps_the_run(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({i: _set_json(i, []) for i in range(1, 11)})
    col, _ = _collector(s, mirror)
    out = col.run(list(range(1, 11)), max_sets=3)
    assert out["planned"] == 3 and out["requests"] == 4  # 1 guard + 3 sets


def test_dictionary_loads_from_saved_tags_json(tmp_path):
    s = _store(tmp_path)
    f = tmp_path / "tags.json"
    f.write_text(json.dumps({"tags": [{"id": 1, "name": "style/messy", "ruleset_id": 0, "description": "x"},
                                      {"id": 2, "name": "expression/simple", "ruleset_id": None, "description": None}]}), encoding="utf-8")
    col, _ = _collector(s, Mirror({}))
    assert col.load_dictionary(f) == 2 and summary(s)["dictionary"] == 2
    assert col.load_dictionary(tmp_path / "nao_existe.json") == 0


def test_default_endpoint_is_the_local_one_and_marks_the_source(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({1: _set_json(1, [(10, [{"tag_id": 3, "count": 4}])])})
    col, _ = _collector(s, mirror, raw_dir=tmp_path / "rawtags")
    col.run([1])
    assert "/v3/osu/beatmaps/s/1" in mirror.calls and not any("/details" in p for p in mirror.calls)
    with s.engine.connect() as c:
        assert {r["source"] for r in c.execute(m.beatmap_tags.select()).mappings()} == {"hinai_raw"}
        assert c.execute(m.tag_fetches.select()).mappings().first()["source"] == "hinai_raw"
    assert (tmp_path / "rawtags" / "1.local.json.gz").exists()


def test_details_endpoint_is_still_available_and_marked_differently(tmp_path):
    s = _store(tmp_path)
    mirror = Mirror({1: _set_json(1, [(10, [{"tag_id": 3, "count": 4}])])})
    col, _ = _collector(s, mirror, endpoint="details")
    col.run([1])
    assert "/v3/osu/beatmaps/s/1/details" in mirror.calls
    with s.engine.connect() as c:
        assert c.execute(m.tag_fetches.select()).mappings().first()["source"] == "hinai"
    with pytest.raises(ValueError):
        _collector(s, mirror, endpoint="outro")
