"""Perfil do jogador com **forma atual**: só passes, com peso por recência e por esforço (pp), sem deixar as jogadas fáceis mexerem muito no modelo.

Problema medido (PXD Vieira, jogadas de 24/09): o perfil usa os melhores passes de sempre e, num jogador que voltou depois de uma pausa, 185 dos 249
passes têm mais de 6 meses e são mais difíceis (mediana 147 pp) do que os recentes (mediana 113 pp; teto ~200). O perfil descreve o pico e não a forma:
nos mapas que o desafiam previa-se 95,8 % de accuracy e ele fez 91,3 %.

Pesos (só passes; um passe = um mapa, o melhor):
- **recência**: `w_rec = 0,25 + 0,75 · 0,5^(idade/45 dias)`, com a idade medida desde o último passe do próprio jogador (quem parou não fica todo "velho");
  o piso de 0,25 mantém o pico antigo como informação de fundo, mas com pouca voz.
- **esforço (pp)**: a gama atual do jogador vem dos seus passes mais recentes (`chão` = P25, `teto` = P95 dos últimos `recent_n`); passes fáceis (pp ≤ chão)
  pesam `easy_w = 0,5` (0,2 mediu pior) e o peso sobe linearmente até 1 a meio da gama (chão + ½·(teto − chão)). Jogadas "de 100 pp" (o esperado, normalmente fáceis) quase não
  mexem no perfil; as de 150+ pp (difíceis de alcançar) mandam.
- peso final = `w_rec · w_pp`. Sem passes recentes suficientes ou sem gama de pp, cai para o perfil normal (`base`).

As estatísticas do perfil (p50/p90 das características dos mapas, accuracy média, partilhas DT/HD/HR) passam a ser **ponderadas**; o "máx" usa só passes com peso
>= 0,22 (uma jogada fácil e recente não define o teto). O `k` (nº de passes) e a estrutura do vetor são os mesmos do perfil de treino, por isso o modelo é o mesmo.
"""

from __future__ import annotations

from typing import Any

from . import pass_model as pm

HALF_LIFE_DAYS = 45.0
REC_FLOOR = 0.25
EASY_W = 0.5  # medido: 0,2 (esforço forte) piorou a população (AUC 0,737); 0,5 iguala a recência pura e segue a ideia de não deixar as fáceis mandar
RECENT_N = 60
RECENT_DAYS = 90.0  # a gama de pp "atual" só conta passes dos últimos 90 dias (senão, com poucos recentes, entrariam passes antigos e mais difíceis)
MIN_RECENT = 15
MAX_W_MIN = 0.22
MODES = ("base", "recency", "form")


def weighted_quantile(v, w, q):
    import numpy as np

    v, w = np.asarray(v, dtype=np.float64), np.asarray(w, dtype=np.float64)
    o = np.argsort(v)
    v, w = v[o], w[o]
    c = np.cumsum(w) - 0.5 * w
    c = c / w.sum()
    return float(np.interp(q, c, v))


def form_weights(pp, age_days, *, recent_n: int | None = None, half_life: float | None = None, use_pp: bool = True) -> tuple[Any, dict[str, Any]]:
    """(pesos, info). `pp` pode ter NaN (mapas loved): recebem o peso de esforço a meio (0,6). Poucos passes recentes -> pesos 1."""
    import numpy as np

    recent_n = RECENT_N if recent_n is None else recent_n
    half_life = HALF_LIFE_DAYS if half_life is None else half_life
    pp = np.asarray(pp, dtype=np.float64)
    age = np.asarray(age_days, dtype=np.float64)
    n = len(pp)
    info: dict[str, Any] = {"mode": "base", "n": n}
    if n < MIN_RECENT:
        return np.ones(n), info
    rec = REC_FLOOR + (1 - REC_FLOOR) * 0.5 ** (age / half_life)
    w = rec.copy()
    info.update({"mode": "recency", "recency_weight_mean": round(float(rec.mean()), 3)})
    if use_pp:
        pool = np.nonzero(age <= RECENT_DAYS)[0]
        recent = pool[np.argsort(age[pool], kind="stable")][:recent_n]
        rpp = pp[recent][~np.isnan(pp[recent])]
        if len(rpp) >= MIN_RECENT and np.percentile(rpp, 95) - np.percentile(rpp, 25) >= 20:
            lo, hi = float(np.percentile(rpp, 25)), float(np.percentile(rpp, 95))
            mid = lo + 0.5 * (hi - lo)
            gate = EASY_W + (1 - EASY_W) * np.clip((np.nan_to_num(pp, nan=mid * 0.85) - lo) / max(mid - lo, 1e-9), 0.0, 1.0)
            w = rec * gate
            info.update({"mode": "form", "pp_floor": round(lo), "pp_ceiling": round(hi), "pp_mid": round(mid)})
    return w, info


def _weighted_profile(attrs, acc, flags, k, w, order):
    import numpy as np

    a = attrs[order].astype(np.float64)
    ww = w[order]
    big = ww >= MAX_W_MIN
    cols = []
    for j in range(a.shape[1]):
        cols += [weighted_quantile(a[:, j], ww, 0.5), weighted_quantile(a[:, j], ww, 0.9), float(a[big, j].max() if big.any() else a[:, j].max())]
    fl = flags[order]
    tot = ww.sum()
    accw = float(np.sum(np.nan_to_num(acc[order], nan=0.0) * ww) / tot)
    shares = [float(np.sum(((fl >> s) & 1) * ww) / tot) for s in (0, 1, 2)]
    return np.array(cols + [accw, float(k)] + shares, dtype=np.float32)


def build_profile(attrs5, axis5, pp, acc, flags, age_days, n_pairs: int, mean_log_attempts: float = 1.0, mode: str = "form"):
    """(perfil (vetor de `pm.PROFILE_FEATS`), b_extra, níveis por eixo (P90), info). Passes = 1 por mapa. `axis5`: notas por eixo (aim, speed, stamina, reading, stars).

    mode `base`: igual ao treino (top 200 por pp, sem pesos); `recency`: só recência; `form`: recência + esforço (pp)."""
    import numpy as np

    k = len(pp)
    if k < pm.MIN_PASSES:
        return None
    use_w = mode in ("recency", "form")
    w, info = form_weights(pp, age_days, use_pp=(mode == "form")) if use_w else (np.ones(k), {"mode": "base", "n": k})
    if mode == "base" or info["mode"] == "base":
        order = np.argsort(-np.nan_to_num(pp, nan=-1.0), kind="stable")[:pm.TOP_PASSES]
        prof, bx = pm.profile_vector(attrs5, pp, acc, flags, n_pairs, mean_log_attempts)
        wl = np.ones(len(order))
    else:
        eff = np.nan_to_num(pp, nan=-1.0) * (REC_FLOOR + (1 - REC_FLOOR) * 0.5 ** (np.asarray(age_days, dtype=np.float64) / HALF_LIFE_DAYS))
        order = np.argsort(-eff, kind="stable")[:pm.TOP_PASSES]
        prof = _weighted_profile(attrs5, acc, flags, k, w, order)
        bx = np.array([k / max(n_pairs, 1), mean_log_attempts], dtype=np.float32)
        wl = w[order]
    if mode == "base" or info["mode"] == "base":  # igual ao recomendador antigo (percentil linear do top 200)
        levels = [float(np.percentile(axis5[order][:, j], 90)) for j in range(axis5.shape[1])]
    else:
        levels = [weighted_quantile(axis5[order][:, j], wl, 0.9) for j in range(axis5.shape[1])]
    return prof.astype(np.float32), bx, levels, info


def best_pass_per_map(map_ids, pp, mask):
    """Índices (dentro de `mask`) do passe de maior pp de cada mapa: o perfil de treino também usa 1 linha por par (jogador, mapa)."""
    import numpy as np

    best: dict[int, int] = {}
    pp_ = np.nan_to_num(np.asarray(pp, dtype=np.float64), nan=-1.0)
    for i in np.nonzero(mask)[0]:
        j = int(map_ids[i])
        if j not in best or pp_[i] > pp_[best[j]]:
            best[j] = i
    return np.array(sorted(best.values()), dtype=np.int64)


def ages_in_days(ended_at, idx):
    """Idade (dias) de cada passe em `idx` desde o último passe do próprio jogador (quem parou não fica todo 'velho'). NaT conta como idade 0."""
    import numpy as np

    e = np.asarray(ended_at)[idx]
    ok = ~np.isnat(e)
    if not ok.any():
        return np.zeros(len(idx))
    ref = e[ok].max()
    age = np.zeros(len(idx))
    age[ok] = (ref - e[ok]).astype("timedelta64[D]").astype(float)
    return age
