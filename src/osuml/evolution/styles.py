"""Classes de jogador por estilo (jumper, streamer, tech…) a partir das tags votadas dos mapas (0 pedidos à API).

Cada mapa tem um peso por estilo = quota dos votos das tags desse estilo no total de votos de estilo do mapa (mapas só com tags de
"expressão"/"meta" não têm estilo e ficam de fora). Para cada jogador:
- **Preferência**: quota média de cada estilo nos mapas que jogou, em z-score contra os restantes jogadores acompanhados (o "mundo" aqui são
  os jogadores da BD, não todos os jogadores de osu!). A classe principal é o estilo com maior z (se for claramente acima da média).
- **Desempenho**: o que o jogador faz *acima ou abaixo do esperado* nesse estilo: resíduo `accuracy real − accuracy prevista pelo modelo`
  (o modelo já desconta dificuldade e nível) centrado na média do próprio jogador, ponderado pelo peso do estilo e encolhido com poucos
  mapas. Assim "forte em Flow" quer dizer "rendes mais em Flow do que o teu nível faria prever", não "jogas mais Flow".
Limites: as tags são votos da comunidade (mapas pouco votados têm estilo incerto); o desempenho vem das avaliações-sombra (precisa de ≥ 8
mapas com peso no estilo); a preferência mede o que jogas, que também depende do que te recomendam/lês nos rankings.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select

from ..storage import models as m

# estilo → (rótulo, tag_ids). Só tags de osu!standard (ruleset 0) ou genéricas.
STYLES: dict[str, tuple[str, tuple[int, ...]]] = {
    "jumps": ("Jumper", (19, 20, 21, 22, 130, 131, 132, 138, 139, 140)),
    "streams": ("Streamer", (25, 26, 27, 29, 75, 141, 142)),
    "flow": ("Flow aim", (24,)),
    "alt": ("Alternado", (34,)),
    "tech": ("Tech", (15, 16, 17, 18, 23, 42)),
    "stamina": ("Stamina", (28, 133)),
    "reading": ("Leitura", (30, 31, 32, 66)),
    "precision": ("Precisão", (33,)),
}
CLASS_STYLES = ("jumps", "streams", "flow", "alt", "tech", "stamina")  # leitura/precisão têm poucas tags (≈ 1 % dos votos): só como informação, nunca como classe
MIN_SHARE_CLASS = 0.04    # quota mínima de um estilo para poder ser a classe do jogador
MIN_MAPS_PREF = 30        # mapas com estilo para dizer o que o jogador prefere
MIN_MAPS_PERF = 8         # peso total mínimo num estilo para falar de desempenho
SHRINK_K = 10.0
PRIMARY_Z = 0.5           # z mínimo para haver uma classe principal
STRONG_PTS = 1.0          # pontos de accuracy acima/abaixo do esperado para "forte"/"fraco"


def _tag_to_style() -> dict[int, str]:
    return {t: s for s, (_l, ids) in STYLES.items() for t in ids}


def map_styles(store: Any) -> dict[int, dict[str, float]]:
    """{beatmap_id: {estilo: peso}} com os pesos a somar 1 (só mapas com tags de estilo)."""
    t2s = _tag_to_style()
    votes: dict[int, dict[str, float]] = {}
    with store.engine.connect() as c:
        for bid, tag, v in c.execute(select(m.beatmap_tags.c.beatmap_id, m.beatmap_tags.c.tag_id, m.beatmap_tags.c.votes)):
            s = t2s.get(int(tag))
            if s:
                d = votes.setdefault(int(bid), {})
                d[s] = d.get(s, 0.0) + float(v or 0)
    out = {}
    for bid, d in votes.items():
        tot = sum(d.values())
        if tot > 0:
            out[bid] = {s: v / tot for s, v in d.items()}
    return out


def played_maps(store: Any) -> dict[int, set[int]]:
    """{user_id: mapas jogados} (passes e falhas, sem mods distintos)."""
    out: dict[int, set[int]] = {}
    with store.engine.connect() as c:
        for uid, bid in c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id).distinct()):
            out.setdefault(int(uid), set()).add(int(bid))
    return out


def residual_rows(store: Any) -> dict[int, list[tuple[int, float | None, float | None]]]:
    """{user_id: [(beatmap_id, resíduo_accuracy, resíduo_passar)]} das avaliações-sombra sem mods.
    Resíduo de accuracy = real − prevista (bruta, em fração); de passar = passou − P(passar) bruta (só tentativas do lazer)."""
    pl = m.prediction_log
    out: dict[int, list[tuple[int, float | None, float | None]]] = {}
    with store.engine.connect() as c:
        rows = c.execute(select(pl.c.user_id, pl.c.beatmap_id, pl.c.best_acc, pl.c.acc_pass_raw, pl.c.passed, pl.c.p_pass_raw, pl.c.n_lazer_attempts, pl.c.mods)
                         .where(pl.c.kind == "sombra", pl.c.evaluated_at.isnot(None)))
        for uid, bid, best, pred, passed, p, nl, mods in rows:
            if mods:
                continue
            ra = float(best) - float(pred) if best is not None and pred is not None else None
            rp = (1.0 if passed else 0.0) - float(p) if passed is not None and p is not None and (nl or 0) > 0 else None
            out.setdefault(int(uid), []).append((int(bid), ra, rp))
    return out


def shares(maps: set[int], styles: dict[int, dict[str, float]]) -> tuple[dict[str, float], int]:
    """Quota média de cada estilo nos mapas jogados que têm estilo, e quantos mapas contaram."""
    acc = {s: 0.0 for s in STYLES}
    n = 0
    for b in maps:
        w = styles.get(b)
        if w:
            n += 1
            for s, v in w.items():
                acc[s] += v
    return ({s: v / n for s, v in acc.items()} if n else {s: 0.0 for s in STYLES}), n


def performance(rows: list[tuple[int, float | None, float | None]], styles: dict[int, dict[str, float]]) -> dict[str, dict[str, Any]]:
    """Por estilo: pontos de accuracy acima(+)/abaixo(−) do esperado e diferença na taxa de passar, centrados no próprio jogador."""
    ra = [r[1] for r in rows if r[1] is not None]
    rp = [r[2] for r in rows if r[2] is not None]
    mean_a = sum(ra) / len(ra) if ra else 0.0
    mean_p = sum(rp) / len(rp) if rp else 0.0
    out: dict[str, dict[str, Any]] = {}
    for s in STYLES:
        wa = sum(styles[b][s] for b, a, _p in rows if a is not None and b in styles and s in styles[b])
        sa = sum(styles[b][s] * (a - mean_a) for b, a, _p in rows if a is not None and b in styles and s in styles[b])
        wp = sum(styles[b][s] for b, _a, p in rows if p is not None and b in styles and s in styles[b])
        sp = sum(styles[b][s] * (p - mean_p) for b, _a, p in rows if p is not None and b in styles and s in styles[b])
        out[s] = {"weight": round(wa, 1),
                  "acc_pts": round(100 * sa / (wa + SHRINK_K), 2) if wa >= MIN_MAPS_PERF else None,
                  "pass_pts": round(100 * sp / (wp + SHRINK_K), 1) if wp >= MIN_MAPS_PERF else None}
    return out


def build(store: Any, min_players: int = 8) -> dict[str, Any]:
    """Perfis de estilo de todos os jogadores com mapas suficientes: {"base", "players": {user_id: perfil}}."""
    styles = map_styles(store)
    played = played_maps(store)
    res = residual_rows(store)
    pref = {}
    for uid, mp in played.items():
        sh, n = shares(mp, styles)
        if n >= MIN_MAPS_PREF:
            pref[uid] = (sh, n)
    if len(pref) < min_players:
        return {"ok": False, "reason": f"poucos jogadores com ≥ {MIN_MAPS_PREF} mapas com tags ({len(pref)})", "players": {}}
    stats = {}
    for s in STYLES:
        v = [sh[s] for sh, _n in pref.values()]
        mu = sum(v) / len(v)
        sd = math.sqrt(sum((x - mu) ** 2 for x in v) / len(v)) or 1e-9
        stats[s] = (mu, sd)
    players = {}
    for uid, (sh, n) in pref.items():
        z = {s: (sh[s] - stats[s][0]) / stats[s][1] for s in STYLES}
        perf = performance(res.get(uid, []), styles)
        order = sorted((s for s in CLASS_STYLES if sh[s] >= MIN_SHARE_CLASS), key=lambda s: -z[s])
        primary = order[0] if order and z[order[0]] >= PRIMARY_Z else None
        secondary = order[1] if primary and len(order) > 1 and z[order[1]] >= PRIMARY_Z else None
        known = [(s, p["acc_pts"]) for s, p in perf.items() if p["acc_pts"] is not None]
        strong = max(known, key=lambda x: x[1]) if known else None
        weak = min(known, key=lambda x: x[1]) if known else None
        players[uid] = {"n_maps": n, "share": {s: round(sh[s], 3) for s in STYLES}, "z": {s: round(z[s], 2) for s in STYLES},
                        "performance": perf, "primary": primary, "secondary": secondary,
                        "label": (STYLES[primary][0] + (f" / {STYLES[secondary][0]}" if secondary else "")) if primary else "Equilibrado",
                        "strong": strong[0] if strong and strong[1] >= STRONG_PTS else None,
                        "weak": weak[0] if weak and weak[1] <= -STRONG_PTS else None}
    return {"ok": True, "styles": {s: STYLES[s][0] for s in STYLES}, "base": {s: round(stats[s][0], 3) for s in STYLES}, "players": players}


def map_role(style_weights: dict[str, float] | None, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Papel de um mapa para o jogador: estilo dominante e "conforto" (estilo forte/preferido), "crescimento" (estilo fraco ou pouco jogado) ou "neutro"."""
    if not style_weights or not profile:
        return None
    pool = {k: v for k, v in style_weights.items() if k in CLASS_STYLES} or style_weights
    dom = max(pool, key=pool.get)
    z, perf = profile["z"][dom], profile["performance"][dom]["acc_pts"]
    if (perf is not None and perf <= -STRONG_PTS) or z <= -PRIMARY_Z:
        role = "crescimento"
    elif (perf is not None and perf >= STRONG_PTS) or z >= PRIMARY_Z:
        role = "conforto"
    else:
        role = "neutro"
    return {"style": dom, "label": STYLES[dom][0], "role": role}
