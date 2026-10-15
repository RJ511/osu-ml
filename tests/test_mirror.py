import gzip
import hashlib
import json
from datetime import datetime

import httpx

from osuml.mirror import client as mc
from osuml.storage import models as m
from osuml.storage.database import Store


def _store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'data' / 't.db'}", tmp_path / "data" / "raw")


OSU = b"osu file format v14\n\n[General]\nAudioFilename: a.mp3\n"


def _seed(store, rows):
    """rows: (user_id, beatmap_id, status, md5)"""
    now = datetime(2026, 10, 1)
    with store.engine.begin() as c:
        for i, (uid, bid, status, md5) in enumerate(rows):
            if not c.execute(m.beatmaps.select().where(m.beatmaps.c.beatmap_id == bid)).first():
                c.execute(m.beatmaps.insert().values(beatmap_id=bid, beatmapset_id=bid, status=status, checksum=md5, raw={}, fetched_at=now))
            c.execute(m.scores.insert().values(score_id=500 + i, user_id=uid, beatmap_id=bid, raw={}, content_sha256="x", first_source="best",
                                               first_seen_at=now, last_seen_at=now))


class Fake:
    def __init__(self, files=None, pressure=False, status=200):
        self.files, self.pressure, self.calls, self.status = files or {}, pressure, [], status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/guard"):
            return httpx.Response(200, json={"pressure_active": False, "pain_last_60s": 5 if self.pressure else 0, "thresholds": {"pain_60s": 5, "retry_after_s": 120}})
        if self.status != 200:
            return httpx.Response(self.status, headers={"Retry-After": "1"})
        if "/beatmaps/osu/" in req.url.path:
            bid = int(req.url.path.rsplit("/", 1)[1])
            data = self.files.get(bid)
            return httpx.Response(200 if data else 404, content=data or b"")
        if req.url.path.endswith("/performance"):
            rows = [{"pp": 9000.0 - i, "global_rank": 1000 + i} for i in range(50)]
            return httpx.Response(200, json={"data": {"cursor": {"page": 3}, "ranking": rows}, "success": True})
        if req.url.path.endswith("/strains"):
            return httpx.Response(200, json={"success": True, "length_ms": 10000, "skills": [{"name": "aim", "values": [0.0, 1.0, 4.0, 2.0]}, {"name": "speed", "values": [0.0, 3.0, 1.0, 1.0]}]})
        return httpx.Response(404)


def _client(store, fake):
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]

    return mc.MirrorClient(store, transport=httpx.MockTransport(fake), sleep=lambda s: None, clock=clock)


def test_usage_counts_per_day_and_per_purpose(tmp_path):
    day = ["2026-10-15"]
    u = mc.Usage(tmp_path / "u.json", today=lambda: day[0])
    assert u.remaining("goal") == 3
    u.add("goal")
    u.add("goal")
    assert u.used("goal") == 2 and u.remaining("goal") == 1 and u.used("osu") == 0
    day[0] = "2026-10-16"
    assert u.used("goal") == 0  # o contador é diário


def test_fill_osu_saves_only_files_whose_md5_matches_and_skips_the_rest(tmp_path):
    st = _store(tmp_path)
    good = hashlib.md5(OSU).hexdigest()
    _seed(st, [(1, 10, "ranked", good), (1, 11, "ranked", "f" * 32), (1, 12, "ranked", good), (2, 13, "graveyard", good), (1, 14, "ranked", None)])
    todo = mc.missing_osu(st, [1])
    assert [b for b, _ in todo] == [10, 11, 12]  # graveyard e sem checksum ficam de fora; o jogador 2 não é prioritário
    assert [b for b, _ in mc.missing_osu(st, [1], include_others=True)] == [10, 11, 12]  # 13 é graveyard
    fake = Fake({10: OSU, 11: OSU})  # 11: md5 diferente; 12: 404
    cl = _client(st, fake)
    out = mc.fill_osu_files(cl, st, mc.Usage(tmp_path / "data" / "control" / "usage.json"), todo)
    assert out["saved"] == 1 and out["md5_mismatch"] == 1 and out["not_found"] == 1 and out["aborted"] is None
    assert (st.raw.raw_dir / "osu_files" / f"{good}.osu").exists()
    again = mc.missing_osu(st, [1])
    assert again == []  # o guardado já não falta; os 2 problemáticos ficam 30 dias na lista de saltos
    assert "osuml/1.0" in cl.http.headers["user-agent"]


def test_fill_osu_respects_the_daily_cap_the_guard_and_consecutive_errors(tmp_path):
    st = _store(tmp_path)
    good = hashlib.md5(OSU).hexdigest()
    _seed(st, [(1, 20 + i, "loved", good) for i in range(5)])
    todo = mc.missing_osu(st, [1])
    u = mc.Usage(tmp_path / "data" / "control" / "u.json")
    u.add("osu", mc.DAILY_CAPS["osu"] - 1)  # resta 1 pedido hoje
    out = mc.fill_osu_files(_client(st, Fake({b: OSU for b, _ in todo})), st, u, todo)
    assert out["saved"] == 1 and "teto diário" in out["aborted"]
    red = Fake(pressure=True)
    cl = _client(st, red)
    out2 = mc.fill_osu_files(cl, st, mc.Usage(tmp_path / "data" / "control" / "u2.json"), mc.missing_osu(st, [1]), max_wait_guard_s=0.0)
    assert "guard vermelho" in out2["aborted"] and not any("/beatmaps/osu/" in p for p, _ in red.calls)  # nada foi pedido
    bad = Fake(status=503)
    out3 = mc.fill_osu_files(_client(st, bad), st, mc.Usage(tmp_path / "data" / "control" / "u3.json"), mc.missing_osu(st, [1]))
    assert "3 falhas seguidas" in out3["aborted"] and sum(1 for p, _ in bad.calls if "/beatmaps/osu/" in p) == 3


def test_refresh_goal_reads_the_target_row_once_a_week_and_keeps_history(tmp_path):
    goal = tmp_path / "goal.json"
    goal.write_text(json.dumps({"user_id": 1, "country": "PT", "target_country_rank": 100, "target_pp": 8300.0, "target_global_rank": 18000, "measured_at": "2026-09-01"}), encoding="utf-8")
    st = _store(tmp_path)
    fake = Fake()
    out = mc.refresh_goal(_client(st, fake), mc.Usage(tmp_path / "u.json"), goal)
    g = json.loads(goal.read_text(encoding="utf-8"))
    assert out["target_pp"] == 8951.0 and g["target_pp"] == 8951.0 and g["target_global_rank"] == 1049  # linha 50 da página 2
    req = [pr for p, pr in fake.calls if p.endswith("/performance")][0]
    assert req["country"] == "PT" and json.loads(req["cursor"]) == {"page": 2}
    assert [h["pp"] for h in g["target_history"]] == [8300.0, 8951.0]
    fake2 = Fake()
    again = mc.refresh_goal(_client(st, fake2), mc.Usage(tmp_path / "u2.json"), goal)  # medida hoje: não pede nada
    assert again["checked"] is False and not any(p.endswith("/performance") for p, _ in fake2.calls)


def test_strains_are_cached_on_disk_and_compacted(tmp_path):
    st = _store(tmp_path)
    raw = tmp_path / "strains"
    fake = Fake()
    out = mc.fetch_strains(_client(st, fake), mc.Usage(tmp_path / "u.json"), raw, [(5, ""), (6, "DT")])
    assert out["saved"] == 2 and any(pr.get("mods") == "DT" for p, pr in fake.calls if p.endswith("/strains"))
    fake2 = Fake()
    out2 = mc.fetch_strains(_client(st, fake2), mc.Usage(tmp_path / "u2.json"), raw, [(5, ""), (6, "DT")])
    assert out2["planned"] == 0 and not any(p.endswith("/strains") for p, _ in fake2.calls)  # em disco: nunca se repete
    cv = mc.compact_curve(mc.load_strains(raw, 5, ""))
    assert cv["curve"] == [0, 75, 100, 50] and cv["peak_pct"] == 62 and cv["length_s"] == 10  # máx(aim, speed) por troço, normalizado pelo pico
    assert mc.compact_curve({"unavailable": True}) is None and mc.compact_curve(None) is None
    (raw / "7_NM.json.gz").write_bytes(gzip.compress(b'{"unavailable": true}'))
    assert mc.load_strains(raw, 7, "") == {"unavailable": True}


# ------------------------------------------------------------------ disponibilidade de download
def _avail_json(disabled=False, removed=False, source="local_raw", info=None):
    return {"success": True, "availability": {"download_disabled": disabled, "more_information": info, "source": source}, "takedown": {"removed": removed}}


def test_parse_availability_is_only_negative_when_it_is_clear():
    p = mc.parse_availability
    assert p(200, _avail_json(False))["available"] is True
    assert p(200, _avail_json(True, info="DMCA"))["available"] is False and "DMCA" in p(200, _avail_json(True, info="DMCA"))["why"]
    assert p(200, _avail_json(None, source="unknown"))["available"] is None  # null não é "pode descarregar"
    assert p(200, _avail_json(False, removed=True))["available"] is False  # takedown do mirror manda
    assert p(404, {"code": "content_removed"})["available"] is False and p(404, {"code": "not_archived"})["available"] is None and p(404, None)["available"] is None


def test_availability_cache_ttls_and_check_marks_unavailable_sets(tmp_path):
    day = [__import__("datetime").date(2026, 10, 15)]
    cache = mc.AvailabilityCache(tmp_path / "a.json", today=lambda: day[0])
    st = _store(tmp_path)

    class Av:
        def __init__(self):
            self.calls = []

        def __call__(self, req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/guard"):
                return httpx.Response(200, json={"pressure_active": False, "pain_last_60s": 0, "thresholds": {"pain_60s": 5, "retry_after_s": 120}})
            sid = int(req.url.path.split("/s/")[1].split("/")[0])
            self.calls.append(sid)
            if sid == 2:
                return httpx.Response(200, json=_avail_json(True, info="removido a pedido do autor"))
            if sid == 3:
                return httpx.Response(200, json=_avail_json(None, source="unknown"))
            if sid == 4:
                return httpx.Response(404, json={"code": "content_removed"})
            return httpx.Response(200, json=_avail_json(False))

    fake = Av()
    out = mc.check_availability(_client(st, fake), mc.Usage(tmp_path / "u.json"), cache, [1, 2, 3, 4, 2, None])
    assert out["checked"] == 4 and out["unavailable"] == 2 and out["unknown"] == 1 and sorted(fake.calls) == [1, 2, 3, 4]  # duplicados e None ignorados
    assert cache.unavailable_sets() == {2, 4} and cache.get(1)["available"] is True
    assert cache.stale([1, 2, 3, 4, 5]) == [5]  # os recentes não se repetem
    day[0] = __import__("datetime").date(2026, 10, 16)
    assert cache.stale([1, 2, 3]) == [3]  # "não sei" repete-se ao fim de 1 dia
    day[0] = __import__("datetime").date(2026, 10, 22)
    assert sorted(cache.stale([1, 2, 3])) == [1, 2, 3]  # os claros vencem aos 7 dias
    again = mc.check_availability(_client(st, Av()), mc.Usage(tmp_path / "u2.json"), mc.AvailabilityCache(tmp_path / "a.json", today=lambda: day[0]), [1])
    assert again["checked"] == 1


def test_recommender_masks_out_maps_of_unavailable_sets():
    import numpy as np

    from osuml.recommend.core import Recommender

    rec = Recommender.__new__(Recommender)
    rec._index = {"ids": np.array([10, 11, 12, 13]), "labels": {10: {"set_id": 1}, 11: {"set_id": 1}, 12: {"set_id": 2}}}  # 13 sem set conhecido
    rec.availability = None
    assert not rec._unavailable_mask(4).any()  # sem verificação: nada fica de fora
    rec.availability = type("A", (), {"unavailable_sets": lambda self: {1}, "request": lambda self, ids: None})()
    assert list(rec._unavailable_mask(4)) == [True, True, False, False]  # todas as dificuldades do set indisponível
