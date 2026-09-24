"""Parser do formato .osu (texto), focado em preservar informação.

Referência: https://osu.ppy.sh/wiki/en/Client/File_formats/osu_(file_format)

Não converte o mapa num vetor de features: devolve a estrutura completa
(secções, timing points, hit objects com dados de slider) para que as features
possam ser recalculadas de formas diferentes mais tarde. O ficheiro original
fica sempre guardado em data/raw/osu_files/.

Tempo de fim dos sliders:
    duração_por_passagem = length / (SliderMultiplier * 100 * SV) * beatLength
    SV = -100 / beatLength do inherited point ativo (1.0 se não houver)
    end_time = time + duração_por_passagem * slides
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

# Bits do campo `type` dos hit objects
CIRCLE, SLIDER, NEW_COMBO, SPINNER, HOLD = 1, 2, 4, 8, 128


@dataclass
class TimingPoint:
    time: float
    beat_length: float
    meter: int
    uninherited: bool
    kiai: bool


@dataclass
class HitObject:
    index: int
    time: int
    end_time: float
    x: int
    y: int
    kind: str  # circle | slider | spinner | hold
    new_combo: bool
    combo_skip: int
    hitsound: int
    curve_type: str | None = None
    curve_points: list[tuple[int, int]] | None = None
    slides: int | None = None
    length: float | None = None
    beat_length: float | None = None  # beatLength do uninherited ativo (ms por batida)
    sv: float | None = None  # multiplicador de slider velocity ativo


@dataclass
class ParsedBeatmap:
    format_version: int | None
    general: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    difficulty: dict[str, str] = field(default_factory=dict)
    timing_points: list[TimingPoint] = field(default_factory=list)
    hit_objects: list[HitObject] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def diff(self, key: str, default: float) -> float:
        try:
            return float(self.difficulty.get(key, default))
        except ValueError:
            return default

    @property
    def mode(self) -> int:
        try:
            return int(self.general.get("Mode", 0))
        except ValueError:
            return 0

    def summary(self) -> dict[str, Any]:
        objs = self.hit_objects
        od = self.diff("OverallDifficulty", 5)
        return {
            "format_version": self.format_version,
            "mode": self.mode,
            "hp": self.diff("HPDrainRate", 5),
            "cs": self.diff("CircleSize", 5),
            "od": od,
            "ar": self.diff("ApproachRate", od),  # formatos antigos: AR = OD
            "slider_multiplier": self.diff("SliderMultiplier", 1.4),
            "slider_tick_rate": self.diff("SliderTickRate", 1),
            "n_objects": len(objs),
            "n_circles": sum(o.kind == "circle" for o in objs),
            "n_sliders": sum(o.kind == "slider" for o in objs),
            "n_spinners": sum(o.kind == "spinner" for o in objs),
            "first_object_ms": objs[0].time if objs else None,
            "last_object_end_ms": max((o.end_time for o in objs), default=None),
            "n_timing_points": len(self.timing_points),
            "n_uninherited": sum(t.uninherited for t in self.timing_points),
            "parse_warnings": len(self.warnings),
        }


class _TimingIndex:
    """Resolve o beatLength (uninherited) e o SV (inherited) ativos num instante."""

    def __init__(self, points: list[TimingPoint]) -> None:
        # Ordenação estável: no mesmo instante, uninherited aplica-se antes do inherited.
        pts = sorted(points, key=lambda p: (p.time, not p.uninherited))
        self.red = [p for p in pts if p.uninherited]
        self.green = [p for p in pts if not p.uninherited]
        self.red_t = [p.time for p in self.red]
        self.green_t = [p.time for p in self.green]

    def at(self, t: float) -> tuple[float, float]:
        if not self.red:
            return 500.0, 1.0
        i = bisect.bisect_right(self.red_t, t) - 1
        red = self.red[max(i, 0)]  # antes do 1.º ponto usa o 1.º (comportamento do jogo)
        sv = 1.0
        j = bisect.bisect_right(self.green_t, t) - 1
        if j >= 0 and self.green[j].time >= red.time:
            bl = self.green[j].beat_length
            if bl < 0:
                sv = min(max(-100.0 / bl, 0.1), 10.0)
        return red.beat_length, sv


def _kv(line: str, sep: str) -> tuple[str, str] | None:
    if sep not in line:
        return None
    k, v = line.split(sep, 1)
    return k.strip(), v.strip()


def parse_osu(text: str) -> ParsedBeatmap:
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    pb = ParsedBeatmap(format_version=None)
    if lines and lines[0].strip().startswith("osu file format v"):
        try:
            pb.format_version = int(lines[0].strip().rsplit("v", 1)[1])
        except ValueError:
            pb.warnings.append("versão de formato ilegível")
    section = None
    raw_objects: list[str] = []
    for line in lines[1:]:
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            continue
        if section in ("General", "Editor"):
            kv = _kv(s, ":")
            if kv and section == "General":
                pb.general[kv[0]] = kv[1]
        elif section == "Metadata":
            kv = _kv(s, ":")
            if kv:
                pb.metadata[kv[0]] = kv[1]
        elif section == "Difficulty":
            kv = _kv(s, ":")
            if kv:
                pb.difficulty[kv[0]] = kv[1]
        elif section == "TimingPoints":
            parts = s.split(",")
            try:
                beat_length = float(parts[1])
                uninherited = bool(int(parts[6])) if len(parts) > 6 else beat_length > 0
                pb.timing_points.append(TimingPoint(
                    time=float(parts[0]),
                    beat_length=beat_length,
                    meter=int(parts[2]) if len(parts) > 2 and parts[2] else 4,
                    uninherited=uninherited,
                    kiai=bool(int(parts[7]) & 1) if len(parts) > 7 and parts[7] else False,
                ))
            except (ValueError, IndexError):
                pb.warnings.append(f"timing point ilegível: {s[:60]}")
        elif section == "HitObjects":
            raw_objects.append(s)

    timing = _TimingIndex(pb.timing_points)
    slider_mult = pb.diff("SliderMultiplier", 1.4)
    for idx, s in enumerate(raw_objects):
        parts = s.split(",")
        try:
            x, y, t, typ, hs = int(float(parts[0])), int(float(parts[1])), int(float(parts[2])), int(parts[3]), int(parts[4])
        except (ValueError, IndexError):
            pb.warnings.append(f"hit object ilegível: {s[:60]}")
            continue
        beat_length, sv = timing.at(t)
        obj = HitObject(
            index=idx, time=t, end_time=float(t), x=x, y=y, kind="circle",
            new_combo=bool(typ & NEW_COMBO), combo_skip=(typ >> 4) & 7, hitsound=hs,
            beat_length=beat_length, sv=sv,
        )
        try:
            if typ & SLIDER:
                obj.kind = "slider"
                curve = parts[5].split("|")
                obj.curve_type = curve[0]
                obj.curve_points = [
                    (int(float(a)), int(float(b))) for a, b in (p.split(":", 1) for p in curve[1:] if ":" in p)
                ]
                obj.slides = int(parts[6])
                obj.length = float(parts[7])
                per_slide = obj.length / (slider_mult * 100 * sv) * beat_length
                obj.end_time = t + per_slide * obj.slides
            elif typ & SPINNER:
                obj.kind = "spinner"
                obj.end_time = float(parts[5])
            elif typ & HOLD:
                obj.kind = "hold"
                obj.end_time = float(parts[5].split(":", 1)[0])
        except (ValueError, IndexError, ZeroDivisionError):
            pb.warnings.append(f"dados de objeto incompletos no índice {idx}")
        pb.hit_objects.append(obj)
    return pb
