"""Colunas derivadas, calculadas no export (a BD normalizada não muda).

- is_legacy      : score vindo do osu!stable (tem legacy_score_id). Stable e
                   lazer avaliam objetos de forma diferente; accuracy não é
                   diretamente comparável entre regimes.
- mods_effective : mods sem `CL` (Classic). Nos scores stable o CL é
                   acrescentado na conversão, não é escolha do jogador.
- progress       : fração do mapa avaliada = objetos julgados / objetos totais.
                   1.0 num pass; num fail indica onde o jogador falhou.
- session_id     : sessões de jogo; um intervalo > SESSION_GAP entre scores
                   consecutivos inicia uma sessão nova.
- attempt_index  : n.º da tentativa no mesmo mapa dentro da sessão (1, 2, ...).
                   Retries do mesmo mapa não são exemplos independentes: o split
                   treino/validação/teste deve ser feito por sessão.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

SESSION_GAP = timedelta(minutes=30)

# Resultados de julgamento "principais" (um por objeto). Exclui ticks, slider
# tails, bónus, ignore_* e legacy_combo_increase, que não contam objetos.
# great/ok/meh/miss: osu!, taiko, catch (parcial); perfect/good: mania.
_OBJECT_RESULTS = ("perfect", "great", "good", "ok", "meh", "miss")


def _load(v: Any) -> dict[str, int]:
    if v is None:
        return {}
    if isinstance(v, str):
        v = json.loads(v)
    return v if isinstance(v, dict) else {}


def progress(statistics: Any, maximum_statistics: Any, passed: bool | None) -> float | None:
    if passed:
        return 1.0
    stats, maxs = _load(statistics), _load(maximum_statistics)
    total = sum(int(maxs.get(k, 0)) for k in _OBJECT_RESULTS)
    if total <= 0:
        return None
    judged = sum(int(stats.get(k, 0)) for k in _OBJECT_RESULTS)
    return round(min(judged / total, 1.0), 6)


def mods_effective(mod_acronyms: str | None) -> str:
    if not mod_acronyms:
        return ""
    return ",".join(m for m in mod_acronyms.split(",") if m and m != "CL")


def add_derived(rows: list[dict[str, Any]], session_gap: timedelta = SESSION_GAP) -> list[dict[str, Any]]:
    """`rows` tem de estar ordenado por ended_at. Altera e devolve as linhas."""
    session = 0
    prev_end: datetime | None = None
    attempts: dict[int, int] = {}
    for r in rows:
        r["is_legacy"] = r.get("legacy_score_id") is not None
        r["mods_effective"] = mods_effective(r.get("mod_acronyms"))
        r["progress"] = progress(r.get("statistics"), r.get("maximum_statistics"), r.get("passed"))

        ended = r.get("ended_at")
        ended_dt = datetime.fromisoformat(ended) if isinstance(ended, str) else ended
        if ended_dt is None:
            r["session_id"] = None
            r["attempt_index"] = None
            continue
        if prev_end is None or ended_dt - prev_end > session_gap:
            session += 1
            attempts = {}
        prev_end = ended_dt
        bid = r.get("beatmap_id")
        attempts[bid] = attempts.get(bid, 0) + 1
        r["session_id"] = session
        r["attempt_index"] = attempts[bid]
    return rows
