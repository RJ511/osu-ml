"""Exportação versionada: data/processed/<version>/scores_<user_id>.<ext> + manifest.json.

Parquet se o pyarrow estiver instalado (pip install .[parquet]); senão JSONL.
O manifest regista contagens, hash do ficheiro, data e git commit, para que
cada treino possa apontar para uma versão exata do dataset.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..storage import models as m
from ..storage.database import Store

COLUMNS = [
    "score_id", "user_id", "beatmap_id", "ruleset_id", "legacy_score_id", "passed", "accuracy",
    "total_score", "legacy_total_score", "classic_total_score", "max_combo", "pp", "rank",
    "is_perfect_combo", "mod_acronyms", "mods", "statistics", "maximum_statistics",
    "started_at", "ended_at", "first_source", "revision",
]


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def _rows(store: Store, user_id: int) -> list[dict[str, Any]]:
    s, b = m.scores, m.beatmaps
    q = (
        select(*[s.c[c] for c in COLUMNS], b.c.beatmapset_id, b.c.difficulty_rating.label("nomod_star_rating"))
        .select_from(s.outerjoin(b, b.c.beatmap_id == s.c.beatmap_id))
        .where(s.c.user_id == user_id)
        .order_by(s.c.ended_at, s.c.score_id)
    )
    out = []
    with store.engine.connect() as c:
        for r in c.execute(q).mappings():
            row = dict(r)
            for k in ("mods", "statistics", "maximum_statistics"):
                row[k] = json.dumps(row[k], sort_keys=True) if row[k] is not None else None
            for k in ("started_at", "ended_at"):
                row[k] = row[k].isoformat() if row[k] else None
            out.append(row)
    return out


def export_user(store: Store, user_id: int, out_dir: Path, version: str) -> dict[str, Any]:
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    rows = _rows(store, user_id)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = target / f"scores_{user_id}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), path)
        fmt = "parquet"
    except ImportError:
        path = target / f"scores_{user_id}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fmt = "jsonl"

    manifest = {
        "dataset_version": version,
        "user_id": user_id,
        "file": path.name,
        "format": fmt,
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "report": store.user_report(user_id),
        "notes": "Uma linha por score único (dedup por score_id). ended_at em UTC. "
                 "nomod_star_rating é o SR sem mods embutido na resposta da API (NULL se ainda não houver metadata).",
    }
    (target / f"manifest_{user_id}.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
