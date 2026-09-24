"""Importa uma tabela **numérica** de um dump mysqldump dentro de um `.tar.bz2` (ex.: `osu_user_beatmap_playcount`)
para Parquet, em streaming, sem extrair para disco. Progresso pela posição no ficheiro comprimido (barra suave
mesmo enquanto o bz2 passa por membros grandes). Só aceita linhas de inteiros/decimais/NULL.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import Any

from ..progress import Progress
from .sqldump import columns

_ROW = re.compile(rb"\(([-\d.eE+,NUL]+)\)")


def _lines(src: Any, chunk: int = 1 << 24):
    carry = b""
    while block := src.read(chunk):
        parts = (carry + block).split(b"\n")
        carry = parts.pop()
        yield from parts
    if carry:
        yield carry


class _Counting:
    """Envolve o ficheiro comprimido e reporta a posição a cada leitura (o `tar` em streaming só devolve
    membros inteiros; sem isto a barra não mexia enquanto se passava por membros de vários GB)."""

    def __init__(self, fh: Any, progress: Progress | None, label: str) -> None:
        self.fh, self.progress, self.label = fh, progress, label

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n)
        if self.progress:
            self.progress.update(self.fh.tell(), label=self.label)
        return data


def import_numeric_table(tar_path: Path, table: str, out_path: Path, progress: Progress | None = None,
                         max_rows: int | None = None) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    size = tar_path.stat().st_size
    tag = tar_path.name.split("_osu_")[-1].split(".")[0]  # ex.: random_10000 / top_1000
    prefix = f"INSERT INTO `{table}`".encode()
    create_prefix = f"CREATE TABLE `{table}`".encode()
    cols: list[str] = []
    writer = None
    batch: list[list[float | None]] = []
    total = 0

    def flush() -> None:
        nonlocal writer, batch
        if not batch or not cols:
            return
        arrays = {c: pa.array([r[i] for r in batch]) for i, c in enumerate(cols)}
        table_ = pa.table(arrays)
        if writer is None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            writer = pq.ParquetWriter(out_path, table_.schema, compression="zstd")
        writer.write_table(table_.cast(writer.schema))
        batch = []

    with tar_path.open("rb") as fh, tarfile.open(fileobj=_Counting(fh, progress, f"A procurar {table} no dump ({tag})"),
                                                 mode="r|bz2") as tar:
        for member in tar:
            if not (member.isfile() and member.name.endswith(f"{table}.sql")):
                continue
            create: list[str] = []
            in_create = False
            for line in _lines(tar.extractfile(member)):
                if line.startswith(create_prefix):
                    in_create = True
                if in_create:
                    create.append(line.decode("utf-8", errors="replace"))
                    if line.startswith(b")"):
                        in_create = False
                        cols = columns("\n".join(create))
                elif line.startswith(prefix):
                    for m in _ROW.finditer(line):
                        batch.append([None if x == b"NULL" else (float(x) if b"." in x or b"e" in x.lower() else int(x))
                                      for x in m.group(1).split(b",")])
                        total += 1
                    if len(batch) >= 500_000:
                        flush()
                    if progress:
                        progress.update(fh.tell(), label=f"A importar {table} ({tag}) — {total:,} linhas")
                    if max_rows and total >= max_rows:
                        break
            break
    flush()
    if writer is not None:
        writer.close()
    if progress:
        progress.finish()
    return {"table": table, "rows": total, "columns": cols, "out": str(out_path), "compressed_bytes": size}
