"""Fila de sets por fase para a recolha de tags (`osuml tags plan` / `run`).

Fases (prioridade 1 = primeiro), por ordem de utilidade imediata:
- **B** candidatos que o recomendador propõe ao jogador (para sugerir melhor por estilo);
- **C** mapas jogados pelos jogadores acompanhados mais parecidos com o jogador (perfil de estilo comparável);
- **A** amostra aleatória do catálogo (taxas-base: o que é "normal" em cada estilo).
Só entram sets ranked/loved/approved (os que o mirror responde localmente). Nada aqui faz pedidos de rede.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from ..storage import models as m
from ..storage.database import Store, utcnow
from .collect import GOOD_STATUSES

PHASE_PRIORITY = {"B": 1, "C": 2, "A": 3}
AXES = ("aim", "speed", "stamina", "reading")


def enqueue(store: Store, set_ids: Iterable[int], phase: str, *, now: datetime | None = None) -> dict[str, int]:
    """Acrescenta à fila os sets ainda não recolhidos nem já na fila. Devolve {added, already}."""
    now = now or utcnow()
    ids = sorted({int(s) for s in set_ids if s})
    with store.engine.begin() as c:
        have = {r[0] for r in c.execute(select(m.tag_fetches.c.beatmapset_id))}
        queued = {r[0] for r in c.execute(select(m.tag_queue.c.beatmapset_id))}
        new = [s for s in ids if s not in have and s not in queued]
        if new:
            c.execute(m.tag_queue.insert(), [{"beatmapset_id": s, "phase": phase, "priority": PHASE_PRIORITY[phase], "added_at": now} for s in new])
    return {"added": len(new), "already": len(ids) - len(new)}


def next_batch(store: Store, limit: int) -> list[int]:
    """Próximos sets por fazer (fila por prioridade, depois ordem de entrada), sem os já recolhidos."""
    with store.engine.connect() as c:
        done = select(m.tag_fetches.c.beatmapset_id)
        rows = c.execute(select(m.tag_queue.c.beatmapset_id).where(m.tag_queue.c.beatmapset_id.notin_(done))
                         .order_by(m.tag_queue.c.priority, m.tag_queue.c.added_at, m.tag_queue.c.beatmapset_id).limit(limit)).all()
    return [int(r[0]) for r in rows]


def queue_counts(store: Store) -> dict[str, dict[str, int]]:
    """Por fase: total na fila, feitos e por fazer (feitos = há registo em `tag_fetches`)."""
    out: dict[str, dict[str, int]] = {}
    with store.engine.connect() as c:
        done = {r[0] for r in c.execute(select(m.tag_fetches.c.beatmapset_id))}
        for phase, sid in c.execute(select(m.tag_queue.c.phase, m.tag_queue.c.beatmapset_id)):
            d = out.setdefault(phase, {"total": 0, "done": 0, "todo": 0})
            d["total"] += 1
            d["done" if sid in done else "todo"] += 1
    return out


# ------------------------------------------------------------------ planeamento (só leitura local)
def plan_candidates(recommender: Any, user_id: int, n_per_skill: int = 250) -> list[int]:
    """Sets das sugestões do recomendador para o jogador, por cada skill (união, pela ordem de aparição)."""
    seen: dict[int, None] = {}
    for skill in AXES:
        res = recommender.recommend(user_id, [skill], n=n_per_skill)
        for it in res.get("items", []) or []:
            sid = it.get("beatmapset_id")
            if sid:
                seen.setdefault(int(sid), None)
    return list(seen)


def similar_players(store: Store, user_id: int, k: int = 20) -> list[int]:
    """Jogadores acompanhados mais parecidos (distância nos 4 eixos do perfil), com perfil fiável."""
    import json

    with store.engine.connect() as c:
        rows = c.execute(select(m.player_profiles.c.user_id, m.player_profiles.c.ratings)).all()
    prof = {}
    for uid, ratings in rows:
        r = json.loads(ratings) if isinstance(ratings, str) else (ratings or {})
        if r.get("confident") and all(r.get(f"{a}_rating") is not None for a in AXES):
            prof[int(uid)] = [r[f"{a}_rating"] for a in AXES]
    if user_id not in prof:
        return []
    me = prof[user_id]
    dist = sorted((math.dist(me, v), uid) for uid, v in prof.items() if uid != user_id)
    return [uid for _, uid in dist[:k]]


def plan_neighbors(store: Store, user_id: int, k: int = 20) -> list[int]:
    """Sets ranked/loved/approved dos mapas jogados pelos `k` jogadores mais parecidos."""
    players = similar_players(store, user_id, k)
    if not players:
        return []
    with store.engine.connect() as c:
        rows = c.execute(
            select(m.beatmaps.c.beatmapset_id).distinct()
            .select_from(m.scores.join(m.beatmaps, m.beatmaps.c.beatmap_id == m.scores.c.beatmap_id))
            .where(m.scores.c.user_id.in_(players), m.beatmaps.c.status.in_(GOOD_STATUSES), m.beatmaps.c.beatmapset_id.isnot(None))
            .order_by(m.beatmaps.c.beatmapset_id)).all()
    return [int(r[0]) for r in rows]


def plan_sample(labels_parquet: Path, n: int, seed: int = 42) -> list[int]:
    """Amostra aleatória (semente fixa) dos sets do catálogo do recomendador (ranked/loved do dump)."""
    import pyarrow.parquet as pq

    sets = sorted({int(s) for s in pq.read_table(labels_parquet, columns=["set_id"]).column("set_id").to_pylist() if s})
    random.Random(seed).shuffle(sets)
    return sets[:n]


def tags_today(store: Store, now: datetime | None = None) -> int:
    """Sets recolhidos hoje (UTC), para o teto diário."""
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with store.engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(m.tag_fetches).where(m.tag_fetches.c.fetched_at >= start)).scalar() or 0)
