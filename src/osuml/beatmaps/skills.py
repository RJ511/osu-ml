"""Escala **aberta** por skill (`score = 50 + 20·z`) e nota tipo A/B/C, calibradas contra a pool de
referência (`reference_pool_<version>.parquet`, ver `reference_pool.py` e `docs/skills_comunidade.md`).

Porquê aberta (decisão do utilizador, 2026-09-23): uma skill **não pode chegar a 100**. Em vez de um
percentil (que satura em 100 para o melhor mapa da pool e acaba por atribuir "100 de aim" a mapas
diferentes), ajusta-se uma log-normal a cada campo da pool (`z = (log(valor) − μ)/σ`) e
`score = 50 + 20·z`: 50 = mapa mediano da pool, ±20 por desvio-padrão, sem teto nem chão. O
percentil continua disponível como `*_pct` ("top x%", secundário e sim, satura).

Eixos (Q0.3): **Aim**, **Speed** (rosu-pp, com os mods), **Stamina** (`density` = objetos/s de
`hitfeatures.py`, nomod) e **Reading** — port do Reading oficial do osu!lazer (`reading.py`), calculado
**com os mods**, por isso Reading, Aim e Speed sobem/descem juntos com DT/HR/HD como no jogo.
`reading_visual` e `tech_entropy` (hitfeatures.py) deixam de entrar no Reading (o Tech não é leitura).

A nota (`grade`) é definida por z (equivale aos antigos cortes de percentil sob a log-normal); é uma
convenção do projeto, não uma escala oficial da comunidade.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

DIFFICULTY_FIELDS = ("stars", "aim", "speed", "reading")
HITFEATURE_FIELDS = ("density",)
ALL_REFERENCE_FIELDS = DIFFICULTY_FIELDS + HITFEATURE_FIELDS

SCORE_MID, SCORE_SPREAD = 50.0, 20.0

_GRADE_BINS = (
    (111.8, "X"), (96.6, "SSS"), (87.6, "SS"), (81.0, "S+"), (75.6, "S"),
    (66.8, "A"), (60.5, "B+"), (55.0, "B"), (50.0, "B-"),
    (45.0, "C+"), (39.5, "C"), (33.2, "D+"), (24.4, "D"),
)


def grade(score: float) -> str:
    for floor, letter in _GRADE_BINS:
        if score >= floor:
            return letter
    return "F"


class ReferenceScale:
    """Ajuste log-normal (+ distribuição ordenada para o percentil) de cada campo da pool."""

    def __init__(self, sorted_values_by_field: dict[str, list[float]]):
        self._sorted = sorted_values_by_field
        self._fit: dict[str, tuple[float, float, float]] = {}
        for f, vals in sorted_values_by_field.items():
            positives = [v for v in vals if v > 0]
            if len(positives) < 2:
                continue
            floor = positives[int(0.01 * (len(positives) - 1))]
            logs = [math.log(max(v, floor)) for v in vals]
            mu = sum(logs) / len(logs)
            sigma = math.sqrt(sum((x - mu) ** 2 for x in logs) / len(logs)) or 1.0
            self._fit[f] = (mu, sigma, floor)

    @classmethod
    def from_parquet(cls, path: Path, fields: tuple[str, ...] = ALL_REFERENCE_FIELDS) -> "ReferenceScale":
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=list(fields)).to_pylist()
        sorted_values = {f: sorted(r[f] for r in table if r[f] is not None) for f in fields}
        return cls(sorted_values)

    def score(self, field: str, value: float) -> float:
        """`50 + 20·z`, aberta: sem teto nem chão."""
        if field not in self._fit:
            return SCORE_MID
        mu, sigma, floor = self._fit[field]
        return round(SCORE_MID + SCORE_SPREAD * (math.log(max(value, floor)) - mu) / sigma, 1)

    def percentile(self, field: str, value: float) -> float:
        values = self._sorted[field]
        if not values:
            return 0.0
        lo = bisect.bisect_left(values, value)
        hi = bisect.bisect_right(values, value)
        return round(100 * ((lo + hi) / 2) / len(values), 2)


class SkillScorer:
    """Notas abertas (50 = mediano) + letra dos 4 eixos (+ ★) para UM mapa, a partir dos valores brutos.

    `raw`: stars, aim, speed, reading (com os mods do score) e density (nomod, hitfeatures.py).
    """

    def __init__(self, scale: ReferenceScale) -> None:
        self.scale = scale

    def score(self, raw: dict) -> dict:
        out: dict = {}
        for f in DIFFICULTY_FIELDS:
            out.update(_scored(self.scale, f, raw.get(f)))
        if raw.get("density") is not None:
            out.update(_scored(self.scale, "density", raw["density"], prefix="stamina"))
        return out


def _scored(scale: ReferenceScale, field: str, value, prefix: str | None = None) -> dict:
    if value is None:
        return {}
    p = prefix or field
    sc = scale.score(field, value)
    return {f"{p}_score": sc, f"{p}_grade": grade(sc), f"{p}_pct": scale.percentile(field, value)}


def export_skill_scales(difficulty_path: Path, beatmaps_path: Path, reference_pool_path: Path,
                         out_dir: Path, version: str, user_id: int) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    scale = ReferenceScale.from_parquet(reference_pool_path)

    # Stamina: por mapa, nomod (hitfeatures.py) — uma vez por beatmap_id.
    stamina: dict[int, dict] = {}
    for r in pq.read_table(beatmaps_path, columns=["beatmap_id", "density"]).to_pylist():
        stamina[r["beatmap_id"]] = _scored(scale, "density", r["density"], prefix="stamina")

    # Aim/Speed/Reading/Stars: por (mapa, combinação de mods) — difficulty.py.
    out = []
    for r in pq.read_table(difficulty_path).to_pylist():
        row: dict = {"beatmap_id": r["beatmap_id"], "mods": r["mods"]}
        for f in DIFFICULTY_FIELDS:
            row.update(_scored(scale, f, r.get(f)))
        row.update(stamina.get(r["beatmap_id"], {}))
        out.append(row)

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"skills_{user_id}.parquet"
    pq.write_table(pa.Table.from_pylist(out), path)

    manifest = {
        "dataset_version": version,
        "user_id": user_id,
        "file": path.name,
        "rows": len(out),
        "reference_pool": str(reference_pool_path),
        "skills_covered": ["aim", "speed", "stamina", "reading"],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Escala aberta 50 + 20*z (z = log-normal ajustada à pool nomod; sem teto), nota por z "
                 "(_GRADE_BINS) e *_pct = percentil na pool (secundário, satura). Aim/Speed/Reading "
                 "variam por combinação de mods (difficulty_<id>.parquet; Reading = port do Reading "
                 "oficial do lazer, beatmaps/reading.py). Stamina (density) é nomod. A nota é uma "
                 "convenção do projeto. Ver docs/skills_comunidade.md.",
    }
    (target / f"manifest_skills_{user_id}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
