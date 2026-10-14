"""Projeção da meta de pp a partir da evolução do nível por eixo (0 pedidos à API).

Cadeia de raciocínio, com as limitações à vista:
1. **Nível → pp**: nos jogadores acompanhados (perfil fiável) ajusta-se `ln(pp) = a + b·nível_médio`. Associação entre jogadores
   (não prova que subir 1 ponto dá `b`), mas é a única ponte medida com dados nossos.
2. **Ritmo de evolução**: inclinação robusta (Theil-Sen) do nível médio semanal do próprio jogador nas semanas com dados.
   Com poucas semanas ou em pausa, o ritmo é pouco fiável e a projeção diz isso em vez de inventar.
3. **Cenários** de dias de jogo por semana: o ritmo observado escala com `dias_cenário / dias_históricos`.
4. **Monte Carlo** (incerteza do ritmo e de `b`) → probabilidade de chegar à meta em 12/24/60 meses.
Também devolve o inverso: quantos pontos de nível (e por mês) são precisos para chegar à meta num dado prazo.
Não existe histórico de pp para modelar o crescimento de uma população: por isso não há "taxa típica de jogadores" aqui.
"""

from __future__ import annotations

import math
import random
from typing import Any

WEEKS_PER_MONTH = 52 / 12
MIN_FIT_PLAYERS = 12
MIN_SLOPE_POINTS = 6
SIMS = 4000
HORIZONS = (12, 24, 60)
WHAT_IF_PACES = (0.25, 0.5, 1.0, 2.0)  # pontos de nível médio por mês (cenários hipotéticos, sem probabilidade)


def fit_pp_vs_level(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """OLS de ln(pp) sobre o nível médio. `points` = [(nível_médio, pp)]. Devolve b, erro padrão de b, R², n, desvio dos resíduos."""
    pts = [(x, math.log(y)) for x, y in points if x is not None and y and y > 0]
    n = len(pts)
    if n < MIN_FIT_PLAYERS:
        return None
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    a = my - b * mx
    res = [y - (a + b * x) for x, y in pts]
    sse = sum(r * r for r in res)
    sst = sum((y - my) ** 2 for _, y in pts)
    sd = math.sqrt(sse / (n - 2))
    return {"b": b, "se_b": sd / math.sqrt(sxx), "r2": 1 - sse / sst if sst > 0 else 0.0, "n": n, "resid_sd": sd}


def theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """(inclinação mediana, erro padrão aproximado) das inclinações entre todos os pares. None com poucos pontos."""
    if len(xs) < MIN_SLOPE_POINTS:
        return None
    slopes = sorted((ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i])
    if not slopes:
        return None
    med = slopes[len(slopes) // 2]
    q1, q3 = slopes[len(slopes) // 4], slopes[(3 * len(slopes)) // 4]
    sigma = (q3 - q1) / 1.349  # desvio robusto das inclinações
    return med, max(sigma / math.sqrt(len(xs) / 2), 1e-6)


def mean_level_series(series: dict[str, list[dict[str, Any]]], axes: tuple[str, ...]) -> list[tuple[int, float]]:
    """[(índice da semana, nível médio dos eixos)] só nas semanas em que todos os eixos têm nível."""
    n = len(next(iter(series.values()))) if series else 0
    out = []
    for i in range(n):
        vals = [series[a][i]["level"] for a in axes if a in series]
        if len(vals) == len(axes) and all(v is not None for v in vals):
            out.append((i, sum(vals) / len(vals)))
    return out


def months_to_target(pp_now: float, target: float, b: float, slope_per_month: float) -> float:
    """Meses até chegar à meta com crescimento ln(pp) = b·ritmo·t. `inf` sem progresso."""
    rate = b * slope_per_month
    if pp_now >= target:
        return 0.0
    return math.log(target / pp_now) / rate if rate > 0 else math.inf


def project(pp_now: float | None, target_pp: float | None, fit: dict[str, Any] | None, series_pts: list[tuple[int, float]],
            days_hist: float | None, scenarios: tuple[int, ...] = (2, 3, 5), seed: int = 7, sims: int = SIMS) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False}
    if not pp_now or not target_pp:
        return {**out, "reason": "sem pp atual ou meta"}
    if fit is None:
        return {**out, "reason": "poucos jogadores acompanhados com perfil fiável para ligar nível a pp"}
    b, se_b = fit["b"], fit["se_b"]
    out["fit"] = {"b": round(b, 4), "se_b": round(se_b, 4), "r2": round(fit["r2"], 2), "n": fit["n"]}
    need = math.log(target_pp / pp_now) / b if pp_now < target_pp and b > 0 else 0.0
    out["needed"] = {"level_points": round(need, 1),
                     "per_month": {str(h): round(need / h, 2) for h in HORIZONS}}
    xs = [i for i, _ in series_pts]
    sl = theil_sen([float(x) for x in xs], [v for _, v in series_pts])
    if sl is None:
        return {**out, "ok": True, "observed": None, "scenarios": [],
                "reason": f"ritmo não medível: só {len(series_pts)} semana(s) com os 4 níveis (precisa de {MIN_SLOPE_POINTS})"}
    per_week, se_week = sl
    out["observed"] = {"per_month": round(per_week * WEEKS_PER_MONTH, 2), "se_month": round(se_week * WEEKS_PER_MONTH, 2),
                       "weeks_used": len(series_pts), "days_per_week": round(days_hist, 1) if days_hist else None}
    out["what_if"] = [{"per_month": r, "months": round(months_to_target(pp_now, target_pp, b, r), 1)} for r in WHAT_IF_PACES]
    if per_week <= 0:  # ritmo medido nulo ou negativo (pausa, estagnação): escalar por dias de jogo não faz sentido
        out.update({"ok": True, "scenarios": [], "stagnant": True})
        return out
    rng = random.Random(seed)
    sc = []
    for d in scenarios:
        scale = (d / days_hist) if days_hist else 1.0
        months = []
        for _ in range(sims):
            s = rng.gauss(per_week, se_week) * WEEKS_PER_MONTH * scale
            bb = rng.gauss(b, se_b)
            months.append(months_to_target(pp_now, target_pp, bb, s))
        finite = sorted(m for m in months if math.isfinite(m))
        row: dict[str, Any] = {"days": d, "per_month": round(per_week * WEEKS_PER_MONTH * scale, 2),
                               "p": {str(h): round(sum(1 for m in months if m <= h) / sims, 2) for h in HORIZONS}}
        row["median_months"] = round(finite[len(finite) // 2], 1) if len(finite) > sims / 2 else None  # só se mais de metade chega
        sc.append(row)
    out["scenarios"] = sc
    out["ok"] = True
    return out
