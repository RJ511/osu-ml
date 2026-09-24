"""Testes offline: a osu!API é simulada com httpx.MockTransport."""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from osuml.api.osu import OsuClient
from osuml.api.rate_limit import MinIntervalLimiter
from osuml.collector.scores import ScoreCollector
from osuml.storage import models as m
from osuml.storage.database import Store

SECRET = "super-secret-value"
TOKEN = "tok-abc-123"
USER_ID = 4242


def iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def score(sid: int, ended: datetime, passed: bool = True, beatmap: int = 100, pp: float | None = 100.0,
          embed: bool = True) -> dict:
    s = {
        "id": sid, "user_id": USER_ID, "beatmap_id": beatmap, "ruleset_id": 0, "passed": passed,
        "accuracy": 0.97, "total_score": 900000, "max_combo": 500, "pp": pp, "rank": "A",
        "mods": [{"acronym": "HD"}, {"acronym": "DT", "settings": {"speed_change": 1.5}}],
        "statistics": {"great": 480, "ok": 15, "miss": 2}, "ended_at": iso(ended), "type": "solo_score",
    }
    if embed:
        s["beatmap"] = {"id": beatmap, "beatmapset_id": beatmap * 10, "mode": "osu", "version": "Insane",
                        "difficulty_rating": 5.1, "status": "ranked", "checksum": "x"}
        s["beatmapset"] = {"id": beatmap * 10, "artist": "A", "title": "T", "creator": "C", "status": "ranked"}
    return s


class FakeServers:
    """Simula a osu!API. Guarda a lista de pedidos recebidos."""

    def __init__(self, now: datetime):
        self.now = now
        self.calls: list[tuple[str, str, dict]] = []
        self.best = [score(i, now - timedelta(days=400 - i), beatmap=1000 + i) for i in range(1, 151)]  # 150 → 2 páginas
        self.firsts: list[dict] = []
        self.pinned = [self.best[0]]
        self.recent = [score(9001, now - timedelta(hours=2), passed=False, beatmap=5),
                       score(9002, now - timedelta(hours=1), beatmap=6),
                       self.best[-1]]  # duplicado entre fontes
        self.fail_next: list[int] = []  # status a devolver nos próximos pedidos à API osu
        self.token_requests = 0
        self.reject_token_once = False

    def osu_handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append(("osu", path, params))
        if path == "/oauth/token":
            self.token_requests += 1
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            assert body["grant_type"] == "client_credentials" and body["scope"] == "public"
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 86400, "access_token": TOKEN})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-api-version"]
        if self.reject_token_once:
            self.reject_token_once = False
            return httpx.Response(401, json={"authentication": "basic"})
        if self.fail_next:
            status = self.fail_next.pop(0)
            return httpx.Response(status, headers={"Retry-After": "3"} if status == 429 else {})
        if path == "/api/v2/users/@PXD Vieira/osu" or path == "/api/v2/users/@PXD Vieira":
            return httpx.Response(200, json={"id": USER_ID, "username": "PXD Vieira", "playmode": "osu"})
        prefix = f"/api/v2/users/{USER_ID}/scores/"
        if path.startswith(prefix):
            kind = path.removeprefix(prefix)
            limit, offset = int(params["limit"]), int(params["offset"])
            data = {"best": self.best, "firsts": self.firsts, "pinned": self.pinned, "recent": self.recent}[kind]
            if kind == "recent" and params.get("include_fails") != "1":
                data = [s for s in data if s["passed"]]
            return httpx.Response(200, json=data[offset:offset + limit])
        return httpx.Response(404, json={"error": "not found"})


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


@pytest.fixture
def env(tmp_path):
    now = datetime(2026, 9, 23, 12, 0, 0)
    fake = FakeServers(now)
    sleeps: list[float] = []
    clock = {"now": now}

    def make():
        osu = OsuClient("1", SECRET, user_agent="test", transport=httpx.MockTransport(fake.osu_handler),
                        sleep=sleeps.append, limiter=MinIntervalLimiter(1.0, clock=lambda: 0.0, sleep=sleeps.append))
        store = Store(f"sqlite:///{tmp_path}/t.db", tmp_path / "raw")
        return ScoreCollector(store, osu, now=lambda: clock["now"]), store

    return fake, make, clock, sleeps, tmp_path


def count(store, table):
    with store.engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar()


def test_first_run_collects_all_sources_without_duplicates(env):
    fake, make, clock, _, tmp = env
    collector, store = make()
    summary = collector.collect("PXD Vieira")

    assert summary["status"] == "ok", summary["errors"]
    # 150 best + 9001 + 9002 ; duplicados: best[-1] no recent, best[0] no pinned
    assert count(store, m.scores) == 152
    rep = summary["dataset"]
    assert rep["unique_scores"] == 152
    assert rep["fails"] == 1
    assert rep["scores_first_found_by_source"] == {"best": 150, "recent": 2}
    assert set(summary["sources"]) == {"best", "firsts", "pinned", "recent"}
    assert summary["sources"]["best"]["requests"] == 2  # paginação 100 + 50
    assert summary["sources"]["recent"]["duplicates_ignored"] == 1
    # mods completos preservados (incluindo settings do DT)
    with store.engine.connect() as c:
        mods = c.execute(select(m.scores.c.mods, m.scores.c.mod_acronyms).where(m.scores.c.score_id == 9001)).one()
    assert mods.mods[1]["settings"]["speed_change"] == 1.5 and mods.mod_acronyms == "DT,HD"
    # metadata de beatmap embutida guardada sem pedidos extra
    assert count(store, m.beatmaps) == 152
    # raw preservado e legível
    with store.engine.connect() as c:
        paths = [r[0] for r in c.execute(select(m.api_requests.c.raw_path)).all()]
    assert paths and all((tmp / "raw" / p).exists() for p in paths)
    body = json.loads(gzip.open(tmp / "raw" / paths[0]).read())
    assert body is not None


def test_second_run_is_incremental(env):
    fake, make, clock, _, _ = env
    collector, store = make()
    collector.collect("PXD Vieira")
    fake.calls.clear()

    clock["now"] += timedelta(hours=6)
    fake.now = clock["now"]
    fake.recent.append(score(9003, clock["now"] - timedelta(minutes=5), beatmap=8))
    collector2, _ = make()
    summary = collector2.collect("PXD Vieira")

    paths = [p for svc, p, _ in fake.calls if svc == "osu"]
    assert not any("/scores/best" in p for p in paths), "snapshot não devia repetir dentro do TTL"
    assert not any("/users/@" in p for p in paths), "utilizador devia vir da cache"
    assert summary["user_from_cache"] is True
    assert summary["sources"]["best"]["skipped"]
    assert summary["sources"]["recent"]["new"] == 1
    assert count(store, m.scores) == 153
    # só 1 pedido à API nesta execução: o recent (token + recent)
    assert [p for p in paths if p != "/oauth/token"] == [f"/api/v2/users/{USER_ID}/scores/recent"]
    assert summary["dataset"]["coverage_gaps"] == []


def test_recent_gap_when_polls_are_more_than_24h_apart(env):
    fake, make, clock, _, _ = env
    collector, store = make()
    collector.collect("PXD Vieira")
    clock["now"] += timedelta(hours=40)
    fake.now = clock["now"]
    collector2, _ = make()
    summary = collector2.collect("PXD Vieira")
    gaps = [g for g in summary["dataset"]["coverage_gaps"] if g["source"] == "recent"]
    assert len(gaps) == 1 and "24h" in gaps[0]["reason"]


def test_recent_gap_when_response_is_capped(env):
    fake, make, clock, _, _ = env
    fake.recent = [score(20000 + i, clock["now"] - timedelta(minutes=i), beatmap=9) for i in range(100)]
    collector, _ = make()
    summary = collector.collect("PXD Vieira")
    gaps = [g for g in summary["dataset"]["coverage_gaps"] if g["source"] == "recent"]
    assert gaps and "100" in gaps[0]["reason"]


def test_retries_with_backoff_on_429_and_500(env):
    fake, make, _, sleeps, _ = env
    collector, _ = make()
    fake.fail_next = [429, 500]
    summary = collector.collect("PXD Vieira")
    assert summary["status"] == "ok"
    assert summary["http"]["osu"]["retries"] == 2
    assert 3.0 in sleeps  # Retry-After respeitado


def test_persistent_error_is_isolated_per_source(env):
    fake, make, _, _, _ = env
    collector, store = make()
    collector.osu.http.max_retries = 1
    # user lookup ok, depois best falha 2x (esgota retries); as outras fontes continuam
    orig = fake.osu_handler

    def handler(req):
        if "/scores/best" in req.url.path:
            fake.calls.append(("osu", req.url.path, {}))
            return httpx.Response(503)
        return orig(req)

    collector.osu.http._client._transport = httpx.MockTransport(handler)
    summary = collector.collect("PXD Vieira")
    assert summary["status"] == "partial"
    assert summary["errors"][0]["source"] == "best"
    assert "recent" in summary["sources"] and "firsts" in summary["sources"]
    with store.engine.connect() as c:
        failed = c.execute(select(func.count()).select_from(m.api_requests).where(m.api_requests.c.error.isnot(None))).scalar()
    assert failed == 1


def test_token_refresh_on_401(env):
    fake, make, _, _, _ = env
    collector, _ = make()
    fake.reject_token_once = True
    summary = collector.collect("PXD Vieira")
    assert summary["status"] == "ok"
    assert fake.token_requests == 2


def test_secrets_never_logged(env, caplog):
    fake, make, _, _, tmp = env
    caplog.set_level(logging.DEBUG)
    collector, store = make()
    collector.collect("PXD Vieira")
    assert SECRET not in caplog.text and TOKEN not in caplog.text
    assert SECRET not in repr(collector.osu.auth) and TOKEN not in repr(collector.osu.auth)
    with store.engine.connect() as c:
        params = json.dumps([r[0] for r in c.execute(select(m.api_requests.c.params)).all()])
    assert SECRET not in params and TOKEN not in params


def test_score_revision_increments_when_pp_changes(env):
    fake, make, clock, _, _ = env
    collector, store = make()
    collector.collect("PXD Vieira")
    fake.recent[1] = score(9002, fake.now - timedelta(hours=1), beatmap=6, pp=150.0)  # recálculo de pp
    clock["now"] += timedelta(hours=1)
    collector2, _ = make()
    collector2.collect("PXD Vieira")
    with store.engine.connect() as c:
        row = c.execute(select(m.scores.c.pp, m.scores.c.revision).where(m.scores.c.score_id == 9002)).one()
    assert row.pp == 150.0 and row.revision == 2


def test_rate_limiter_enforces_min_interval():
    t = {"now": 0.0}
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t["now"] += s

    lim = MinIntervalLimiter(1.0, clock=lambda: t["now"], sleep=sleep)
    lim.wait()
    t["now"] += 0.3
    lim.wait()
    assert slept == [pytest.approx(0.7)]


def test_export_parquet(env):
    fake, make, _, _, tmp = env
    from osuml.dataset.export import export_user

    collector, store = make()
    collector.collect("PXD Vieira")
    manifest = export_user(store, USER_ID, tmp / "processed", "v0.1")
    assert manifest["rows"] == 152
    assert (tmp / "processed" / "v0.1" / manifest["file"]).exists()


def test_derived_columns():
    from osuml.dataset.derived import add_derived

    stats_fail = json.dumps({"great": 90, "ok": 5, "miss": 5, "slider_tail_hit": 40})
    maxs = json.dumps({"great": 400, "slider_tail_hit": 120, "large_tick_hit": 2})
    rows = [
        {"ended_at": "2026-09-22T19:00:00", "beatmap_id": 1, "passed": False, "legacy_score_id": None,
         "mod_acronyms": "", "statistics": stats_fail, "maximum_statistics": maxs},
        {"ended_at": "2026-09-22T19:05:00", "beatmap_id": 1, "passed": True, "legacy_score_id": None,
         "mod_acronyms": "DT,HD", "statistics": maxs, "maximum_statistics": maxs},
        {"ended_at": "2026-09-22T19:10:00", "beatmap_id": 2, "passed": True, "legacy_score_id": 55,
         "mod_acronyms": "CL,DT", "statistics": "{}", "maximum_statistics": "{}"},
        {"ended_at": "2026-09-22T21:00:00", "beatmap_id": 1, "passed": False, "legacy_score_id": None,
         "mod_acronyms": "", "statistics": stats_fail, "maximum_statistics": maxs},
    ]
    out = add_derived(rows)
    assert out[0]["progress"] == 0.25 and out[1]["progress"] == 1.0
    assert [r["session_id"] for r in out] == [1, 1, 1, 2]
    assert [r["attempt_index"] for r in out] == [1, 2, 1, 1]
    assert out[2]["is_legacy"] and not out[0]["is_legacy"]
    assert out[2]["mods_effective"] == "DT" and out[1]["mods_effective"] == "DT,HD"


def test_export_has_derived_columns(env):
    import pyarrow.parquet as pq
    from osuml.dataset.export import export_user

    fake, make, _, _, tmp = env
    collector, store = make()
    collector.collect("PXD Vieira")
    manifest = export_user(store, USER_ID, tmp / "processed", "v0.2")
    cols = pq.read_table(tmp / "processed" / "v0.2" / manifest["file"]).column_names
    for c in ("is_legacy", "mods_effective", "progress", "session_id", "attempt_index", "beatmap_status"):
        assert c in cols


def test_best_snapshot_stops_at_the_200_item_ceiling(env):
    """Com 300 scores disponíveis, `best` faz só 2 pedidos (offset 0 e 100): o 3.º seria sempre vazio."""
    fake, make, *_ = env
    fake.best = [score(i, fake.now - timedelta(days=500 - i), beatmap=2000 + i) for i in range(1, 301)]
    fake.pinned, fake.recent = [], []
    collector, store = make()
    summary = collector.collect("PXD Vieira", snapshot_types=("best",))
    best_calls = [c for c in fake.calls if c[1].endswith("/scores/best")]
    assert [int(c[2]["offset"]) for c in best_calls] == [0, 100]
    assert summary["sources"]["best"]["requests"] == 2
