"""Verificação manual de um jogador (botão "Verificar agora" do painel Explorar).

Faz **no máximo 1 pedido `recent`** à osu!API (+ `best` só se o jogador nunca o teve) pelas mesmas
proteções do `poll` (pausa, teto diário de 400/24 h, bloqueio entre processos, intervalo ≥ 1,1 s do
cliente) e depois recalcula localmente o perfil do jogador. Recusada se a última verificação foi há
menos de `cooldown_s` (5 s) — o botão também fica desativado no ecrã.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from sqlalchemy import func, select

from ..categorize.core import CategorizeController
from ..scheduler.tracker import Tracker
from ..storage import models as m
from ..storage.database import utcnow
from .core import Explorer


class PlayerChecker:
    def __init__(self, explorer: Explorer, tracker: Tracker,
                 collect_factory: Callable[[], tuple[Callable[[int, bool], dict[str, Any]], Callable[[], None]]],
                 categorize_ctrl: CategorizeController | None = None) -> None:
        self.ex, self.tracker, self.collect_factory, self.cat = explorer, tracker, collect_factory, categorize_ctrl
        self._busy = threading.Lock()

    def _n_scores(self, uid: int) -> int:
        with self.ex.store.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(m.scores).where(m.scores.c.user_id == uid)).scalar())

    def force(self, user_id: int) -> dict[str, Any]:
        with self.ex.store.engine.connect() as c:
            if c.execute(select(m.users.c.user_id).where(m.users.c.user_id == user_id)).first() is None:
                return {"error": "jogador não encontrado"}
        info = self.ex.check_info(user_id)
        if info["cooldown_remaining_s"] > 0:
            return {"error": f"verificado há menos de {self.ex.cooldown_s:g} s", "check": info}
        if not self._busy.acquire(blocking=False):
            return {"error": "já há uma verificação em curso"}
        try:
            self.ex.attempts[user_id] = utcnow()  # trava o botão mesmo que o pedido falhe
            before = self._n_scores(user_id)
            collect_fn, close = self.collect_factory()
            try:
                res = self.tracker.check_now(user_id, collect_fn)
            finally:
                close()
            self.ex.attempts[user_id] = utcnow()
            if "error" in res:
                return {"error": res["error"], "check": self.ex.check_info(user_id)}
            out: dict[str, Any] = {"ok": True, "requests": res["requests"], "new_scores": self._n_scores(user_id) - before,
                                   "recategorized": False}
            if self.cat is not None:
                try:
                    out["recategorized"] = self.cat.recategorize_player(user_id) is not None
                except RuntimeError as exc:
                    out["note"] = f"recategorização adiada: {exc}"
            out["check"] = self.ex.check_info(user_id)
            return out
        finally:
            self._busy.release()
