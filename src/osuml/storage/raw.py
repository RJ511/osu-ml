"""Camada raw: cada corpo de resposta é guardado tal como chegou.

Ficheiros gzip content-addressed (sha256 do corpo):
    data/raw/{service}/{YYYY-MM-DD}/{sha256}.json.gz

Respostas idênticas não são duplicadas em disco; o registo do pedido em
`api_requests` aponta sempre para o ficheiro. Nada aqui é apagado.
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path


class RawStore:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    def save(self, service: str, body: bytes, when: datetime | None = None) -> tuple[str, str]:
        """Guarda o corpo e devolve (sha256, caminho relativo a raw_dir)."""
        when = when or datetime.now(timezone.utc)
        digest = hashlib.sha256(body).hexdigest()
        rel = Path(service) / when.strftime("%Y-%m-%d") / f"{digest}.json.gz"
        path = self.raw_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as fh:
                fh.write(body)
            tmp.replace(path)  # escrita atómica
        return digest, rel.as_posix()

    def load(self, rel_path: str) -> bytes:
        with gzip.open(self.raw_dir / rel_path, "rb") as fh:
            return fh.read()
