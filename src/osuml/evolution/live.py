"""Calibração em tempo quase real: assim que há jogadas novas (ou feedback) do jogador na BD, liga as recomendações jogadas,
avalia as previsões e atualiza a correção por jogador, sem esperar pelo `poll`. Tudo local (0 pedidos à API)."""

from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import func, select

from ..storage import models as m


class LiveCalibrator:
    def __init__(self, store: Any, recommender: Any, user_id: int, interval_s: float = 45.0, on_update=None) -> None:
        self.store, self.rec, self.uid, self.interval_s, self.on_update = store, recommender, user_id, interval_s, on_update
        self._sig: tuple | None = None
        self._lock = threading.Lock()
        self.last: dict[str, Any] = {}
        self._thread: threading.Thread | None = None

    def signature(self) -> tuple:
        with self.store.engine.connect() as c:
            s = c.execute(select(func.count(), func.max(m.scores.c.score_id)).where(m.scores.c.user_id == self.uid)).one()
            f = c.execute(select(func.count()).select_from(m.recommendation_feedback).where(m.recommendation_feedback.c.user_id == self.uid)).scalar()
        return (int(s[0]), s[1], int(f))

    def run_once(self, force: bool = False) -> dict[str, Any] | None:
        """Corre a avaliação se a BD mudou desde a última vez (ou `force`). A primeira chamada só memoriza o estado."""
        with self._lock:
            sig = self.signature()
            first = self._sig is None
            if sig == self._sig and not force:
                return None
            self._sig = sig
            if first and not force:
                return None
            ok, why = self.rec.ready()
            if not ok:
                self.last = {"skipped": why}
                return self.last
            from ..recommend.adjust import refresh_adjustments
            from ..recommend.log import evaluate_pending

            out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "eval": evaluate_pending(self.store, self.rec),
                   "adjust": refresh_adjustments(self.store, self.rec.models_dir)}
            self.last = out
        if self.on_update:
            try:
                self.on_update(out)
            except Exception:  # noqa: BLE001
                pass
        return out

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 - nunca derrubar o painel
                    self.last = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                time.sleep(self.interval_s)

        self._thread = threading.Thread(target=loop, name="live-calibration", daemon=True)
        self._thread.start()
