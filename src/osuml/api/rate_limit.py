"""Rate limiter simples e conservador.

A política da osu!API pede no máximo 60 pedidos/minuto ("geralmente 1 por
segundo"). Em vez de um token bucket que permite rajadas, impomos um intervalo
mínimo fixo entre pedidos: é mais previsível e fica sempre abaixo do limite.

O collector é sequencial por desenho: não há concorrência contra as APIs.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class MinIntervalLimiter:
    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval <= 0:
            raise ValueError("min_interval tem de ser > 0")
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Bloqueia até ser permitido fazer o próximo pedido. Devolve o tempo esperado."""
        with self._lock:
            now = self._clock()
            waited = 0.0
            if self._last is not None:
                remaining = self.min_interval - (now - self._last)
                if remaining > 0:
                    self._sleep(remaining)
                    waited = remaining
                    now = self._clock()
            self._last = now
            return waited
