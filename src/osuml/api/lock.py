"""Bloqueio entre processos para pedidos à osu!API.

O limite do peppy (60 pedidos/min) é por cliente, não por processo: a recolha agendada
(`osuml collect`, Task Scheduler) e o painel (`osuml panel`) correm em processos separados, e dois
processos a respeitar 1,1 s cada um somariam ~109 pedidos/min. Por isso só um de cada vez segura
este bloqueio. É um bloqueio de ficheiro do SO (msvcrt/fcntl): liberta-se sozinho se o processo morrer,
não deixa "lock velho" para trás.
"""

from __future__ import annotations

import time
from pathlib import Path

try:
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

except ImportError:  # POSIX
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class ApiLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self, timeout: float = 0.0, poll: float = 0.5, settle: float = 0.0) -> bool:
        """Tenta obter o bloqueio; espera até `timeout` segundos (0 = uma só tentativa).

        `settle`: pausa depois de obter o bloqueio, para o 1.º pedido do novo dono nunca ficar colado
        ao último pedido do dono anterior (cada processo só conhece o seu próprio limitador).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(fh):
                self._fh = fh
                if settle > 0:
                    time.sleep(settle)
                return True
            if time.monotonic() >= deadline:
                fh.close()
                return False
            time.sleep(poll)

    def release(self) -> None:
        if self._fh is not None:
            try:
                _unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "ApiLock":
        if not self.acquire():
            raise RuntimeError("outra recolha à API já está em curso")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
