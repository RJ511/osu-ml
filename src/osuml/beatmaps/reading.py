"""Skill **Reading** de osu!standard — port em Python da skill oficial do osu!lazer
(ppy/osu, `Skills/Reading.cs`, `Evaluators/ReadingEvaluator.cs`, `HarmonicSkill.cs`,
`OsuDifficultyHitObject.cs`, `DiffUtils.cs`; lido em 2026-09-23 do branch master).

O `rosu-pp-py` 4.0.2 devolve `reading=None` para osu!standard, por isso não há valor oficial
disponível localmente. O que esta versão calcula, por objeto (e mod-aware, como Aim/Speed):
- dificuldade de **preempt** (tempo de reação: sobe exponencialmente abaixo de 500 ms, AR ≈ 9,66),
- dificuldade de **densidade** de objetos visíveis (passados e futuros numa janela de 3 s),
- dificuldade de **Hidden** (só com HD),
- atenuada pela **repetição de ângulos** e agravada pela **velocidade** do cursor,
- bónus de **BPM alto**; agregada com norma 1,5, tira de decaimento 0,8^(ms/1000), primeiros 60 s
  reduzidos e soma harmónica dos objetos mais difíceis; `rating = sqrt(valor) * 0,0675`.

**Diferenças conhecidas face ao original (não verificadas contra o C#):**
1. sem *stacking* (`StackedPosition`): notas empilhadas ficam à distância 0 em vez de um pequeno desvio;
2. o caminho dos sliders é aproximado: Bézier (com âncoras vermelhas) e círculo perfeito são
   amostrados, catmull é uma polilinha; os pontos aninhados (ticks/repeats) são regenerados;
3. `SliderTick` fora do intervalo de tracking (caso raro) não é reordenado;
4. mods de velocidade personalizada (`speed_change`) e Magnetised não são tratados.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .parser import ParsedBeatmap

NORMALISED_RADIUS = 50.0
NORMALISED_DIAMETER = NORMALISED_RADIUS * 2
MIN_DELTA_TIME = 25.0
ASSUMED_SLIDER_RADIUS = NORMALISED_RADIUS * 1.8
TAIL_LENIENCY = -36.0
PREEMPT_MIN = 450.0
READING_WINDOW = 3000.0
DISTANCE_INFLUENCE_THRESHOLD = NORMALISED_DIAMETER * 1.5
HD_FADE_OUT_MULTIPLIER = 0.3


# ------------------------------------------------------------------ DiffUtils
def smootherstep(x: float, start: float, end: float) -> float:
    x = min(1.0, max(0.0, (x - start) / (end - start)))
    return x * x * x * (x * (6.0 * x - 15.0) + 10.0)


def reverse_lerp(x: float, start: float, end: float) -> float:
    return min(1.0, max(0.0, (x - start) / (end - start)))


def norm(p: float, *values: float) -> float:
    return sum(v ** p for v in values) ** (1.0 / p)


def logistic_ok(x: float) -> float:  # só para documentação de CountTopWeighted (não usado no rating)
    return 1 / (1 + math.exp(-x))


# --------------------------------------------------------------- mods e escalas
def difficulty_range(value: float, minimum: float, mid: float, maximum: float) -> float:
    if value > 5:
        return mid + (maximum - mid) * (value - 5) / 5
    if value < 5:
        return mid - (mid - minimum) * (5 - value) / 5
    return mid


def mod_params(pb: ParsedBeatmap, mods: list[str]) -> dict:
    s = pb.summary()
    cs, ar, od = s["cs"], s["ar"], s["od"]
    ms = set(mods)
    if "HR" in ms:
        cs, ar, od = min(cs * 1.3, 10.0), min(ar * 1.4, 10.0), min(od * 1.4, 10.0)
    if "EZ" in ms:
        cs, ar, od = cs * 0.5, ar * 0.5, od * 0.5
    clock = 1.5 if ms & {"DT", "NC"} else 0.75 if ms & {"HT", "DC"} else 1.0
    return {
        "radius": 32 * (1 - 0.7 * (cs - 5) / 5),
        "preempt_raw": difficulty_range(ar, 1800, 1200, 450),
        "od": od, "clock": clock, "hidden": "HD" in ms,
        "slider_multiplier": s["slider_multiplier"], "tick_rate": s["slider_tick_rate"],
        "touch": "TD" in ms, "relax": "RX" in ms, "autopilot": "AP" in ms,
    }


# ------------------------------------------------------------- caminho dos sliders
def _bezier_points(pts: list[tuple[float, float]], samples: int = 24) -> list[tuple[float, float]]:
    out = []
    for k in range(samples + 1):
        t = k / samples
        work = list(pts)
        while len(work) > 1:
            work = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(work, work[1:])]
        out.append(work[0])
    return out


def _circle_arc(p0, p1, p2, samples: int = 48) -> list[tuple[float, float]] | None:
    ax, ay = p0
    bx, by = p1
    cx, cy = p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, a2 = (math.atan2(y - uy, x - ux) for x, y in (p0, p1, p2))
    two_pi = 2 * math.pi
    d1 = (a1 - a0) % two_pi
    d2 = (a2 - a0) % two_pi
    sweep = d2 if d1 < d2 else d2 - two_pi  # sentido em que p1 fica entre p0 e p2
    return [(ux + r * math.cos(a0 + sweep * k / samples), uy + r * math.sin(a0 + sweep * k / samples))
            for k in range(samples + 1)]


def _polyline(o) -> list[tuple[float, float]]:
    head = (float(o.x), float(o.y))
    pts = [head] + [(float(x), float(y)) for x, y in (o.curve_points or [])]
    kind = o.curve_type or "B"
    if kind == "P" and len(pts) == 3:
        arc = _circle_arc(*pts)
        if arc:
            return arc
        return pts
    if kind == "B" and len(pts) > 2:
        segs, cur = [], [pts[0]]
        for p in pts[1:]:
            cur.append(p)
            if len(cur) > 1 and cur[-1] == cur[-2] and len(cur) > 2:  # âncora vermelha: fecha o segmento
                segs.append(cur[:-1])
                cur = [p]
        if len(cur) > 1:
            segs.append(cur)
        out: list[tuple[float, float]] = []
        for seg in segs:
            out.extend(_bezier_points(seg) if len(seg) > 2 else seg)
        return out or pts
    return pts


class _Path:
    """Posição ao longo do slider por distância percorrida (0..length), esticado/cortado ao `length` pedido."""

    def __init__(self, o) -> None:
        poly = _polyline(o)
        cum = [0.0]
        for a, b in zip(poly, poly[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = float(o.length or 0.0)
        if self.length > 0 and cum[-1] < self.length and len(poly) >= 2:
            (x0, y0), (x1, y1) = poly[-2], poly[-1]
            seg = math.hypot(x1 - x0, y1 - y0) or 1.0
            extra = self.length - cum[-1]
            poly.append((x1 + (x1 - x0) / seg * extra, y1 + (y1 - y0) / seg * extra))
            cum.append(self.length)
        self.poly, self.cum = poly, cum

    def at(self, d: float) -> tuple[float, float]:
        d = min(max(d, 0.0), self.length if self.length > 0 else self.cum[-1])
        lo, hi = 0, len(self.cum) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.cum[mid] <= d:
                lo = mid
            else:
                hi = mid
        seg = self.cum[hi] - self.cum[lo]
        t = 0.0 if seg <= 0 else (d - self.cum[lo]) / seg
        (x0, y0), (x1, y1) = self.poly[lo], self.poly[hi]
        return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t


# ------------------------------------------------------------ objetos de dificuldade
@dataclass
class _Obj:
    base: object
    index: int
    is_spinner: bool
    is_slider: bool
    raw_start: float
    raw_preempt: float
    start: float = 0.0
    delta: float = 0.0
    adjusted_delta: float = MIN_DELTA_TIME
    preempt: float = 0.0
    pos: tuple = (0.0, 0.0)
    lazy_end: tuple | None = None
    lazy_jump: float = 0.0
    travel_distance: float = 0.0
    angle: float | None = None
    nested: list | None = None  # [(x, y, is_repeat)] cabeça, ticks/repeats, cauda

    def opacity_at(self, time: float, hidden: bool) -> float:
        if time > self.raw_start:
            return 0.0
        fade_in_start = self.raw_start - self.raw_preempt
        fade_in = 400 * min(1.0, self.raw_preempt / PREEMPT_MIN)
        if hidden:
            fade_out_start = self.raw_start - self.raw_preempt + fade_in
            fade_out = self.raw_preempt * HD_FADE_OUT_MULTIPLIER
            return min(min(1.0, max(0.0, (time - fade_in_start) / fade_in)),
                       1.0 - min(1.0, max(0.0, (time - fade_out_start) / fade_out)))
        return min(1.0, max(0.0, (time - fade_in_start) / fade_in))


def _slider_nested(o, path: _Path, prm: dict, beat_length: float, sv: float) -> tuple[list, float, tuple]:
    """(nested [(x,y,is_repeat)], lazy_travel_time, lazy_end_position) — ticks e repeats regenerados."""
    slides = max(1, int(o.slides or 1))
    length = path.length or path.cum[-1]
    duration = max(o.end_time - o.time, 0.0)
    span = duration / slides if slides else duration
    scoring = 100 * prm["slider_multiplier"] * (sv or 1.0)
    tick_dist = scoring / max(prm["tick_rate"], 1e-9)
    velocity = scoring / beat_length if beat_length else 0.0
    min_from_end = velocity * 10
    nested: list = [(float(o.x), float(o.y), False)]
    for s in range(slides):
        forward = s % 2 == 0
        d = tick_dist
        while tick_dist > 0 and d <= length:
            if d >= length - min_from_end:
                break
            nested.append((*path.at(d if forward else length - d), False))
            d += tick_dist
        if s < slides - 1:
            nested.append((*path.at(length if forward else 0.0), True))
    nested.append((*path.at(length if slides % 2 == 1 else 0.0), False))

    tracking_end = max(duration + TAIL_LENIENCY, duration / 2)
    end_min = tracking_end / span if span else 0.0
    end_min = 1 - end_min % 1 if end_min % 2 >= 1 else end_min % 1
    lazy_end = path.at(end_min * length)
    return nested, tracking_end, lazy_end


def _build_objects(pb: ParsedBeatmap, prm: dict) -> list[_Obj]:
    hos = pb.hit_objects
    clock, radius = prm["clock"], prm["radius"]
    scaling = NORMALISED_RADIUS / radius
    objs: list[_Obj] = []
    for i in range(1, len(hos)):
        h, last = hos[i], hos[i - 1]
        o = _Obj(base=h, index=i - 1, is_spinner=h.kind == "spinner", is_slider=h.kind == "slider",
                 raw_start=float(h.time), raw_preempt=prm["preempt_raw"])
        o.start = h.time / clock
        o.delta = (h.time - last.time) / clock
        o.adjusted_delta = max(o.delta, MIN_DELTA_TIME)
        o.preempt = prm["preempt_raw"] / clock
        o.pos = (float(h.x), float(h.y))
        if o.is_slider:
            path = _Path(h)
            o.nested, travel_time, o.lazy_end = _slider_nested(h, path, prm, h.beat_length or 0.0, h.sv or 1.0)
            cur = o.pos
            for k in range(1, len(o.nested)):
                nx, ny, is_rep = o.nested[k]
                mx, my = nx - cur[0], ny - cur[1]
                mlen = scaling * math.hypot(mx, my)
                required = ASSUMED_SLIDER_RADIUS
                if k == len(o.nested) - 1:
                    lx, ly = o.lazy_end[0] - cur[0], o.lazy_end[1] - cur[1]
                    if math.hypot(lx, ly) < math.hypot(mx, my):
                        mx, my = lx, ly
                    mlen = scaling * math.hypot(mx, my)
                elif is_rep:
                    required = NORMALISED_RADIUS
                if mlen > required:
                    frac = (mlen - required) / mlen
                    cur = (cur[0] + mx * frac, cur[1] + my * frac)
                    o.travel_distance += mlen * frac
                if k == len(o.nested) - 1:
                    o.lazy_end = cur
        objs.append(o)

    for idx, o in enumerate(objs):
        h, last = o.base, hos[idx]  # hos[idx] = objeto anterior (LastObject)
        if o.is_spinner or last.kind == "spinner":
            continue
        prev = objs[idx - 1] if idx >= 1 else None
        last_cursor = (prev.lazy_end or prev.pos) if prev is not None else (float(last.x), float(last.y))
        o.lazy_jump = math.hypot(o.pos[0] - last_cursor[0], o.pos[1] - last_cursor[1]) * scaling
        if idx >= 2 and not objs[idx - 2].is_spinner:
            pp = objs[idx - 2]
            lastlast = pp.lazy_end or pp.pos
            if prev.is_slider and prev.travel_distance > 0:
                last_cursor = prev.pos

            def angle(cur, lp, llp):
                v1 = (llp[0] - lp[0], llp[1] - lp[1])
                v2 = (cur[0] - lp[0], cur[1] - lp[1])
                return abs(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))

            a = angle(o.pos, last_cursor, lastlast)
            end_prev = prev.lazy_end or prev.pos
            llp2 = lastlast
            if prev.is_slider and prev.travel_distance > 0 and prev.nested and len(prev.nested) >= 2:
                llp2 = prev.nested[-2][:2]
            a2 = angle(o.pos, end_prev, llp2)
            o.angle = min(a, a2)
    return objs


# ------------------------------------------------------------ ReadingEvaluator
def _time_nerf(dt: float) -> float:
    return min(1.0, max(0.0, 2 - dt / (READING_WINDOW / 2)))


def _high_bpm_bonus(ms: float) -> float:
    return 1 / (1 - 0.8 ** (ms / 1000))


def _constant_angle_nerf(objs: list[_Obj], cur: _Obj) -> float:
    count, gap, k = 0.0, 0.0, 0
    p0, p1, p2 = cur, None, None
    while gap < 2000:
        j = cur.index - 1 - k
        if j < 0:
            break
        lo = objs[j]
        long_interval = 1 - reverse_lerp(lo.adjusted_delta, 200, 2000)
        if lo.angle is not None and cur.angle is not None:
            diff = abs(cur.angle - lo.angle)
            alt = math.pi
            if p0.angle is not None and p1 is not None and p1.angle is not None and p2 is not None and p2.angle is not None:
                alt = abs(p1.angle - lo.angle) + abs(p2.angle - p0.angle)
                w = reverse_lerp(min(lo.angle, p0.angle) * 180 / math.pi, 20, 5)
                w *= reverse_lerp(max(lo.angle, p0.angle) * 180 / math.pi, 60, 120)
                alt = math.pi + (0.1 * alt - math.pi) * w
            stack = smootherstep(lo.lazy_jump, 0, NORMALISED_RADIUS)
            count += math.cos(3 * min(math.radians(30), min(diff, alt) * stack)) * long_interval
        gap = cur.start - lo.start
        k += 1
        p2, p1, p0 = p1, p0, lo
    return min(1.0, max(0.2, 2 / count)) if count > 0 else 1.0


def _past_influence(objs: list[_Obj], cur: _Obj) -> float:
    total = 0.0
    for i in range(cur.index):
        lo = objs[cur.index - 1 - i]
        if cur.start - lo.start > READING_WINDOW or lo.start < cur.start - cur.preempt:
            break
        d = cur.opacity_at(lo.raw_start, False) * smootherstep(lo.lazy_jump, 15, DISTANCE_INFLUENCE_THRESHOLD)
        total += d * _time_nerf(cur.start - lo.start)
    return total


def _visible_density(objs: list[_Obj], cur: _Obj) -> float:
    count, j = 0.0, cur.index + 1
    while j < len(objs):
        ho = objs[j]
        if ho.start - cur.start > READING_WINDOW or cur.start < ho.start - ho.preempt:
            break
        count += ho.opacity_at(cur.raw_start, False) * _time_nerf(ho.start - cur.start)
        j += 1
    return count


def object_difficulty(objs: list[_Obj], cur: _Obj, hidden: bool) -> float:
    if cur.is_spinner or cur.index == 0:
        return 0.0
    nxt = objs[cur.index + 1] if cur.index + 1 < len(objs) else None
    velocity = max(1.0, cur.lazy_jump / cur.adjusted_delta)
    visible = _visible_density(objs, cur)
    past = _past_influence(objs, cur)
    angle_nerf = _constant_angle_nerf(objs, cur)

    future = math.sqrt(visible)
    if nxt is not None:
        future *= smootherstep(nxt.lazy_jump, 15, DISTANCE_INFLUENCE_THRESHOLD)
    density = (past + future) ** 1.7 * 0.4 * angle_nerf * velocity
    density = max(0.0, density - 2.5) ** 0.45 * 2.4

    hidden_diff = 0.0
    if hidden:
        pre = cur.preempt ** 2.2 * 0.01
        dens = (visible + past) ** 3.3 * 3
        hidden_diff = ((pre + dens) * angle_nerf * velocity * 0.01) ** 0.4 * 0.28
        prev = objs[cur.index - 1]
        if (cur.lazy_jump == 0 and prev.opacity_at(prev.raw_start, True) == 0 and cur.opacity_at(prev.raw_start, True) == 0
                and prev.start > cur.start - cur.preempt):
            hidden_diff += 0.28 * 2500 / cur.adjusted_delta ** 1.5

    preempt_diff = (max(0.0, 500 - cur.preempt) ** 2.5) / 140000 * angle_nerf * velocity
    return norm(1.5, preempt_diff, hidden_diff, density) * _high_bpm_bonus(cur.adjusted_delta)


# ------------------------------------------------------------------ Reading skill
def reading_difficulty_value(pb: ParsedBeatmap, mods: list[str] | tuple[str, ...] = ()) -> float:
    """Valor de dificuldade bruto (antes da conversão em rating)."""
    prm = mod_params(pb, list(mods))
    objs = _build_objects(pb, prm)
    if not objs:
        return 0.0
    od_hit = (80 - 6 * prm["od"]) / prm["clock"]
    od_eff = (79.5 - od_hit / 2) / 6
    od_mult = 0.825 + max(0.0, od_eff) ** 2.2 / 1125.0

    strain, diffs = 0.0, []
    first_start = objs[0].start
    reduced_until = first_start + 60_000
    reduced_count = 0
    for o in objs:
        decay = 0.8 ** (o.delta / 1000)
        d = object_difficulty(objs, o, prm["hidden"])
        if prm["touch"]:
            d = d ** 0.89
        if prm["relax"]:
            d *= 0.4
        if prm["autopilot"]:
            d *= 0.1
        d *= od_mult
        strain = strain * decay + d * (1 - decay) * 2.5
        diffs.append(strain)
        if o.start <= reduced_until:
            reduced_count += 1

    diffs = [v for v in diffs if v > 0]
    for i in range(min(len(diffs), reduced_count)):
        scale = math.log10(1 + 9 * min(1.0, max(0.0, i / reduced_count)))  # log10(lerp(1, 10, t))
        diffs[i] *= scale

    total = 0.0
    for idx, v in enumerate(sorted(diffs, reverse=True)):
        h = 1.0 / (1 + idx)
        total += v * (1 + h) / (idx ** 0.9 + 1 + h)
    return total


def reading_rating(pb: ParsedBeatmap, mods: list[str] | tuple[str, ...] = ()) -> float:
    """Rating de Reading (mesma conversão do calculador oficial: sqrt(valor) * 0,0675)."""
    return math.sqrt(reading_difficulty_value(pb, mods)) * 0.0675
