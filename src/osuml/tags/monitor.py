"""Estado monitorizável da recolha de tags: fila por fase, teto diário, estado do guard do mirror e nível de stress.

Só lê a BD local (0 pedidos), exceto `guard_now` (1 pedido ao `guard`, com intervalo mínimo entre pedidos manuais).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from ..storage import models as m
from ..storage.database import Store, utcnow
from .collect import AMBER_PAIN, HIGH_PAIN, PRESSURE_INTERVAL_S
from .queue import queue_counts, tags_today

DAILY_CAP = 1000
SLOW_MS = 1500            # latência média acima disto = sinal de carga
GUARD_COOLDOWN_S = 30.0   # entre verificações manuais do guard
GUARD_STALE_S = 6 * 3600  # um estado de guard mais antigo que isto aparece como "desatualizado"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def stress_level(guard: dict[str, Any] | None, last_run: dict[str, Any] | None, recent_throttles: int, now: datetime | None = None) -> dict[str, Any]:
    """verde / âmbar / vermelho / desconhecido, com os motivos (para mostrar ao utilizador)."""
    now = now or utcnow()
    reasons: list[str] = []
    level = "green"

    def bump(to: str, why: str) -> None:
        nonlocal level
        order = {"green": 0, "amber": 1, "red": 2}
        if order[to] > order[level]:
            level = to
        reasons.append(why)

    if guard is None:
        return {"level": "unknown", "reasons": ["ainda sem consulta ao guard"]}
    pain, limit = guard.get("pain_60s"), (guard.get("thresholds") or {}).get("pain_60s") or 5
    if pain is not None and pain >= min(HIGH_PAIN, limit):
        bump("red", f"dor do mirror {pain}/{limit} (alta): recolha parada")
    elif guard["pressure"]:
        bump("amber", f"o guard indica pressão ativa com dor baixa ({pain}/{limit}): a recolha continua a 1 pedido a cada {PRESSURE_INTERVAL_S:.0f} s")
    elif pain is not None and pain >= AMBER_PAIN:
        bump("amber", f"dor do mirror {pain}/{limit}: a recolha abranda para 1 pedido a cada 2 s")
    if (now - guard["at"]).total_seconds() > GUARD_STALE_S:
        bump("amber", "estado do guard desatualizado (>6 h)")
    if recent_throttles:
        bump("amber" if recent_throttles < 3 else "red", f"{recent_throttles} resposta(s) 429/503 nas últimas 24 h")
    if last_run:
        if last_run.get("aborted"):
            bump("amber", f"última execução parou: {last_run['aborted']}")
        if (last_run.get("avg_ms") or 0) > SLOW_MS:
            bump("amber", f"respostas lentas (média {int(last_run['avg_ms'])} ms)")
    if not reasons:
        reasons.append("guard calmo, sem erros nem respostas lentas")
    return {"level": level, "reasons": reasons}


def snapshot(store: Store, *, daily_cap: int = DAILY_CAP, paused: bool = False, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    with store.engine.connect() as c:
        g = c.execute(select(m.tag_guard_log).order_by(m.tag_guard_log.c.id.desc()).limit(1)).mappings().first()
        runs = [dict(r) for r in c.execute(select(m.tag_runs).order_by(m.tag_runs.c.run_id.desc()).limit(8)).mappings()]
        throttles = sum((r["status_counts"] or {}).get("429", 0) + (r["status_counts"] or {}).get("503", 0)
                        for r in c.execute(select(m.tag_runs.c.status_counts).where(m.tag_runs.c.started_at >= now - timedelta(hours=24))).mappings())
        fetched = c.execute(select(m.tag_fetches.c.status, m.tag_fetches.c.fetched_at)).all()
        by_source = dict(c.execute(select(m.tag_fetches.c.source, func.count()).group_by(m.tag_fetches.c.source)).all())
        n_tags = c.execute(select(m.beatmap_tags.c.beatmap_id).distinct()).all()
    guard = None
    if g:
        pain, lim = g["pain_60s"], g["pain_limit"] or 5
        guard = {"at": g["at"], "pressure": bool(g["pressure"]), "pain_60s": pain, "thresholds": {"pain_60s": g["pain_limit"], "retry_after_s": g["retry_after_s"]},
                 "light": "red" if (pain is not None and pain >= min(HIGH_PAIN, lim)) else ("amber" if g["pressure"] or (pain or 0) >= AMBER_PAIN else "green"),
                 "age_s": int((now - g["at"]).total_seconds())}
    queue = queue_counts(store)
    used = tags_today(store, now)
    last_run = runs[0] if runs else None
    todo = sum(q["todo"] for q in queue.values())
    stress = stress_level(guard, last_run, throttles, now)
    return {
        "now": _iso(now), "paused": paused, "stress": stress,
        "guard": ({**guard, "at": _iso(guard["at"])} if guard else None),
        "sets": {"done": len(fetched), "ok": sum(1 for s, _ in fetched if s == "ok"), "not_found": sum(1 for s, _ in fetched if s == "not_found"),
                 "maps_with_tags": len(n_tags), "queue_todo": todo, "by_source": by_source},
        "queue": {k: queue[k] for k in sorted(queue, key={"B": 1, "C": 2, "A": 3}.get)},
        "today": {"used": used, "cap": daily_cap, "remaining": max(daily_cap - used, 0)},
        "eta_days": (round(todo / daily_cap, 1) if todo else 0),
        "throttles_24h": throttles,
        "runs": [{"run_id": r["run_id"], "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]), "trigger": r["trigger"],
                  "planned": r["planned"], "ok": r["ok"], "not_found": r["not_found"], "requests": r["requests"], "guard_checks": r["guard_checks"],
                  "avg_ms": r["avg_ms"], "max_ms": r["max_ms"], "status_counts": r["status_counts"], "aborted": r["aborted"], "note": r["note"]} for r in runs],
    }


class TagsMonitor:
    """Estado + ações do separador "Tags" do painel."""

    def __init__(self, store: Store, control_dir: Path, *, daily_cap: int = DAILY_CAP, collector_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self.control_dir, self.daily_cap = store, Path(control_dir), daily_cap
        self._factory, self._clock = collector_factory, clock
        self._last_guard: float | None = None

    @property
    def pause_file(self) -> Path:
        return self.control_dir / "TAGS_PAUSE"

    def state(self) -> dict[str, Any]:
        return snapshot(self.store, daily_cap=self.daily_cap, paused=self.pause_file.exists())

    def pause(self, body: dict | None = None) -> dict:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.pause_file.write_text("pausado a pedido no painel\n", encoding="utf-8")
        return {"ok": True, "paused": True}

    def resume(self, body: dict | None = None) -> dict:
        self.pause_file.unlink(missing_ok=True)
        return {"ok": True, "paused": False}

    def guard_now(self, body: dict | None = None) -> dict:
        """1 pedido ao `guard` (grava o resultado). Recusa se houve outra verificação há menos de `GUARD_COOLDOWN_S`."""
        now = self._clock()
        if self._last_guard is not None and now - self._last_guard < GUARD_COOLDOWN_S:
            return {"error": f"espera {int(GUARD_COOLDOWN_S - (now - self._last_guard)) + 1} s antes de nova verificação"}
        if self._factory is None:
            return {"error": "verificação indisponível"}
        self._last_guard = now
        col = self._factory()
        try:
            return {"ok": True, "guard": col.guard()}
        except Exception as exc:  # rede em baixo, etc.
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            col.close()
