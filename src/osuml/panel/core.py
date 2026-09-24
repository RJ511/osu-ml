"""Núcleo do painel de recolhas (`osuml panel`): fila de jogadores, cancelamento e medição do
intervalo REAL entre pedidos.

- O painel só envia pedidos quando o utilizador carrega em "Iniciar"; corre 1 jogador de cada vez,
  sequencialmente, com um único cliente (um só limitador para todos os jogadores).
- Cancelar nunca deixa sair um pedido novo: o cliente HTTP consulta `cancel_check()` antes de cada
  pedido (e outra vez depois da espera do limitador). Um pedido já em voo termina.
- O intervalo mostrado é medido nos pedidos realmente enviados (instante de início, token OAuth
  incluído) e não o configurado: um intervalo abaixo do mínimo fica marcado como violação.
- Cada jogador tem um orçamento de pedidos (`JOB_REQUEST_BUDGET`) — paginação nunca é ilimitada.
- Bloqueio entre processos (`ApiLock`): não corre ao mesmo tempo que a recolha agendada.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from sqlalchemy import select, update

from ..api.http import Cancelled
from ..api.lock import ApiLock
from ..storage import models as m
from ..storage.database import Store, utcnow

MIN_ALLOWED_INTERVAL = 1.1  # s — limite oficial 60/min; o painel nunca deixa ir abaixo
TOLERANCE_MS = 50  # jitter de relógio aceitável ao classificar um intervalo como violação
JOB_REQUEST_BUDGET = 20  # user + best (3 páginas) + recent ≈ 5; folga para retries
EXCLUDED_USER_IDS = {13745526}  # PXD Vieira: já coberto pela recolha agendada


class Runner(Protocol):
    def run_job(self, user_id: int) -> dict[str, Any]: ...
    def close(self) -> None: ...


class RequestLog:
    """Últimos pedidos enviados, com o intervalo (início→início) para o pedido anterior."""

    def __init__(self, maxlen: int = 400) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._prev_start: float | None = None
        self.total = 0
        self.violations = 0
        self.min_gap_ms: int | None = None

    def add(self, event: dict[str, Any], min_interval: float, job: int | None) -> dict[str, Any]:
        with self._lock:
            start = float(event["start"])
            gap = None if self._prev_start is None else round((start - self._prev_start) * 1000)
            limit_ms = round(min_interval * 1000)
            violation = gap is not None and gap < limit_ms - TOLERANCE_MS
            self._prev_start = start
            self.total += 1
            if violation:
                self.violations += 1
            if gap is not None and (self.min_gap_ms is None or gap < self.min_gap_ms):
                self.min_gap_ms = gap
            row = {"n": self.total, "start": start, "duration_ms": event["duration_ms"], "status": event["status"],
                   "method": event["method"], "path": event["path"], "job": job, "gap_ms": gap,
                   "min_interval_ms": limit_ms, "violation": violation}
            self._events.append(row)
            return row

    def snapshot(self, limit: int = 80, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            events = list(self._events)
            last_start = self._prev_start
        return {
            "events": events[-limit:][::-1],
            "total": self.total,
            "violations": self.violations,
            "min_gap_ms": self.min_gap_ms,
            "last_60s": sum(1 for e in events if e["start"] >= now - 60),
            "since_last_start_ms": None if last_start is None else round((now - last_start) * 1000),
        }


class PanelController:
    def __init__(self, store: Store, panel_file: Path, make_runner: Callable[["PanelController"], Runner],
                 *, lock_path: Path, min_interval: float = MIN_ALLOWED_INTERVAL,
                 lock_settle: float = MIN_ALLOWED_INTERVAL) -> None:
        self.store = store
        self.panel_file = Path(panel_file)
        self._make_runner = make_runner
        self._lock = ApiLock(lock_path)
        self._lock_settle = lock_settle
        self.min_interval = max(min_interval, MIN_ALLOWED_INTERVAL)
        self.log = RequestLog()
        self.status = "idle"  # idle | running | cancelling
        self.current: int | None = None
        self.message = ""
        self._cancel_all = threading.Event()
        self._cancel_job: int | None = None
        self._abort_reason: str | None = None
        self._job_requests = 0
        self._thread: threading.Thread | None = None
        self._order: list[int] = []
        self.load_jobs()

    # ---------------------------------------------------------------- jobs
    def load_jobs(self) -> None:
        entries = json.loads(self.panel_file.read_text(encoding="utf-8"))
        self._order = []
        with self.store.engine.begin() as c:
            for e in entries:
                uid = int(e["user_id"])
                if uid in EXCLUDED_USER_IDS:
                    continue
                self._order.append(uid)
                if c.execute(select(m.panel_jobs.c.user_id).where(m.panel_jobs.c.user_id == uid)).first():
                    continue
                label = e.get("name") or f"{e.get('band')} · rank {e.get('rank_at_snapshot')}"
                c.execute(m.panel_jobs.insert().values(user_id=uid, label=label, band=e.get("band"),
                                                       status="queued", requests=0))

    def _set(self, uid: int, **values: Any) -> None:
        with self.store.engine.begin() as c:
            c.execute(update(m.panel_jobs).where(m.panel_jobs.c.user_id == uid).values(**values))

    def jobs(self) -> list[dict[str, Any]]:
        with self.store.engine.connect() as c:
            rows = {r["user_id"]: dict(r) for r in c.execute(select(m.panel_jobs)).mappings()}
        out = []
        for uid in self._order:
            r = rows[uid]
            for k in ("started_at", "finished_at"):
                r[k] = r[k].isoformat() if r[k] else None
            if uid == self.current:
                r["requests"] = self._job_requests
            out.append(r)
        return out

    # ------------------------------------------------------------- control
    def start(self, min_interval: float | None = None) -> str | None:
        """Devolve None se arrancou, ou o motivo pelo qual não arrancou."""
        if self._thread is not None and self._thread.is_alive():
            return "já está em curso"
        if min_interval is not None:
            if min_interval < MIN_ALLOWED_INTERVAL:
                return f"intervalo mínimo permitido: {MIN_ALLOWED_INTERVAL}s (60 pedidos/min)"
            self.min_interval = float(min_interval)
        if not self._lock.acquire(timeout=0, settle=self._lock_settle):
            return "outra recolha à API está em curso (ex.: a tarefa agendada) — tenta daqui a uns minutos"
        self._cancel_all.clear()
        self._cancel_job = None
        with self.store.engine.begin() as c:
            c.execute(update(m.panel_jobs).where(m.panel_jobs.c.status == "cancelled").values(status="queued"))
        self.message = ""
        self.status = "running"
        self._thread = threading.Thread(target=self._run, name="panel-worker", daemon=True)
        self._thread.start()
        return None

    def cancel_all(self) -> None:
        """Nenhum pedido novo sai; os jogadores em fila passam a `cancelled` (persistente)."""
        self._cancel_all.set()
        if self.status == "running":
            self.status = "cancelling"
        with self.store.engine.begin() as c:
            c.execute(update(m.panel_jobs).where(m.panel_jobs.c.status == "queued").values(status="cancelled"))

    def cancel_job(self, user_id: int) -> None:
        if user_id == self.current:
            self._cancel_job = user_id
        else:
            with self.store.engine.begin() as c:
                c.execute(update(m.panel_jobs).where(m.panel_jobs.c.user_id == user_id,
                                                     m.panel_jobs.c.status == "queued").values(status="cancelled"))

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------- hooks do HttpClient
    def _cancel_check(self) -> bool:
        if self._cancel_all.is_set():
            return True
        if self.current is not None and self._cancel_job == self.current:
            return True
        if self._job_requests >= JOB_REQUEST_BUDGET:
            self._abort_reason = f"orçamento de {JOB_REQUEST_BUDGET} pedidos por jogador esgotado"
            return True
        return False

    def _observe(self, event: dict[str, Any]) -> None:
        self.log.add(event, self.min_interval, self.current)
        if self.current is not None:
            self._job_requests += 1

    # -------------------------------------------------------------- worker
    def _run(self) -> None:
        runner: Runner | None = None
        try:
            runner = self._make_runner(self)
            with self.store.engine.connect() as c:
                queued = {r[0] for r in c.execute(select(m.panel_jobs.c.user_id)
                                                  .where(m.panel_jobs.c.status == "queued"))}
            for uid in [u for u in self._order if u in queued]:
                if self._cancel_all.is_set():
                    break
                self._run_job(runner, uid)
        except Exception as exc:  # falha a montar o cliente, etc.
            self.message = f"{type(exc).__name__}: {exc}"
        finally:
            if runner is not None:
                runner.close()
            self.current = None
            self.status = "idle"
            self._lock.release()

    def _run_job(self, runner: Runner, uid: int) -> None:
        self._job_requests = 0
        self._abort_reason = None
        self._cancel_job = None
        self.current = uid
        self._set(uid, status="running", started_at=utcnow(), finished_at=None, error=None)
        status, error, total = "done", None, None
        try:
            summary = runner.run_job(uid)
            total = (summary.get("dataset") or {}).get("unique_scores")
            if summary.get("status") == "partial":
                status, error = "done", "parcial: " + "; ".join(e.get("error", "") for e in summary.get("errors", []))[:300]
        except Cancelled:
            if self._abort_reason:
                status, error = "failed", self._abort_reason
            else:
                status, error = "cancelled", "cancelado pelo utilizador"
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        self._set(uid, status=status, error=error, scores_total=total, requests=self._job_requests,
                  finished_at=utcnow())
        self.current = None

    # --------------------------------------------------------------- estado
    def state(self) -> dict[str, Any]:
        jobs = self.jobs()
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        return {"status": self.status, "message": self.message, "min_interval": self.min_interval,
                "min_allowed": MIN_ALLOWED_INTERVAL, "budget": JOB_REQUEST_BUDGET, "current": self.current,
                "counts": counts, "total_jobs": len(jobs), "jobs": jobs, "log": self.log.snapshot(),
                "now": time.time()}


def default_runner_factory(settings: Any, store: Store) -> Callable[[PanelController], Runner]:
    """Um só `OsuClient` (um só limitador) para toda a fila; só `best` + `recent` por jogador."""
    from ..api.osu import OsuClient
    from ..collector.scores import ScoreCollector

    def factory(ctrl: PanelController) -> Runner:
        osu = OsuClient(settings.client_id, settings.client_secret, user_agent=settings.user_agent,
                        min_interval=ctrl.min_interval, base_url=settings.osu_base_url,
                        api_version=settings.api_version)
        osu.http.cancel_check = ctrl._cancel_check
        osu.http.observer = ctrl._observe
        collector = ScoreCollector(store, osu, snapshot_ttl=timedelta(hours=settings.snapshot_ttl_hours),
                                   user_ttl=timedelta(hours=settings.user_ttl_hours))

        class _Runner:
            def run_job(self, user_id: int) -> dict[str, Any]:
                return collector.collect(user_id, snapshot_types=("best",))

            def close(self) -> None:
                osu.close()

        return _Runner()

    return factory
