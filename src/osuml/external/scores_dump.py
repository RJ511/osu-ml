"""Importa a tabela `scores` dos dumps oficiais de performance (`*_performance_osu_*.tar.bz2`) para
Parquet, em streaming e **sem extrair para disco** (o `scores.sql` tem ~1,3 GB).

- Só linhas do ruleset osu! (`ruleset_id == 0`); só `user_id`, nunca nomes (dados pessoais de terceiros).
- Observado numa amostra de 461 mil linhas: **todas `passed = 1`** (o dump não tem fails) e histórico
  desde 2009 — serve para accuracy dos passes e estudos de progresso, **não** para prever pass/fail.
- A PK do dump é `(id, preserve, unix_updated_at)`, por isso o mesmo `id` pode surgir mais de uma vez;
  guardam-se todas as linhas com `preserve` e `unix_updated_at` e o número de ids repetidos vai para o
  manifest (quem consumir deve ficar com o `unix_updated_at` mais recente por `id`).
- Licença do dump: só análise estatística, nada público/produção sem autorização do ppy → dados locais.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..dataset.derived import mods_effective

# (id, user_id, ruleset_id, beatmap_id, has_replay, preserve, ranked, rank, passed, accuracy, max_combo,
#  total_score, data, pp, legacy_score_id, legacy_total_score, started_at, ended_at, unix_updated_at, build_id)
_ROW = re.compile(
    r"\((\d+),(\d+),(\d+),(\d+),(\d),(\d),(\d),'(\w*)',(-?\d+),([-\d.eE+]+),(NULL|\d+),(\d+),"
    r"'((?:[^'\\]|\\.)*)',(NULL|[-\d.eE+]+),(NULL|\d+),(\d+),(NULL|'[^']*'),'([^']*)',(\d+),(NULL|\d+)\)")
_UNESCAPE = re.compile(r"\\(.)")
_MAP = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "b": "\b", "Z": "\x1a"}

SCHEMA_FIELDS = (
    "score_id", "user_id", "beatmap_id", "rank", "passed", "accuracy", "max_combo", "total_score", "pp",
    "is_legacy", "started_at", "ended_at", "unix_updated_at", "preserve", "ranked", "mods", "mods_effective",
    "speed_change", "n_great", "n_ok", "n_meh", "n_miss", "n_max_great")


def _unescape(s: str) -> str:
    return _UNESCAPE.sub(lambda m: _MAP.get(m.group(1), m.group(1)), s) if "\\" in s else s


def _dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def parse_line(line: str) -> Iterator[dict[str, Any]]:
    """Linhas (dicts) de um `INSERT INTO scores VALUES (...),(...)`, só ruleset 0."""
    for g in _ROW.findall(line):
        if g[2] != "0":
            continue
        try:
            data = json.loads(_unescape(g[12]))
        except ValueError:
            data = {}
        mods_list = data.get("mods") or []
        acronyms = ",".join(sorted(m["acronym"] for m in mods_list if "acronym" in m))
        speed = next((m.get("settings", {}).get("speed_change") for m in mods_list
                      if m.get("settings", {}).get("speed_change") is not None), None)
        st, mx = data.get("statistics") or {}, data.get("maximum_statistics") or {}
        started = g[16]
        yield {
            "score_id": int(g[0]), "user_id": int(g[1]), "beatmap_id": int(g[3]), "rank": g[7], "passed": int(g[8]) == 1,
            "accuracy": float(g[9]), "max_combo": None if g[10] == "NULL" else int(g[10]), "total_score": int(g[11]),
            "pp": None if g[13] == "NULL" else float(g[13]),
            "is_legacy": g[14] != "NULL" and started == "NULL",
            "started_at": None if started == "NULL" else _dt(started.strip("'")), "ended_at": _dt(g[17]),
            "unix_updated_at": int(g[18]), "preserve": int(g[5]), "ranked": int(g[6]),
            "mods": acronyms, "mods_effective": mods_effective(acronyms), "speed_change": speed,
            "n_great": st.get("great"), "n_ok": st.get("ok"), "n_meh": st.get("meh"), "n_miss": st.get("miss"),
            "n_max_great": mx.get("great"),
        }


def _lines(src: Any, chunk: int = 1 << 24) -> Iterator[bytes]:
    """Linhas de um ficheiro binário (o modo stream do tar não é seekable, logo sem TextIOWrapper)."""
    carry = b""
    while block := src.read(chunk):
        parts = (carry + block).split(b"\n")
        carry = parts.pop()
        yield from parts
    if carry:
        yield carry


def _insert_lines(tar_path: Path, member_suffix: str = "scores.sql", progress: Any | None = None) -> Iterator[bytes]:
    """Linhas `INSERT INTO scores` cruas (bytes) do `scores.sql` do dump, em streaming."""
    from .table_dump import _Counting

    with tar_path.open("rb") as fh, tarfile.open(fileobj=_Counting(fh, progress, "A ler scores.sql do dump"), mode="r|bz2") as tar:
        for member in tar:
            if member.isfile() and member.name.endswith(member_suffix):
                for raw in _lines(tar.extractfile(member)):
                    if raw.startswith(b"INSERT INTO `scores`"):
                        yield raw
                return


def iter_scores(tar_path: Path, member_suffix: str = "scores.sql", progress: Any | None = None) -> Iterator[dict[str, Any]]:
    for raw in _insert_lines(tar_path, member_suffix, progress):
        yield from parse_line(raw.decode("utf-8", errors="replace"))


def _pa_schema():
    import pyarrow as pa

    i64, f64, ts = pa.int64(), pa.float64(), pa.timestamp("us")
    return pa.schema([("score_id", i64), ("user_id", i64), ("beatmap_id", i64), ("rank", pa.string()), ("passed", pa.bool_()), ("accuracy", f64),
                      ("max_combo", i64), ("total_score", i64), ("pp", f64), ("is_legacy", pa.bool_()), ("started_at", ts), ("ended_at", ts),
                      ("unix_updated_at", i64), ("preserve", i64), ("ranked", i64), ("mods", pa.string()), ("mods_effective", pa.string()),
                      ("speed_change", f64), ("n_great", i64), ("n_ok", i64), ("n_meh", i64), ("n_miss", i64), ("n_max_great", i64)])


def _parse_chunk(lines: list[bytes]):
    """Corre num processo-filho: linhas SQL -> tabela Arrow com o esquema fixo (chunks todos nulos numa coluna não mudam o tipo)."""
    import pyarrow as pa

    rows = [r for raw in lines for r in parse_line(raw.decode("utf-8", errors="replace"))]
    return pa.Table.from_pylist(rows, schema=_pa_schema()) if rows else None


def _import_parallel(tar_path: Path, path: Path, workers: int, max_rows: int | None, progress: Any | None):
    """Lê o SQL num só processo (o bzip2 é sequencial) e reparte o parsing (a parte cara) por `workers` processos."""
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor

    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    writer = pq.ParquetWriter(path, _pa_schema(), compression="zstd")
    st = {"total": 0, "passed": 0}
    users: set[int] = set()
    years: dict[int, int] = {}
    ids: list = []

    def consume(t) -> None:
        if t is None:
            return
        writer.write_table(t)
        st["total"] += t.num_rows
        st["passed"] += int(pc.sum(t["passed"].cast("int64")).as_py() or 0)
        users.update(pc.unique(t["user_id"]).to_pylist())
        for v in pc.value_counts(pc.year(t["ended_at"])).to_pylist():
            if v["values"] is not None:
                years[v["values"]] = years.get(v["values"], 0) + v["counts"]
        ids.append(t["score_id"].to_numpy())

    with ProcessPoolExecutor(workers) as ex:
        pending: deque = deque()
        buf: list[bytes] = []
        size = 0
        for raw in _insert_lines(tar_path, progress=progress):
            buf.append(raw)
            size += len(raw)
            if size >= 8_000_000:  # ~8 MB de SQL por tarefa
                pending.append(ex.submit(_parse_chunk, buf))
                buf, size = [], 0
                while len(pending) > workers * 3:
                    consume(pending.popleft().result())
                if max_rows and st["total"] >= max_rows:
                    break
        if buf:
            pending.append(ex.submit(_parse_chunk, buf))
        while pending:
            consume(pending.popleft().result())
    writer.close()
    all_ids = np.concatenate(ids) if ids else np.array([], dtype=np.int64)
    dup = int(len(all_ids) - len(np.unique(all_ids)))
    return st["total"], dup, st["passed"], users, years


def import_scores(tar_path: Path, out_dir: Path, version: str, *, max_rows: int | None = None,
                  batch: int = 200_000, progress: Any | None = None, workers: int = 1) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    stem = tar_path.name.split(".")[0].replace("_performance_osu", "")
    path = target / f"dump_scores_{stem}.parquet"
    writer = None
    rows: list[dict[str, Any]] = []
    total = 0
    seen: set[int] = set()
    dup_ids = 0
    passed_n = 0
    users: set[int] = set()
    years: dict[int, int] = {}

    def flush() -> None:
        nonlocal writer, rows
        if not rows:
            return
        table = pa.Table.from_pylist(rows)
        if writer is None:
            writer = pq.ParquetWriter(path, table.schema, compression="zstd")
        writer.write_table(table.cast(writer.schema))
        rows = []

    if workers > 1:
        total, dup_ids, passed_n, users, years = _import_parallel(tar_path, path, workers, max_rows, progress)
    for r in (iter_scores(tar_path, progress=progress) if workers <= 1 else ()):
        if r["score_id"] in seen:
            dup_ids += 1
        else:
            seen.add(r["score_id"])
        passed_n += r["passed"]
        users.add(r["user_id"])
        if r["ended_at"]:
            years[r["ended_at"].year] = years.get(r["ended_at"].year, 0) + 1
        rows.append(r)
        total += 1
        if len(rows) >= batch:
            flush()
        if max_rows and total >= max_rows:
            break
    flush()
    if writer is not None:
        writer.close()

    if progress:
        progress.finish()
    manifest = {
        "dataset_version": version, "file": path.name, "source_tar": tar_path.name, "rows": total,
        "users": len(users), "passed_rows": passed_n, "failed_rows": total - passed_n, "duplicate_score_ids": dup_ids,
        "rows_by_year": dict(sorted(years.items())), "columns": list(SCHEMA_FIELDS),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Só ruleset osu! (0) e só user_id. O dump não tem fails (passed=1 em todas as linhas amostradas). "
                 "Mesmo score_id pode repetir-se (PK do dump inclui preserve/unix_updated_at): ficar com o "
                 "unix_updated_at mais recente. Licença: só análise estatística, uso privado.",
    }
    (target / f"manifest_dump_scores_{stem}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
