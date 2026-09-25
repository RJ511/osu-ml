"""Pontos de falha: **onde** no mapa o jogador falhou e **se foi mesmo uma morte** (HP) ou só um reinício.

Dados: falhas do lazer na BD (`passed = 0`, rank F; o stable não envia falhas). Cada score traz as contagens de acertos até ao momento da falha
(`statistics`) e o máximo do mapa (`maximum_statistics`).

1. **Progresso** = (great + ok + meh + miss) / máximo de "great" (nº de objetos julgados / nº de objetos). Validado: correlação 0,95 com a duração real da
   jogada (`ended_at − started_at`) sobre a duração do mapa (1 767 falhas). O objeto n-ésimo dá o instante da falha (`t_fail`).
2. **Morreu ou reiniciou?** A API não distingue. Modelo de HP do osu!lazer (`DrainingHealthProcessor` + `OsuHealthProcessor`, lidos do GitHub em 2026-09-25):
   o dreno é calibrado para que um jogo PERFEITO nunca desça abaixo de uma vida mínima `H_min(HP) = 0,99 / 0,90 / 0,40` (HP 0 / 5 / 10; interpolação linear).
   Cada resultado imperfeito tira vida face ao perfeito (great = +0,03): miss `0,03 + pen_miss(HP)` com `pen_miss = 0,03 / 0,125 / 0,20`; meh `0,028`; ok `0,019`;
   tick grande falhado `0,015 + pen_tick(HP)` com `pen_tick = 0,02 / 0,075 / 0,14`. Sem a ordem dos erros só se pode limitar: **só se pode ter morrido se o dano total
   `D` atingir `H_min`** (pior caso: todos os erros juntos no ponto de vida mais baixa). `D < H_min` => **reinício certo**. `D >= H_min` => "pode ser morte"; `D >= 1,5·H_min`
   => "morte provável" (heurística: mesmo repartidos, os erros custaram vida a mais). HR multiplica o HP por 1,4 (máx. 10), EZ por 0,5 (e tem vidas extra: excluído);
   SD/PF morrem ao primeiro erro; RX/AP excluídos.
3. **Trecho da falha**: janela de 10 s antes de `t_fail` (densidade, espaçamento, velocidade, streams, sliders, "intensidade") e o percentil dessa janela entre todas as
   janelas do mapa. Se a classificação de morte fizer sentido, as mortes concentram-se em trechos difíceis (percentil alto) e os reinícios não.

Limites: aproximação do HP (sem a ordem dos erros, sem pausas/breaks, ticks pequenos ignorados); mapas fora do catálogo v1 ficam de fora; só mods DT/HT ajustam o tempo real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..progress import Progress

HP_TARGET = (0.99, 0.90, 0.40)     # vida mínima de um jogo perfeito, HP 0/5/10
MISS_PEN = (0.03, 0.125, 0.20)
TICK_PEN = (0.02, 0.075, 0.14)
GREAT, OK, MEH, TICK_HIT = 0.03, 0.011, 0.002, 0.015
WINDOW_MS = 10_000
LIKELY_RATIO = 1.5
_ID = re.compile(r"^(\d+)\.osu$")


def hp_range(hp: float, lo: float, mid: float, hi: float) -> float:
    """`IBeatmapDifficultyInfo.DifficultyRange`: interpolação linear 0 -> lo, 5 -> mid, 10 -> hi."""
    hp = max(0.0, min(10.0, float(hp)))
    return lo + (mid - lo) * hp / 5.0 if hp <= 5 else mid + (hi - mid) * (hp - 5.0) / 5.0


def effective_hp(hp: float, mods: str) -> float:
    m = set((mods or "").split(","))
    if "HR" in m:
        hp = min(10.0, hp * 1.4)
    if "EZ" in m:
        hp = hp * 0.5
    return hp


def damage(stats: dict[str, Any], hp: float) -> float:
    """Dano (vida perdida face a um jogo perfeito) causado pelos erros registados nas estatísticas."""
    g = lambda k: float(stats.get(k, 0) or 0)  # noqa: E731
    miss_pen, tick_pen = hp_range(hp, *MISS_PEN), hp_range(hp, *TICK_PEN)
    return (g("miss") * (GREAT + miss_pen) + g("meh") * (GREAT - MEH) + g("ok") * (GREAT - OK)
            + (g("large_tick_miss") + g("small_tick_miss")) * (TICK_HIT + tick_pen))


def classify(stats: dict[str, Any], hp: float, mods: str) -> tuple[str, float]:
    """(classe, D / H_min). Classes: reinicio_certo | pode_ser_morte | morte_provavel | sd_pf | excluida."""
    m = set((mods or "").split(","))
    if m & {"EZ", "RX", "AP"}:
        return "excluida", float("nan")
    if m & {"SD", "PF"}:
        bad = sum(float(stats.get(k, 0) or 0) for k in ("miss", "meh", "ok") if k == "miss" or "PF" in m)
        return ("sd_pf" if bad > 0 else "reinicio_certo"), float("nan")
    hp_e = effective_hp(hp, mods)
    ratio = damage(stats, hp_e) / hp_range(hp_e, *HP_TARGET)
    return ("reinicio_certo" if ratio < 1.0 else ("morte_provavel" if ratio >= LIKELY_RATIO else "pode_ser_morte")), ratio


def progress(stats: dict[str, Any], maximum: dict[str, Any]) -> float | None:
    total = float((maximum or {}).get("great") or 0)
    if total <= 0:
        return None
    return min(1.0, sum(float((stats or {}).get(k, 0) or 0) for k in ("great", "ok", "meh", "miss")) / total)


def load_maps(bundle: Path, wanted: set[int], progress_cb=None) -> dict[int, dict[str, Any]]:
    """Objetos (tempo, x, y, tipo) e dificuldade dos mapas pedidos, lidos do bundle/arquivo de `.osu` em streaming."""
    import numpy as np

    from ..beatmaps.acquire import _iter_path
    from ..beatmaps.parser import parse_osu

    out: dict[int, dict[str, Any]] = {}
    for n, (name, data) in enumerate(_iter_path(Path(bundle)), 1):
        mt = _ID.match(Path(name).name)
        if not mt or int(mt.group(1)) not in wanted:
            continue
        try:
            pb = parse_osu(data.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if pb.mode != 0 or not pb.hit_objects:
            continue
        o = pb.hit_objects
        out[int(mt.group(1))] = {
            "t": np.array([h.time for h in o], dtype=np.float64), "x": np.array([h.x for h in o], dtype=np.float64), "y": np.array([h.y for h in o], dtype=np.float64),
            "slider": np.array([h.kind == "slider" for h in o]), "hp": pb.diff("HPDrainRate", 5.0), "cs": pb.diff("CircleSize", 5.0)}
        if progress_cb and len(out) % 200 == 0:
            progress_cb(len(out))
        if len(out) == len(wanted):
            break
    return out


def window_features(mp: dict[str, Any], t_end: float, window_ms: float = WINDOW_MS) -> dict[str, float]:
    """Características dos objetos em [t_end − janela, t_end]."""
    import numpy as np

    t, x, y = mp["t"], mp["x"], mp["y"]
    sel = np.nonzero((t >= t_end - window_ms) & (t <= t_end))[0]
    if len(sel) < 2:
        return {"density": float(len(sel)) / (window_ms / 1000.0), "spacing": 0.0, "speed": 0.0, "stream_share": 0.0, "slider_share": float(mp["slider"][sel].mean()) if len(sel) else 0.0, "intensity": 0.0}
    dt = np.maximum(np.diff(t[sel]), 1.0)
    dist = np.hypot(np.diff(x[sel]), np.diff(y[sel]))
    radius = 54.4 - 4.48 * mp["cs"]
    return {"density": len(sel) / (window_ms / 1000.0), "spacing": float(dist.mean()), "speed": float((dist / dt).mean()), "stream_share": float((dt <= 125).mean()),
            "slider_share": float(mp["slider"][sel].mean()),
            "intensity": float(np.sum((1.0 + dist / (2.0 * radius)) / np.maximum(dt, 30.0)) * 1000.0 / (window_ms / 1000.0))}


def window_percentile(mp: dict[str, Any], t_end: float, key: str = "intensity") -> float:
    """Percentil (0-1) da janela que termina em `t_end` entre as janelas do mapa (passo de 2 s)."""
    import numpy as np

    cache = mp.setdefault("_grid", {})
    if key not in cache:
        ends = np.arange(mp["t"][0] + WINDOW_MS, mp["t"][-1] + 1, 2000.0)
        cache[key] = np.sort(np.array([window_features(mp, e)[key] for e in ends])) if len(ends) else np.array([0.0])
    grid = cache[key]
    v = window_features(mp, t_end)[key]
    lo, hi = np.searchsorted(grid, v, side="left"), np.searchsorted(grid, v, side="right")
    return float((lo + hi) / 2.0 / max(len(grid), 1))  # posto médio: janelas iguais (mapas monótonos) não empurram tudo para o topo


def analyze(store, bundle: Path, out_dir: Path, version: str = "v1", *, progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import select

    from ..storage import models as m

    sc = m.scores.c
    with store.engine.connect() as c:
        names = dict(c.execute(select(m.users.c.user_id, m.users.c.username)).all())
        fails = c.execute(select(sc.score_id, sc.user_id, sc.beatmap_id, sc.accuracy, sc.statistics, sc.maximum_statistics, sc.mod_acronyms, sc.started_at, sc.ended_at)
                          .where(sc.passed == False, sc.legacy_score_id.is_(None), sc.rank == "F", sc.beatmap_id.isnot(None))).all()  # noqa: E712
    prog = Progress(progress_path, "Pontos de falha — a ler os mapas", 100, "%")
    maps = load_maps(bundle, {int(r[2]) for r in fails}, lambda n: prog.update(min(60, n / 40)))
    prog.update(60, label="Pontos de falha — a classificar e a medir os trechos", force=True)
    rows: list[dict[str, Any]] = []
    for k, (sid, uid, bid, acc, stt, mx, mods, t0, t1) in enumerate(fails):
        mp = maps.get(int(bid))
        pr = progress(stt or {}, mx or {})
        if mp is None or pr is None:
            continue
        n_obj = len(mp["t"])
        n_j = int(round(pr * float((mx or {}).get("great") or n_obj)))
        idx = max(0, min(n_obj - 1, n_j - 1)) if n_j > 0 else 0
        t_fail = float(mp["t"][idx])
        klass, ratio = classify(stt or {}, mp["hp"], mods or "")
        f = window_features(mp, t_fail)
        rate = 1.5 if ("DT" in (mods or "") or "NC" in (mods or "")) else (0.75 if ("HT" in (mods or "") or "DC" in (mods or "")) else 1.0)
        length_s = (float(mp["t"][-1]) - float(mp["t"][0])) / 1000.0 / rate
        dur = (t1 - t0).total_seconds() if (t0 and t1) else None
        rows.append({"score_id": int(sid), "user_id": int(uid), "beatmap_id": int(bid), "mods": mods or "", "accuracy": float(acc or 0.0), "progress": float(pr),
                     "duration_ratio": (dur / length_s if dur and length_s > 0 else None), "t_fail_ms": t_fail, "hp": float(mp["hp"]), "klass": klass,
                     "damage_ratio": None if ratio != ratio else float(ratio), "miss": int((stt or {}).get("miss", 0) or 0), "ok": int((stt or {}).get("ok", 0) or 0),
                     "meh": int((stt or {}).get("meh", 0) or 0), **{f"w_{a}": b for a, b in f.items()},
                     "pct_intensity": window_percentile(mp, t_fail, "intensity"), "pct_density": window_percentile(mp, t_fail, "density"),
                     "pct_speed": window_percentile(mp, t_fail, "speed")})
        if k % 200 == 0:
            prog.update(60 + 35 * k / max(len(fails), 1))
    target = Path(out_dir) / version
    target.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), target / "fail_points.parquet")
    summary = summarize(rows, names)
    summary["input"] = {"fails_in_db": len(fails), "analysed": len(rows), "maps_found": len(maps), "maps_needed": len({int(r[2]) for r in fails})}
    (target / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    prog.finish()
    return summary


def summarize(rows: list[dict[str, Any]], names: dict[int, str] | None = None) -> dict[str, Any]:
    import numpy as np

    names = names or {}
    out: dict[str, Any] = {"n": len(rows), "classes": {}, "by_class": {}, "validation": {}, "players": {}}
    for kl in sorted({r["klass"] for r in rows}):
        rs = [r for r in rows if r["klass"] == kl]
        out["classes"][kl] = len(rs)
        out["by_class"][kl] = {"progress_median": round(float(np.median([r["progress"] for r in rs])), 3), "accuracy_median": round(float(np.median([r["accuracy"] for r in rs])), 3),
                               "miss_median": float(np.median([r["miss"] for r in rs])), "pct_intensity_median": round(float(np.median([r["pct_intensity"] for r in rs])), 3),
                               "pct_intensity_mean": round(float(np.mean([r["pct_intensity"] for r in rs])), 3)}
    rest = [r["pct_intensity"] for r in rows if r["klass"] == "reinicio_certo"]
    die = [r["pct_intensity"] for r in rows if r["klass"] in ("pode_ser_morte", "morte_provavel")]
    if rest and die:  # se a classificação faz sentido, as mortes caem em trechos mais intensos do que os reinícios
        out["validation"] = {"pct_intensity_mean_restart": round(float(np.mean(rest)), 3), "pct_intensity_mean_death": round(float(np.mean(die)), 3),
                             "n_restart": len(rest), "n_death": len(die)}
    for uid in sorted({r["user_id"] for r in rows}):
        rs = [r for r in rows if r["user_id"] == uid]
        if len(rs) < 15:
            continue
        deaths = [r for r in rs if r["klass"] in ("pode_ser_morte", "morte_provavel")]
        out["players"][names.get(uid, str(uid))] = {
            "fails": len(rs), "restart_certain": sum(r["klass"] == "reinicio_certo" for r in rs), "possible_deaths": len(deaths),
            "death_pct_intensity_mean": round(float(np.mean([r["pct_intensity"] for r in deaths])), 3) if deaths else None,
            "death_window_mean": {k: round(float(np.mean([r[f"w_{k}"] for r in deaths])), 2) for k in ("density", "spacing", "speed", "stream_share", "slider_share")} if deaths else None}
    return out
