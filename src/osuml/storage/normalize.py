"""Normalização tolerante dos objetos da API.

Princípio: extrair colunas úteis sem nunca descartar o objeto original (vai
inteiro para a coluna `raw` e para o ficheiro raw). Campos em falta ficam NULL;
não assumimos que todos os campos existem em todas as versões da API.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def parse_dt(value: Any) -> datetime | None:
    """ISO 8601 -> datetime UTC *naive* (convenção de armazenamento do projeto)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _mod_acronyms(mods: Any) -> str | None:
    if not isinstance(mods, list):
        return None
    acr = []
    for m in mods:
        if isinstance(m, dict) and "acronym" in m:
            acr.append(str(m["acronym"]))
        elif isinstance(m, str):  # formato antigo (lista de strings)
            acr.append(m)
    return ",".join(sorted(acr))


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def effective_passed(s: dict[str, Any]) -> bool | None:
    """`passed` fiável. A API devolve `passed: false` em scores **legacy** (do stable, com `legacy_score_id`) com rank A..XH: são passes (o stable só dá
    rank S/A/B/C/D a quem completou o mapa). Medido: 3 002 scores de 31 jogadores estavam assim e entravam como falhas. Nos scores do lazer `passed` é fiável
    (falhados têm rank F sem exceção)."""
    p = s.get("passed")
    if p is False and s.get("legacy_score_id") and (s.get("rank") or "F") != "F":
        return True
    return p


def normalize_score(s: dict[str, Any]) -> dict[str, Any] | None:
    """Objeto Score (formato >= 20220705) -> colunas. None se não tiver id/user."""
    score_id = _int(s.get("id"))
    user_id = _int(s.get("user_id")) or _int((s.get("user") or {}).get("id"))
    if score_id is None or user_id is None:
        return None
    beatmap_id = _int(s.get("beatmap_id")) or _int((s.get("beatmap") or {}).get("id"))
    return {
        "score_id": score_id,
        "user_id": user_id,
        "beatmap_id": beatmap_id,
        "ruleset_id": _int(s.get("ruleset_id")),
        "legacy_score_id": _int(s.get("legacy_score_id")),
        "passed": effective_passed(s),
        "accuracy": _float(s.get("accuracy")),
        "total_score": _int(s.get("total_score")),
        "legacy_total_score": _int(s.get("legacy_total_score")),
        "classic_total_score": _int(s.get("classic_total_score")),
        "max_combo": _int(s.get("max_combo")),
        "pp": _float(s.get("pp")),
        "rank": s.get("rank"),
        "is_perfect_combo": s.get("is_perfect_combo"),
        "mods": s.get("mods"),
        "mod_acronyms": _mod_acronyms(s.get("mods")),
        "statistics": s.get("statistics"),
        "maximum_statistics": s.get("maximum_statistics"),
        "started_at": parse_dt(s.get("started_at")),
        "ended_at": parse_dt(s.get("ended_at")),
        "has_replay": s.get("has_replay"),
        "build_id": _int(s.get("build_id")),
    }


def strip_volatile(s: dict[str, Any]) -> dict[str, Any]:
    """Remove objetos embutidos que mudam independentemente do score
    (beatmap/beatmapset/user/weight) antes de calcular o hash de conteúdo.
    Assim, `revision` só sobe quando o *score* muda (ex.: recálculo de pp)."""
    return {k: v for k, v in s.items() if k not in {"beatmap", "beatmapset", "user", "weight", "current_user_attributes"}}
