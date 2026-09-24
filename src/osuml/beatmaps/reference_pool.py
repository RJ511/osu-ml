"""Amostra de referência do dump oficial, para calibrar as escalas 0-100 por skill
(`docs/skills_comunidade.md`, Q0.4b). **Não está ligada a nenhum jogador** — é só uma amostra de
mapas osu!standard do dump, usada para saber onde um mapa qualquer se situa (percentil) em `aim`,
`speed`, etc., algo que não dá para responder só com os 216 mapas de "PXD Vieira".

Amostragem por **reservoir sampling** (Algorithm R), numa única passagem em streaming pelo dump —
não precisa de saber o total de ficheiros antecipadamente e não tem viés de posição no ficheiro
(rejeitado nesta discussão: "primeiros N encontrados", que teria esse viés; estratificar por
dificuldade, que exigiria pedidos extra à API só para saber o SR de mapas fora do repertório do
jogador).

**Não persiste os `.osu` amostrados em disco** (evita os 3-6 GB extra que seriam precisos para o
dump inteiro): dado o mesmo ficheiro de dump e a mesma `--seed`, a amostra é reprodutível sem
guardar nada além do resultado numérico (`calc_difficulty`, nomod) em
`data/processed/<version>/reference_pool_<version>.parquet`.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from .acquire import _ID_NAME, OSU_HEADER, _iter_path
from .difficulty import calc_difficulty
from .hitfeatures import compute_from_parsed
from .parser import parse_osu

_MODE_RE = re.compile(rb"^\s*Mode\s*:\s*(-?\d+)", re.MULTILINE)


def _quick_mode(data: bytes) -> int:
    """Lê só o campo `Mode` do `[General]` via regex — mais barato do que parsear o ficheiro
    inteiro (parser.py) ou construir um `rosu_pp_py.Beatmap` só para filtrar candidatos que nem vão
    entrar na amostra. Omisso = 0 (std), mesma regra de `ParsedBeatmap.mode`."""
    m = _MODE_RE.search(data)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def reservoir_sample(path: Path, n: int, seed: int) -> list[tuple[str, bytes]]:
    """`n` ficheiros `.osu` de modo osu!standard, amostrados uniformemente ao acaso de todo o
    `path` (dump/pasta/zip), com reprodutibilidade total dada a mesma `seed`."""
    rng = random.Random(seed)
    reservoir: list[tuple[str, bytes]] = []
    seen = 0
    for name, data in _iter_path(path):
        if not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER) or _quick_mode(data) != 0:
            continue
        seen += 1
        if len(reservoir) < n:
            reservoir.append((name, data))
        else:
            j = rng.randint(0, seen - 1)
            if j < n:
                reservoir[j] = (name, data)
    return reservoir


def export_reference_pool(dump_path: Path, n: int, seed: int, out_dir: Path, version: str) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    sample = reservoir_sample(dump_path, n, seed)
    rows: list[dict] = []
    failed: list[str] = []
    for name, data in sample:
        mt = _ID_NAME.match(Path(name).name)
        beatmap_id = int(mt.group(1)) if mt else None
        text = data.decode("utf-8", errors="replace")
        try:
            attrs = calc_difficulty(text, "")
            hit_feats = compute_from_parsed(parse_osu(text))
        except Exception:
            failed.append(name)
            continue
        rows.append({
            "beatmap_id": beatmap_id, "source_name": name,
            "md5": hashlib.md5(data).hexdigest(), **attrs, **hit_feats,
        })

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"reference_pool_{version}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)

    manifest = {
        "dataset_version": version,
        "file": path.name,
        "rows": len(rows),
        "requested_sample_size": n,
        "seed": seed,
        "dump_path": str(dump_path),
        "dump_sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest() if dump_path.is_file() else None,
        "failed_to_calculate": len(failed),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Amostra de referência por reservoir sampling (Algorithm R), só osu!standard, "
                 "nomod (mods=''), não ligada a nenhum jogador. Inclui difficulty.py (stars/aim/"
                 "speed/...) e hitfeatures.py (density/reading_visual/tech_entropy). Reprodutível "
                 "a partir do mesmo dump_sha256 + seed, sem persistir os .osu amostrados. "
                 "Ver docs/skills_comunidade.md, Q0.3b e Q0.4b.",
    }
    (target / f"manifest_reference_pool_{version}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
