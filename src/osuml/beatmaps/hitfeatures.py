"""Features de mapa a partir dos hit objects crus (`parser.py`) — Stamina, Reading visual e Tech
(`docs/skills_comunidade.md`, secções 2.2, 3.1 e 4.1). Usado tanto para os mapas do jogador como
para a pool de referência: mesma função, mesma fonte (`ParsedBeatmap`), para nunca haver duas
implementações a divergir.

Só **nomod**: AR muda com DT/HT e por isso a leitura visual também devia mudar com mods, mas isso
fica como refinamento futuro (Q3.2) — não bloqueia esta primeira versão.
"""

from __future__ import annotations

import math
from collections import Counter

from .parser import ParsedBeatmap

_SNAP_FRACTIONS = (1, 1 / 2, 1 / 3, 1 / 4, 1 / 6, 1 / 8, 1 / 12, 1 / 16)
_SNAP_TOLERANCE = 0.08  # 8% de erro relativo: acima disto, cai no cesto "other"


def circle_radius(cs: float) -> float:
    """Raio do circle em osu!pixels, fórmula oficial (playfield 512x384)."""
    return 54.4 - 4.48 * cs


def preempt_ms(ar: float) -> float:
    """Tempo de aproximação (ms) — fórmula oficial AR→preempt."""
    if ar < 5:
        return 1200 + 600 * (5 - ar) / 5
    if ar > 5:
        return 1200 - 750 * (ar - 5) / 5
    return 1200.0


def density(n_objects: int, first_ms: float | None, last_ms: float | None) -> float:
    """Objetos por segundo — proxy de Stamina (Q2.2/Q2.3, simplificada por decisão do utilizador):
    mapas curtos e densos cansam/treinam stamina; mapas longos e esparsos treinam antes
    consistência, não stamina."""
    if not n_objects or first_ms is None or last_ms is None:
        return 0.0
    duration_s = (last_ms - first_ms) / 1000
    return round(n_objects / duration_s, 4) if duration_s > 0 else 0.0


def reading_visual(objects: list[tuple[float, float, float]], ar: float, cs: float) -> float:
    """`objects`: (time_ms, x, y) ordenados por tempo. Fração de objetos com pelo menos um
    predecessor ainda "visível" (dentro da janela de preempt) que se sobrepõe espacialmente —
    proxy de densidade visual/reading (Q3.1)."""
    if len(objects) < 2:
        return 0.0
    preempt = preempt_ms(ar)
    radius = circle_radius(cs)
    overlapping = 0
    for i in range(1, len(objects)):
        t_i, x_i, y_i = objects[i]
        j = i - 1
        while j >= 0 and t_i - objects[j][0] < preempt:
            _, x_j, y_j = objects[j]
            if math.hypot(x_i - x_j, y_i - y_j) < 2 * radius:
                overlapping += 1
                break
            j -= 1
    return round(overlapping / (len(objects) - 1), 4)


def _snap_bucket(ratio: float) -> str:
    if ratio <= 0:
        return "other"
    best, best_err = "other", _SNAP_TOLERANCE
    for frac in _SNAP_FRACTIONS:
        k = round(ratio / frac)
        if k <= 0:
            continue
        err = abs(ratio - k * frac) / (k * frac)
        if err < best_err:
            best, best_err = f"{frac:.4f}", err
    return best


def tech_entropy(times_beat: list[tuple[float, float]]) -> float:
    """`times_beat`: (time_ms, beat_length_ativo_nesse_objeto) ordenados por tempo. Entropia
    normalizada (0-1) da distribuição de snaps rítmicos — proxy de Tech (Q4.1): muitos snaps
    diferentes = mais "tech"; um snap dominante = stream/jump puro, baixa entropia."""
    if len(times_beat) < 3:
        return 0.0
    buckets: Counter[str] = Counter()
    for i in range(1, len(times_beat)):
        t_i, bl_i = times_beat[i]
        t_prev, _ = times_beat[i - 1]
        dt = t_i - t_prev
        if dt <= 0 or not bl_i:
            continue
        buckets[_snap_bucket(dt / bl_i)] += 1
    total = sum(buckets.values())
    if total == 0 or len(buckets) < 2:
        return 0.0
    probs = [c / total for c in buckets.values()]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    return round(entropy / math.log2(len(buckets)), 4)


def compute_from_parsed(pb: ParsedBeatmap) -> dict:
    """`density`, `reading_visual` e `tech_entropy` a partir de um `.osu` já parseado. Fonte única
    reutilizada tanto pelos mapas do jogador como pela pool de referência."""
    hos = pb.hit_objects
    if not hos:
        return {"density": 0.0, "reading_visual": 0.0, "tech_entropy": 0.0}
    s = pb.summary()
    return {
        "density": density(len(hos), s["first_object_ms"], s["last_object_end_ms"]),
        "reading_visual": reading_visual([(o.time, o.x, o.y) for o in hos], s["ar"], s["cs"]),
        "tech_entropy": tech_entropy([(o.time, o.beat_length or 0) for o in hos]),
    }
