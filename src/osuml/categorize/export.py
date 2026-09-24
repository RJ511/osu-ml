"""Exporta as categorias (Parquet + manifest) — `data/processed/categories/<versão>/`.

`map_categories.parquet`: 1 linha por (beatmap_id, mods) com valores brutos e notas dos 4 eixos.
`player_profiles.parquet`: 1 linha por jogador (user_id, nome, pp, rank, ratings por eixo).
Nome/pp vêm do objeto público de utilizador da API: dados de terceiros, manter privado.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..storage import models as m
from ..storage.database import Store


def export_categories(store: Store, out_dir: Path, scheme: str) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with store.engine.connect() as c:
        maps = [dict(r) for r in c.execute(select(m.map_categories).where(m.map_categories.c.scheme == scheme)).mappings()]
        profiles = [dict(r) for r in c.execute(select(m.player_profiles).where(m.player_profiles.c.scheme == scheme)).mappings()]
    map_rows = [{"beatmap_id": r["beatmap_id"], "mods": r["mods"], "status": r["status"], "error": r["error"],
                 **{f"raw_{k}": v for k, v in (r["raw"] or {}).items()}, **(r["scores"] or {})} for r in maps]
    prof_rows = [{"user_id": r["user_id"], "username": r["username"], "pp": r["pp"], "global_rank": r["global_rank"],
                  "n_scores": r["n_scores"], "n_evidence": r["n_evidence"], "n_missing": r["n_missing"],
                  **(r["ratings"] or {})} for r in profiles]
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in (("map_categories.parquet", map_rows), ("player_profiles.parquet", prof_rows)):
        path = out_dir / name
        pq.write_table(pa.Table.from_pylist(rows), path)
        files[name] = {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {"scheme": scheme, "created_at": datetime.now(timezone.utc).isoformat(), "files": files,
                "notes": "Heurística v1 (sem ML): rating por eixo = P90 do eixo nas plays passadas com accuracy>=90%. "
                         "Mapas sem .osu ficam com status no_file. Contém nomes/pp de jogadores: manter privado."}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
