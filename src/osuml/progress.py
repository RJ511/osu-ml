"""Progresso de tarefas longas em `progress.json` (lido por `scripts/progress_window.py`, que mostra a barra).

Formato: {label, status: running|done|error, done, total, unit, started_at, updated_at, computed_now}.
Escrita atómica e limitada no tempo (não escreve mais de uma vez por `min_interval`).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class Progress:
    def __init__(self, path: Path | None, label: str, total: float, unit: str = "", min_interval: float = 0.7) -> None:
        self.path, self.label, self.total, self.unit = Path(path) if path else None, label, total, unit
        self.started = time.time()
        self._last = 0.0
        self.min_interval = min_interval
        self.done = 0.0
        self._write("running")

    def _write(self, status: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps({"label": self.label, "status": status, "done": self.done, "total": self.total,
                              "unit": self.unit, "started_at": self.started, "updated_at": time.time(),
                              "computed_now": self.done, "pid": os.getpid()})
        self._last = time.time()
        # o progresso é acessório: no Windows o `replace` falha se um leitor (a janela) tiver o ficheiro aberto naquele
        # instante; tenta algumas vezes e, se não der, salta esta atualização em vez de deitar a tarefa abaixo
        for attempt in range(5):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))

    def update(self, done: float | None = None, *, add: float = 0.0, label: str | None = None, force: bool = False) -> None:
        self.done = self.done + add if done is None else done
        if label:
            self.label = label
            force = True
        if force or time.time() - self._last >= self.min_interval:
            self._write("running")

    def finish(self, status: str = "done", label: str | None = None) -> None:
        if status == "done":
            self.done = self.total
        if label:
            self.label = label
        self._write(status)
