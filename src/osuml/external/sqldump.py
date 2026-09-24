"""Leitura em streaming dos dumps SQL do data.ppy.sh (formato mysqldump), sem MySQL.

Fonte: `data/external/performance/*.tar.bz2` (dumps oficiais de performance; licença: só análise
estatística, nada público/produção sem autorização do ppy — ver CLAUDE.md).

Só lê `INSERT INTO ... VALUES (...),(...);` e devolve tuplos Python (int/float/str/None).
Não interpreta esquemas: as colunas vêm de `columns()`, lido do `CREATE TABLE`.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import Iterator

_INSERT = "INSERT INTO "
_COL = re.compile(r"^\s+`([^`]+)`\s", re.M)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "b": "\b", "Z": "\x1a"}


def parse_values(text: str) -> Iterator[tuple]:
    """Tuplos de um `VALUES (...),(...)` (sem o prefixo `INSERT INTO ... VALUES`)."""
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] != "(":
            if text[i] == ";":
                return
            i += 1
        i += 1  # depois de '('
        row: list = []
        while i < n:
            c = text[i]
            if c == "'":
                i += 1
                buf: list[str] = []
                while i < n:
                    c = text[i]
                    if c == "\\" and i + 1 < n:
                        buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                        i += 2
                    elif c == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            buf.append("'")
                            i += 2
                        else:
                            i += 1
                            break
                    else:
                        buf.append(c)
                        i += 1
                row.append("".join(buf))
            elif c in ",":
                i += 1
            elif c == ")":
                i += 1
                yield tuple(row)
                break
            else:
                j = i
                while j < n and text[j] not in ",)":
                    j += 1
                tok = text[i:j].strip()
                i = j
                if tok.upper() == "NULL":
                    row.append(None)
                else:
                    try:
                        row.append(int(tok))
                    except ValueError:
                        try:
                            row.append(float(tok))
                        except ValueError:
                            row.append(tok)


def columns(create_table_sql: str) -> list[str]:
    """Nomes das colunas de um `CREATE TABLE` (as linhas de KEY/PRIMARY KEY não começam por crase)."""
    return [m.group(1) for m in _COL.finditer(create_table_sql.split("(", 1)[1])]


def iter_table(path: Path) -> Iterator[dict]:
    """Linhas de um `.sql` (uma tabela) como dicts coluna→valor, em streaming."""
    cols: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        create: list[str] = []
        in_create = False
        for line in fh:
            if line.startswith("CREATE TABLE"):
                in_create = True
            if in_create:
                create.append(line)
                if line.startswith(")"):
                    in_create = False
                    cols = columns("".join(create))
            elif line.startswith(_INSERT):
                vals = line.split(" VALUES ", 1)[1]
                for row in parse_values(vals):
                    yield dict(zip(cols, row))


def extract_members(tar_path: Path, basenames: set[str], out_dir: Path) -> dict[str, Path]:
    """Extrai só os membros pedidos (por nome de ficheiro) de um `.tar.bz2`, em streaming.

    Lê o tar de ponta a ponta (bz2 não tem acesso aleatório), mas só escreve os pedidos — usar só
    para tabelas pequenas; as grandes (`scores.sql`, `osu_beatmap_difficulty_attribs.sql`) devem
    ser processadas em streaming sem passar por disco.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    with tarfile.open(tar_path, "r|bz2") as tar:
        for member in tar:
            base = member.name.rsplit("/", 1)[-1]
            if base in basenames and member.isfile():
                dest = out_dir / f"{tar_path.name.split('.')[0]}__{base}"
                src = tar.extractfile(member)  # uma só vez: em modo stream não há seek para trás
                with dest.open("wb") as fh:
                    while chunk := src.read(1 << 20):
                        fh.write(chunk)
                found[base] = dest
                if len(found) == len(basenames):
                    break
    return found
