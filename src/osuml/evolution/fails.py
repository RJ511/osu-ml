"""Falhas do jogador: **morte ou reinício?** e **onde** no mapa (0 pedidos à API).

Reaproveita `analysis/fail_points.py` (modelo de HP do osu!lazer) e junta a curva de dificuldade do mirror (strains, quando já guardados):
- **Reinício** (`reinicio_certo`): os erros acumulados até ao ponto de saída não chegavam para esvaziar a vida mesmo no pior caso: saíste tu (ou foi um `retry`).
- **Morte** (`pode_ser_morte` / `morte_provavel`): o dano chegava para a vida acabar. Sem a ordem exata dos erros só se pode limitar (pior caso), por isso "pode ser morte" é
  o limite inferior e "provável" quando o dano foi ≥ 1,5× a vida mínima.
- **Onde**: `progress` = objetos julgados / total → instante da falha → fração do mapa; com os strains, em que percentil da dificuldade do mapa (e que fração do pico) estavas.
Só falhas do lazer (o stable não envia falhas), só mapas com `.osu` guardado.

**Reiniciar não mede habilidade nem dificuldade** (decisão do utilizador, 2026-10-15): quem quer accuracy alta reinicia ao primeiro erro parvo, e muitos reinícios seguidos podem ser
bloqueio mental. Por isso os reinícios são só descritos (hábito) e **não entram** no teste semanal (a "1.ª jogada" é a 1.ª a sério), no nível, nas previsões nem na calibração;
só as mortes (a vida acabou) são sinal de limite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select

from ..analysis.fail_points import classify, progress, window_percentile
from ..storage import models as m

DEATH = ("pode_ser_morte", "morte_provavel")


def _load_map(store: Any, rel_path: str) -> dict[str, Any] | None:
    from ..beatmaps.parser import parse_osu

    p = Path(store.raw.raw_dir) / rel_path
    try:
        pb = parse_osu(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    if pb.mode != 0 or not pb.hit_objects:
        return None
    o = pb.hit_objects
    return {"t": np.array([h.time for h in o], dtype=np.float64), "x": np.array([h.x for h in o], dtype=np.float64), "y": np.array([h.y for h in o], dtype=np.float64),
            "slider": np.array([h.kind == "slider" for h in o]), "hp": pb.diff("HPDrainRate", 5.0), "cs": pb.diff("CircleSize", 5.0)}


def strain_position(strains: dict[str, Any] | None, t_frac: float) -> dict[str, float] | None:
    """Dificuldade (máx. entre aim e speed) no ponto `t_frac` (0-1) do mapa: percentil entre os troços do mapa e fração do pico. None sem strains."""
    if not strains or strains.get("unavailable") or not strains.get("skills"):
        return None
    by = {sk["name"]: sk["values"] for sk in strains["skills"] if sk.get("values")}
    aim, speed = by.get("aim") or [], by.get("speed") or []
    n = max(len(aim), len(speed))
    if n < 3:
        return None
    raw = np.array([max(aim[i] if i < len(aim) else 0.0, speed[i] if i < len(speed) else 0.0) for i in range(n)])
    if raw.max() <= 0:
        return None
    i = int(min(max(t_frac, 0.0), 0.999) * n)
    v = float(raw[max(0, i - 1):i + 2].max())  # troço onde caiu e vizinhos (a falha acontece perto do troço difícil, não exatamente nele)
    return {"pct": round(float((raw < v).mean() + 0.5 * (raw == v).mean()), 3), "of_peak": round(v / float(raw.max()), 3)}


def player_fails(store: Any, user_id: int, strains_loader=None) -> list[dict[str, Any]]:
    """Uma linha por falha do lazer com mapa disponível: classe (reinício/morte), progresso, instante, accuracy e (se houver) dificuldade no ponto da falha."""
    sc, bf = m.scores.c, m.beatmap_files.c
    with store.engine.connect() as c:
        fails = c.execute(select(sc.score_id, sc.beatmap_id, sc.accuracy, sc.statistics, sc.maximum_statistics, sc.mod_acronyms, sc.ended_at)
                          .where(sc.user_id == user_id, sc.passed == False, sc.legacy_score_id.is_(None), sc.rank == "F", sc.beatmap_id.isnot(None))  # noqa: E712
                          .order_by(sc.ended_at)).all()
        files = {int(b): rp for b, rp in c.execute(select(bf.beatmap_id, bf.rel_path))}
    maps: dict[int, dict[str, Any] | None] = {}
    rows: list[dict[str, Any]] = []
    for sid, bid, acc, stt, mx, mods, ended in fails:
        bid = int(bid)
        if bid not in maps:
            maps[bid] = _load_map(store, files[bid]) if bid in files else None
        mp = maps[bid]
        pr = progress(stt or {}, mx or {})
        if mp is None or pr is None:
            continue
        n_obj = len(mp["t"])
        n_j = int(round(pr * float((mx or {}).get("great") or n_obj)))
        idx = max(0, min(n_obj - 1, n_j - 1)) if n_j > 0 else 0
        t_fail = float(mp["t"][idx])
        mods_s = mods if isinstance(mods, str) else ",".join(mods or [])
        klass, ratio = classify(stt or {}, mp["hp"], mods_s)
        if klass in ("excluida", "sd_pf"):
            continue
        span = float(mp["t"][-1] - mp["t"][0]) or 1.0
        t_frac = (t_fail - float(mp["t"][0])) / span
        row = {"score_id": int(sid), "beatmap_id": bid, "when": str(ended)[:16], "mods": mods_s, "accuracy": float(acc or 0.0), "progress": round(float(pr), 3),
               "t_frac": round(t_frac, 3), "klass": klass, "kind": "morte" if klass in DEATH else "reinicio", "damage_ratio": None if ratio != ratio else round(float(ratio), 2),
               "miss": int((stt or {}).get("miss", 0) or 0), "pct_intensity": round(window_percentile(mp, t_fail, "intensity"), 3), "strain": None}
        if strains_loader is not None:
            row["strain"] = strain_position(strains_loader(bid, mods_s), t_frac)
        rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Resumo: reinícios vs mortes, onde cada tipo acontece (progresso, percentil de intensidade da janela e, quando há strains, dificuldade no ponto) e os mapas com mais mortes."""
    def med(v: list[float]) -> float | None:
        return round(float(np.median(v)), 3) if v else None

    out: dict[str, Any] = {"n": len(rows), "kinds": {}, "maps": []}
    for kind in ("reinicio", "morte"):
        rs = [r for r in rows if r["kind"] == kind]
        st = [r["strain"]["pct"] for r in rs if r.get("strain")]
        out["kinds"][kind] = {"n": len(rs), "progress_median": med([r["progress"] for r in rs]), "accuracy_median": med([r["accuracy"] for r in rs]),
                              "miss_median": med([r["miss"] for r in rs]), "pct_intensity_median": med([r["pct_intensity"] for r in rs]),
                              "strain_pct_median": med(st), "n_with_strain": len(st)}
    by: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["beatmap_id"], []).append(r)
    maps = []
    for bid, rs in by.items():
        deaths = [r for r in rs if r["kind"] == "morte"]
        maps.append({"beatmap_id": bid, "fails": len(rs), "deaths": len(deaths), "resets": len(rs) - len(deaths),
                     "death_at": med([r["t_frac"] for r in deaths]), "points": [{"t": r["t_frac"], "k": r["kind"]} for r in rs]})
    out["maps"] = sorted(maps, key=lambda x: (-x["deaths"], -x["fails"]))
    # hábito de reinício: NÃO é habilidade nem dificuldade (reiniciar pode ser perfeccionismo, bloqueio ou querer accuracy alta); só se descreve
    rs = [r for r in rows if r["kind"] == "reinicio"]
    out["reset_habit"] = {"n": len(rs), "maps": len({r["beatmap_id"] for r in rs}), "progress_median": med([r["progress"] for r in rs]),
                          "share_low_miss": round(sum(1 for r in rs if r["miss"] <= 2) / len(rs), 3) if rs else None,
                          "top_maps": [{"beatmap_id": x["beatmap_id"], "resets": x["resets"]} for x in sorted(maps, key=lambda x: -x["resets"])[:5] if x["resets"] >= 2]}
    return out


def reset_score_ids(rows: list[dict[str, Any]]) -> set[int]:
    """Ids dos scores classificados como reinício (para os testes/estatísticas os ignorarem)."""
    return {int(r["score_id"]) for r in rows if r["kind"] == "reinicio"}
