"""Agendador `osuml poll`: intervalos aleatórios <24h, best só uma vez, inatividade de 9 dias e
substituição por banda (sem rede, relógio controlado)."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest

from osuml.api.http import ApiError, HttpResult
from osuml.api.lock import ApiLock
from osuml.scheduler import tracker as T
from osuml.storage import models as m
from osuml.storage.database import Store

T0 = datetime(2026, 9, 24, 12, 0, 0)


class Clock:
    def __init__(self, t: datetime = T0): self.t = t
    def __call__(self) -> datetime: return self.t


class Stub:
    def __init__(self, ids): self.ids, self.asked = list(ids), []
    def pick(self, band, exclude):
        self.asked.append(band)
        for i in list(self.ids):
            if i not in exclude:
                self.ids.remove(i)
                return {"user_id": i, "band": band, "source": "stub"}
        return None


def _score(store: Store, uid: int, sid: int, ended: datetime) -> None:
    obj = {"id": sid, "user_id": uid, "beatmap_id": 1, "passed": True, "ended_at": ended.isoformat() + "Z",
           "beatmap": {"id": 1, "beatmapset_id": 1, "status": "ranked", "checksum": "x"}}
    rid = store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    store.ingest_scores([obj], "recent", rid)


def _setup(tmp_path, entries, clock=None, **kw):
    store = Store(f"sqlite:///{tmp_path}/t.db", tmp_path / "raw")
    pf = tmp_path / "panel.json"
    pf.write_text(json.dumps(entries), encoding="utf-8")
    clock = clock or Clock()
    sleeps: list[float] = []
    tr = Tracker = T.Tracker(store, panel_file=pf, now=clock, rng=random.Random(1), sleep=sleeps.append,
                             pause_file=tmp_path / "PAUSE", **kw)
    return tr, store, clock, sleeps


def _entries(n=3, band="1000-10000"):
    return [{"user_id": 100 + i, "band": band, "rank_at_snapshot": i} for i in range(n)]


def _collect_ok(calls):
    def fn(uid, need_best):
        calls.append((uid, need_best))
        return {"sources": {"recent": {"requests": 1}}}
    return fn


def test_seed_excludes_pxd_and_schedules_within_14_to_22h_of_last_poll(tmp_path):
    entries = _entries(2) + [{"user_id": 13745526, "band": "nomeado", "name": "PXD Vieira"}]
    tr, store, clock, _ = _setup(tmp_path, entries)
    last = T0 - timedelta(hours=1)
    store.set_state("user:100:osu:recent", {"last_poll_at": last.isoformat()})
    assert tr.seed_from_panel() == 2 and tr.seed_from_panel() == 0
    rows = {r["user_id"]: r for r in tr._rows()}
    assert set(rows) == {100, 101}
    gap = (rows[100]["next_poll_at"] - last).total_seconds() / 3600
    assert 14 <= gap <= 22
    assert rows[101]["next_poll_at"] == T0  # nunca consultado: já devido


def test_polls_only_due_players_oldest_first_with_random_pauses_and_reschedules_under_24h(tmp_path):
    tr, store, clock, sleeps = _setup(tmp_path, _entries(5))
    tr.seed_from_panel()
    for i, uid in enumerate(range(100, 105)):  # 100 é o mais atrasado
        tr._set(uid, next_poll_at=T0 - timedelta(hours=10 - i))
    tr._set(104, next_poll_at=T0 + timedelta(hours=3))  # ainda não devido
    calls: list = []
    out = tr.poll_once(_collect_ok(calls), max_players=3)
    assert [c[0] for c in calls] == [100, 101, 102] and out["due_total"] == 4
    assert len(sleeps) == 2 and all(20 <= s <= 90 for s in sleeps)
    for uid in (100, 101, 102):
        r = tr._rows(user_id=uid)[0]
        assert 14 <= (r["next_poll_at"] - r["last_poll_at"]).total_seconds() / 3600 <= 22


def test_best_is_requested_only_for_players_without_a_best_snapshot(tmp_path):
    tr, store, clock, _ = _setup(tmp_path, _entries(2))
    tr.seed_from_panel()
    store.set_state("user:101:osu:snapshot:best", {"last_run_at": T0.isoformat()})
    calls: list = []
    tr.poll_once(_collect_ok(calls))
    assert dict(calls) == {100: True, 101: False}


def test_inactive_after_9_days_stops_requests_and_is_replaced_in_the_same_band(tmp_path):
    tr, store, clock, _ = _setup(tmp_path, _entries(2), candidates=Stub([900, 901]))
    tr.seed_from_panel()
    for uid in (100, 101):
        tr._set(uid, tracked_since=T0 - timedelta(days=20), next_poll_at=T0 - timedelta(hours=1))
    _score(store, 100, 1, T0 - timedelta(days=10))  # inativo
    _score(store, 101, 2, T0 - timedelta(days=2))   # ativo
    calls: list = []
    out = tr.poll_once(_collect_ok(calls))
    assert [i["user_id"] for i in out["inactive"]] == [100] and out["inactive"][0]["replacement"] == 900
    old, new = tr._rows(user_id=100)[0], tr._rows(user_id=900)[0]
    assert old["status"] == "inactive" and old["next_poll_at"] is None and old["replaced_by"] == 900
    assert new["status"] == "active" and new["band"] == "1000-10000" and new["replaces"] == 100 and new["next_poll_at"] == T0
    # o inativo nunca mais é pedido
    clock.t = T0 + timedelta(days=3)
    calls.clear()
    tr._set(101, next_poll_at=clock.t)
    tr.poll_once(_collect_ok(calls))
    assert 100 not in [c[0] for c in calls] and (900, True) in calls


def test_grace_period_counts_from_when_tracking_started(tmp_path):
    tr, store, clock, _ = _setup(tmp_path, _entries(1))
    tr.seed_from_panel()
    tr._set(100, tracked_since=T0 - timedelta(days=2), next_poll_at=T0 - timedelta(hours=1))
    _score(store, 100, 1, T0 - timedelta(days=40))  # score antigo, mas só acompanhado há 2 dias
    out = tr.poll_once(_collect_ok([]))
    assert out["inactive"] == [] and tr._rows(user_id=100)[0]["status"] == "active"


def test_named_players_are_not_replaced_automatically(tmp_path):
    entries = [{"user_id": 500, "band": "nomeado", "name": "Alguem"}]
    tr, store, clock, _ = _setup(tmp_path, entries, candidates=Stub([900]))
    tr.seed_from_panel()
    tr._set(500, tracked_since=T0 - timedelta(days=30), next_poll_at=T0 - timedelta(hours=1))
    out = tr.poll_once(_collect_ok([]))
    assert out["inactive"][0]["replacement"] is None and tr._rows(user_id=900) == []
    assert "sem substituição" in tr._rows(user_id=500)[0]["note"]


def test_pause_file_and_daily_cap_and_busy_lock_send_nothing(tmp_path, monkeypatch):
    tr, store, clock, _ = _setup(tmp_path, _entries(2))
    calls: list = []
    (tmp_path / "PAUSE").write_text("x")
    assert "pausado" in tr.poll_once(_collect_ok(calls))["skipped"]
    (tmp_path / "PAUSE").unlink()

    monkeypatch.setattr(T, "DAILY_REQUEST_CAP", 2)
    for _ in range(2):
        store.record_request("t", "osu", HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1))
    assert "teto diário" in tr.poll_once(_collect_ok(calls))["skipped"]
    monkeypatch.setattr(T, "DAILY_REQUEST_CAP", 400)

    holder = ApiLock(tmp_path / "api.lock")
    assert holder.acquire()
    tr.lock, tr.lock_settle = ApiLock(tmp_path / "api.lock"), 0
    try:
        assert "bloqueio" in tr.poll_once(_collect_ok(calls))["skipped"]
    finally:
        holder.release()
    assert calls == []


def test_api_errors_retry_later_but_404_deactivates_and_replaces(tmp_path):
    tr, store, clock, _ = _setup(tmp_path, _entries(2), candidates=Stub([900]))
    tr.seed_from_panel()

    def fn(uid, need_best):
        raise ApiError("x", status=404 if uid == 101 else 500)

    out = tr.poll_once(fn)
    r100 = tr._rows(user_id=100)[0]
    assert r100["status"] == "active" and 2 <= (r100["next_poll_at"] - T0).total_seconds() / 3600 <= 4
    assert out["errors"][0]["user_id"] == 100
    assert tr._rows(user_id=101)[0]["status"] == "inactive" and tr._rows(user_id=900)[0]["replaces"] == 101


def test_dry_run_changes_nothing_and_sends_nothing(tmp_path):
    tr, store, clock, _ = _setup(tmp_path, _entries(2))
    tr.seed_from_panel()
    calls: list = []
    out = tr.poll_once(_collect_ok(calls), dry_run=True)
    assert calls == [] and len(out["would_poll"]) == 2 and out["polled"] == []
    assert all(r["last_poll_at"] is None for r in tr._rows())


def test_file_candidates_prefers_active_panel_candidates(tmp_path):
    d = tmp_path / "players"
    d.mkdir()
    (d / "panel_candidates.json").write_text(json.dumps([
        {"band": "25000-50000", "user_id": 1}, {"band": "25000-50000", "user_id": 2},
        {"band": "25000-50000", "user_id": 3}, {"band": "500000+", "user_id": 4}]), encoding="utf-8")
    (d / "panel_activity.json").write_text(json.dumps({
        "1": {"active_days_28d": 13}, "2": {"active_days_28d": 20}, "3": {"active_days_28d": 5}, "4": {"active_days_28d": 25}}),
        encoding="utf-8")
    fc = T.FileCandidates(d, tmp_path / "nada")
    assert fc.pick("25000-50000", set())["user_id"] == 2  # mais dias ativos
    assert fc.pick("25000-50000", {2})["user_id"] == 1
    assert fc.pick("25000-50000", {2, 1}) is None  # o 3 tem < 12 dias ativos e não há dumps de reserva
