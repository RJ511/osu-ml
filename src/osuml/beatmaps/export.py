"""Export dos mapas parseados:

    data/processed/<versão>/beatmaps_<user_id>.parquet    1 linha por mapa (resumo)
    data/processed/<versão>/hitobjects_<user_id>.parquet  1 linha por hit object
    data/processed/<versão>/manifest_beatmaps_<user_id>.json

Os hit objects ficam "crus" (posição, tempo, tipo, dados de slider, beatLength
e SV ativos). Distâncias, ângulos, densidade, etc. são a camada seguinte e
calculam-se a partir daqui sem voltar a ler os .osu.

`beatmaps_<id>.parquet` inclui também `density`, `reading_visual` e `tech_entropy`
(`hitfeatures.py`, nomod) — proxies de Stamina/Reading/Tech (docs/skills_comunidade.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..storage import models as m
from ..storage.database import Store
from .hitfeatures import compute_from_parsed
from .parser import parse_osu


def export_beatmaps(store: Store, user_id: int, out_dir: Path, version: str) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    s, f = m.scores, m.beatmap_files
    with store.engine.connect() as c:
        files = c.execute(
            select(f.c.beatmap_id, f.c.rel_path, f.c.md5, f.c.checksum_match)
            .where(f.c.beatmap_id.in_(select(s.c.beatmap_id).where(s.c.user_id == user_id)))
            .order_by(f.c.beatmap_id)
        ).all()

    maps, objects, problems = [], [], []
    for row in files:
        text = (store.raw.raw_dir / row.rel_path).read_bytes().decode("utf-8", errors="replace")
        pb = parse_osu(text)
        summary = pb.summary()
        maps.append({"beatmap_id": row.beatmap_id, "md5": row.md5, "checksum_match": row.checksum_match,
                     "title": pb.metadata.get("Title"), "version": pb.metadata.get("Version"), **summary,
                     **compute_from_parsed(pb)})
        if pb.warnings:
            problems.append({"beatmap_id": row.beatmap_id, "warnings": pb.warnings[:5]})
        for o in pb.hit_objects:
            objects.append({
                "beatmap_id": row.beatmap_id, "index": o.index, "time": o.time, "end_time": o.end_time,
                "x": o.x, "y": o.y, "kind": o.kind, "new_combo": o.new_combo, "combo_skip": o.combo_skip,
                "hitsound": o.hitsound, "curve_type": o.curve_type,
                "curve_points": json.dumps(o.curve_points) if o.curve_points is not None else None,
                "slides": o.slides, "length": o.length, "beat_length": o.beat_length, "sv": o.sv,
            })

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    maps_path = target / f"beatmaps_{user_id}.parquet"
    objs_path = target / f"hitobjects_{user_id}.parquet"
    pq.write_table(pa.Table.from_pylist(maps), maps_path)
    pq.write_table(pa.Table.from_pylist(objects), objs_path)
    manifest = {
        "dataset_version": version,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            p.name: {"rows": n, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p, n in ((maps_path, len(maps)), (objs_path, len(objects)))
        },
        "maps_with_parse_warnings": problems,
    }
    (target / f"manifest_beatmaps_{user_id}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
