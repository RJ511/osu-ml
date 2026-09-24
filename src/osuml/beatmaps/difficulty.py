"""Atributos de dificuldade por mapa e por combinação de mods, calculados
localmente a partir dos `.osu` já importados (`osuml maps import`/`fetch`),
com `rosu-pp-py` (wheel `cp314-win_amd64` disponível — corre nativamente em
Python 3.14, sem compilar nada).

Cobre, por mapa, o nomod e cada combinação de mods (`mods_effective`, sem
`CL`) que o jogador realmente jogou — não todas as combinações possíveis.

Isto é uma **baseline local, não ground truth**: replica o algoritmo de pp
via uma biblioteca de terceiros, para deixar de depender do
`nomod_star_rating` embutido na resposta da API (que já sabemos que
subestima scores com DT, ver CLAUDE.md).

Só existem, para osu!standard, três "skills" formais: `aim`, `speed` e
`flashlight` (fica 0.0 sem o mod FL). Termos de comunidade como stream,
jump, stamina ou reading **não** são calculados aqui nem pela biblioteca —
não têm fórmula própria em std (só existem oficialmente para osu!taiko).
Essa tradução fica para uma camada de features de hit objects futura.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..dataset.derived import mods_effective
from ..storage import models as m
from ..storage.database import Store

# Colunas de `DifficultyAttributes` (osu!standard) que guardamos por linha.
_ATTR_FIELDS = (
    "stars", "aim", "speed", "flashlight", "slider_factor",
    "aim_difficult_strain_count", "speed_difficult_strain_count", "speed_note_count",
    "aim_top_weighted_slider_factor", "speed_top_weighted_slider_factor",
    "ar", "hp", "great_hit_window", "max_combo", "n_circles", "n_sliders", "n_spinners",
)


def mods_by_beatmap(store: Store, user_id: int) -> dict[int, set[str]]:
    """Mapas do jogador -> combinações de mods (`mods_effective`) jogadas, mais nomod."""
    s = m.scores
    with store.engine.connect() as c:
        rows = c.execute(
            select(s.c.beatmap_id, s.c.mod_acronyms)
            .where(s.c.user_id == user_id, s.c.beatmap_id.isnot(None))
        ).all()
    out: dict[int, set[str]] = {}
    for bid, acronyms in rows:
        out.setdefault(bid, {""}).add(mods_effective(acronyms))
    return out


def _mods_arg(mods: str) -> list[str]:
    return mods.split(",") if mods else []


def calc_difficulty(text: str, mods: str) -> dict:
    """Atributos de dificuldade de um `.osu` (texto) para uma combinação de mods."""
    import rosu_pp_py as rosu

    from .parser import parse_osu
    from .reading import reading_rating

    beatmap = rosu.Beatmap(content=text)
    attrs = rosu.Difficulty(mods=_mods_arg(mods)).calculate(beatmap)
    reading = reading_rating(parse_osu(text), _mods_arg(mods))
    return {"mods": mods, **{f: getattr(attrs, f) for f in _ATTR_FIELDS}, "reading": reading}


def export_difficulty(store: Store, user_id: int, out_dir: Path, version: str) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    combos = mods_by_beatmap(store, user_id)
    f = m.beatmap_files
    with store.engine.connect() as c:
        files = dict(c.execute(select(f.c.beatmap_id, f.c.rel_path).where(f.c.beatmap_id.in_(combos))).all())

    rows: list[dict] = []
    missing: list[int] = []
    for bid, mods_set in sorted(combos.items()):
        rel = files.get(bid)
        if rel is None:
            missing.append(bid)
            continue
        text = (store.raw.raw_dir / rel).read_bytes().decode("utf-8", errors="replace")
        for mods in sorted(mods_set):
            rows.append({"beatmap_id": bid, **calc_difficulty(text, mods)})

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"difficulty_{user_id}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)

    manifest = {
        "dataset_version": version,
        "user_id": user_id,
        "file": path.name,
        "rows": len(rows),
        "beatmaps": len(combos) - len(missing),
        "missing_beatmap_files": sorted(missing),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Calculado localmente com rosu-pp-py, por (beatmap_id, mods) realmente jogado "
                 "(mods='' = nomod) + nomod sempre incluído. Baseline, não ground truth. Só há três "
                 "'skills' formais para osu!standard: aim, speed, flashlight (flashlight fica 0.0 "
                 "sem o mod FL). `reading` é o port do Reading oficial do osu!lazer "
                 "(beatmaps/reading.py), com os mods. Outros termos de comunidade (stream, jump, ...) "
                 "não são calculados aqui.",
    }
    (target / f"manifest_difficulty_{user_id}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
