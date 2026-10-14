from datetime import date, datetime

from sqlalchemy import insert

from osuml.evolution.live import LiveCalibrator
from osuml.evolution.notify import Notifier, check_events
from osuml.storage import models as m
from osuml.storage.database import Store

DAY = date(2026, 10, 15)  # quinta


def state(days=0, this_week=(), streak=2, ready=True, stale=False, rival_pp=None):
    sess = [{"key": "A", "title": "Consolidar", "minutes": 20, "progress": {"done": 0, "total": 3}}]
    return {"consistency": {"this_week": list(this_week), "this_week_days": days, "weekly_days_goal": 3, "streak_weeks": streak, "days_since_last": 9},
            "week": {"status": "ready" if ready else "building", "sessions": sess, "test": {"summary": {"done": 1, "total": 5}}},
            "data_quality": {"stale": stale}, "rival": {"user_id": 5, "username": "rv", "pp": rival_pp} if rival_pp else None}


def test_session_reminder_only_in_the_evening_once_per_day_and_never_when_played_today(tmp_path):
    n = Notifier(tmp_path / "n.json", ntfy_url="")
    assert check_events(n, state(), DAY, datetime(2026, 10, 15, 12, 0)) == []  # cedo demais
    assert check_events(n, state(), DAY, datetime(2026, 10, 15, 18, 0)) == ["session:2026-10-15"]
    assert check_events(n, state(), DAY, datetime(2026, 10, 15, 19, 0)) == []  # já avisado
    n2 = Notifier(tmp_path / "n2.json", ntfy_url="")
    assert check_events(n2, state(this_week=["2026-10-15"], days=1), DAY, datetime(2026, 10, 15, 18, 0)) == []  # já jogaste hoje


def test_streak_risk_rival_and_stale_events(tmp_path):
    n = Notifier(tmp_path / "n.json", ntfy_url="")
    sat = date(2026, 10, 17)  # sábado: restam 2 dias (sáb+dom), faltam 3
    keys = check_events(n, state(days=0), sat, datetime(2026, 10, 17, 10, 0))
    assert any(k.startswith("streak:") for k in keys)
    check_events(n, state(rival_pp=4000), DAY, datetime(2026, 10, 15, 10, 0))
    keys = check_events(n, state(rival_pp=4040), DAY, datetime(2026, 10, 15, 11, 0))
    assert any(k.startswith("rival:") for k in keys)
    assert any(k.startswith("stale:") for k in check_events(n, state(stale=True), DAY, datetime(2026, 10, 15, 10, 0)))


def test_push_is_off_by_default_quiet_at_night_and_never_breaks_on_failure(tmp_path):
    sent = []
    n = Notifier(tmp_path / "a.json", ntfy_url="", sender=lambda t, b: sent.append(t))
    n.add("k1", "t", "b")
    assert sent == [] and n.list()["push"] is False
    n = Notifier(tmp_path / "b.json", ntfy_url="http://x/topic", now=lambda: datetime(2026, 10, 15, 23, 30), sender=lambda t, b: sent.append(t))
    n.add("k2", "noite", "b")
    assert sent == []  # silêncio de noite
    n = Notifier(tmp_path / "c.json", ntfy_url="http://x/topic", now=lambda: datetime(2026, 10, 15, 12, 0), sender=lambda t, b: sent.append(t))
    n.add("k3", "dia", "b")
    assert sent == ["dia"] and n.list()["items"][0]["pushed"] is True
    def boom(t, b):
        raise RuntimeError("rede")
    n = Notifier(tmp_path / "d.json", ntfy_url="http://x/topic", now=lambda: datetime(2026, 10, 15, 12, 0), sender=boom)
    assert n.add("k4", "falha", "b") is True and n.list()["items"][0]["pushed"] is False
    assert n.mark_read()["unread"] == 0


class FakeRec:
    models_dir = None

    def __init__(self):
        self.ready_ok = True

    def ready(self):
        return self.ready_ok, "x"


def test_live_calibrator_runs_only_when_the_database_changed(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 't.db'}", tmp_path / "raw")
    calls = []
    import osuml.recommend.adjust as adj
    import osuml.recommend.log as lg

    monkeypatch.setattr(lg, "evaluate_pending", lambda st, rec: calls.append("eval") or {"linked": 0})
    monkeypatch.setattr(adj, "refresh_adjustments", lambda st, d: calls.append("adj") or {"players": 0})
    live = LiveCalibrator(store, FakeRec(), 7)
    assert live.run_once() is None  # a 1.ª passagem só memoriza
    assert live.run_once() is None and calls == []  # nada mudou
    with store.engine.begin() as c:
        c.execute(insert(m.recommendation_feedback).values(user_id=7, beatmap_id=1, verdict="serve", kind="teste", skills="", created_at=datetime(2026, 10, 15)))
    out = live.run_once()
    assert out is not None and calls == ["eval", "adj"] and live.run_once() is None
    assert live.run_once(force=True) is not None and calls == ["eval", "adj", "eval", "adj"]
