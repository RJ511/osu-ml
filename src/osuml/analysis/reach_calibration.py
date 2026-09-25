"""Calibração dos modelos "alcançável" para jogadores da API (scores da BD local, 0 pedidos).

Problema medido (`reach_api_check`): o modelo ordena bem mas **subestima** nos jogadores da API (previsto P(≥88 %) 0,25 -> observado 0,62). Os dumps
têm todas as tentativas (incluindo abandonos) e perfis com milhares de passes; a BD da API só tem best (≤ 200) + recentes. Quando o modelo "espera 84 %",
estes jogadores acabam com mediana de ~95 % nos passes e 66 % chegam a ≥ 88 %.

Correção: regressão logística de 2 parâmetros por limiar, `logit(p_cal) = a + b · logit(p_bruto)`, ajustada aos pares jogados **depois** do snapshot
(perfil só com passes anteriores). É monótona (não estraga a ordenação) e barata (2 números por limiar). Avalia-se por validação cruzada **por jogador**
(nenhum jogador aparece no ajuste e no teste) e guarda-se em `calibration.json` ao lado dos modelos.

Limites: 71 jogadores, ~2 900 pares; são mapas que o jogador escolheu jogar (viés de seleção: tendem a ser mais fáceis do que um mapa sugerido ao acaso).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm

THRESHOLDS = (0.85, 0.88, 0.90, 0.93, 0.95, 0.97)
EPS = 1e-4


def logit(p):
    import numpy as np

    p = np.clip(np.asarray(p, dtype=np.float64), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(z):
    import numpy as np

    return 1.0 / (1.0 + np.exp(-np.asarray(z, dtype=np.float64)))


def apply_calibration(p, params: dict[str, dict[str, float]] | None, thresholds=THRESHOLDS):
    """`p`: (n, len(thresholds)) probabilidades brutas -> calibradas (mesma forma). Sem parâmetros devolve `p` igual."""
    import numpy as np

    p = np.asarray(p, dtype=np.float64)
    if not params:
        return p
    out = p.copy()
    for j, t in enumerate(thresholds):
        c = params.get(f"acc{int(round(t * 100))}")
        if c:
            out[:, j] = sigmoid(c["a"] + c["b"] * logit(p[:, j]))
    return out


def fit_platt(y, p, lam: float = 0.5) -> tuple[float, float]:
    """Mínimos da log-verosimilhança negativa de `y ~ sigmoid(a + b·logit(p))`, com uma penalização fraca para (a=0, b=1) (identidade)."""
    import numpy as np
    from scipy.optimize import minimize

    y = np.asarray(y, dtype=np.float64)
    z = logit(p)

    def loss(w):
        q = np.clip(sigmoid(w[0] + w[1] * z), 1e-9, 1 - 1e-9)
        return -np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)) + lam / len(y) * ((w[0]) ** 2 + (w[1] - 1.0) ** 2)

    r = minimize(loss, x0=np.array([0.0, 1.0]), method="BFGS")
    return float(r.x[0]), float(r.x[1])


def ece(y, p, bins: int = 10) -> float:
    import numpy as np

    y, p = np.asarray(y, dtype=np.float64), np.asarray(p, dtype=np.float64)
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.any():
            tot += m.mean() * abs(float(y[m].mean()) - float(p[m].mean()))
    return tot


def collect_api_pairs(store, index_dir: Path, models_dir: Path, cutoff: str = "2026-09-01", progress: Progress | None = None):
    """(P bruto (n, 6), Y (n, 6), user_id (n,)) dos pares jogados depois de `cutoff` por jogadores da API; perfil só com passes anteriores."""
    import numpy as np
    from sqlalchemy import select

    from ..recommend.core import LightGbmPredictor, Recommender
    from ..storage import models as m

    rec = Recommender(store, index_dir, models_dir, predictor=LightGbmPredictor(models_dir, calibration=False))
    ok, why = rec.ready()
    if not ok:
        raise RuntimeError(why)
    rec._load()
    ids, x, pred = rec._index["ids"], rec._index["x"], rec._predictor
    cut = np.datetime64(cutoff)
    pidx = [pm.MAP_FEATS.index(a) for a in pm.PROF_ATTRS]
    with store.engine.connect() as c:
        rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.pp,
                                m.scores.c.mod_acronyms, m.scores.c.ended_at).where(m.scores.c.beatmap_id.isnot(None))).all()
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(int(r[0]), []).append(r)
    P, Y, U = [], [], []
    for n, (uid, plays) in enumerate(by.items(), 1):
        if progress:
            progress.update(n)
        bid = np.array([r[1] for r in plays], dtype=np.int64)
        pos = np.searchsorted(ids, bid)
        pos[pos >= len(ids)] = 0
        inc = ids[pos] == bid
        passed = np.array([bool(r[2]) for r in plays]) & inc
        acc = np.array([r[3] or 0.0 for r in plays], dtype=np.float32)
        pp = np.array([r[4] if r[4] is not None else np.nan for r in plays], dtype=np.float32)
        mods = [r[5] or "" for r in plays]
        fl = np.array([(1 if ("DT" in md or "NC" in md) else 0) | (2 if "HD" in md else 0) | (4 if "HR" in md else 0) for md in mods], dtype=np.uint8)
        end = np.array([np.datetime64(r[6]) if r[6] is not None else np.datetime64("NaT") for r in plays])
        before = passed & np.array([(not np.isnat(e)) and e < cut for e in end])
        after = np.array([(not np.isnat(e)) and e >= cut for e in end]) & inc
        if before.sum() < pm.MIN_PASSES or not after.any():
            continue
        res = pm.profile_vector(x[pos[before]][:, pidx], pp[before], acc[before], fl[before], int(before.sum()), 1.0)
        if res is None:
            continue
        prof = res[0]
        targ = sorted({int(b) for b in bid[after]})
        tpos = np.searchsorted(ids, np.array(targ, dtype=np.int64))
        feats = np.hstack([x[tpos], np.repeat(prof[None, :], len(tpos), axis=0), pm.gaps(x[tpos], prof)]).astype(np.float32)
        p = np.asarray(pred.predict(feats))
        for k, b in enumerate(targ):
            sel = after & passed & (bid == b)
            best = float(acc[sel].max()) if sel.any() else 0.0
            P.append(p[k])
            Y.append([1.0 if best >= t else 0.0 for t in THRESHOLDS])
            U.append(uid)
    return np.array(P), np.array(Y), np.array(U)


def run_reach_calibration(store, index_dir: Path, models_dir: Path, out_dir: Path, version: str = "v1", *, cutoff: str = "2026-09-01",
                          folds: int = 5, progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np

    prog = Progress(progress_path, "Calibração ≥88/93 % (jogadores da API)", 100, "jogadores")
    P, Y, U = collect_api_pairs(store, index_dir, models_dir, cutoff, prog)
    if len(P) < 300:
        raise RuntimeError(f"poucos pares fora do tempo ({len(P)}) para calibrar")
    params: dict[str, dict[str, float]] = {}
    cv: dict[str, Any] = {}
    fold = U % folds
    for j, t in enumerate(THRESHOLDS):
        name = f"acc{int(round(t * 100))}"
        a, b = fit_platt(Y[:, j], P[:, j])
        params[name] = {"a": round(a, 4), "b": round(b, 4)}
        cal_cv = np.zeros(len(P))
        for f in range(folds):  # ajusta sem os jogadores da dobra e prevê para eles: nunca vê o próprio jogador
            tr, te = fold != f, fold == f
            if te.any() and tr.any():
                af, bf = fit_platt(Y[tr, j], P[tr, j])
                cal_cv[te] = sigmoid(af + bf * logit(P[te, j]))
        cv[name] = {"base_rate": round(float(Y[:, j].mean()), 4), "mean_raw": round(float(P[:, j].mean()), 4), "mean_calibrated_cv": round(float(cal_cv.mean()), 4),
                    "ece_raw": round(ece(Y[:, j], P[:, j]), 4), "ece_calibrated_cv": round(ece(Y[:, j], cal_cv), 4),
                    "brier_raw": round(float(np.mean((P[:, j] - Y[:, j]) ** 2)), 4), "brier_calibrated_cv": round(float(np.mean((cal_cv - Y[:, j]) ** 2)), 4),
                    "reliability_calibrated_cv": [{"predicted": [lo, hi], "n": int(m.sum()), "mean_predicted": round(float(cal_cv[m].mean()), 3),
                                                   "observed": round(float(Y[m, j].mean()), 3)}
                                                  for lo, hi in ((0, .2), (.2, .4), (.4, .5), (.5, .6), (.6, .8), (.8, 1.01))
                                                  for m in [(cal_cv >= lo) & (cal_cv < hi)] if m.sum() >= 30]}
    out = {"created_at": datetime.now(timezone.utc).isoformat(), "cutoff": cutoff, "n_pairs": int(len(P)), "n_players": int(len(set(U.tolist()))),
           "thresholds": list(THRESHOLDS), "params": params, "cv_by_player": cv,
           "notes": "logit(p_cal) = a + b*logit(p_bruto), por limiar. Ajustado a pares jogados depois do snapshot por jogadores da API (viés: mapas escolhidos pelo jogador)."}
    target = Path(out_dir) / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    (Path(models_dir) / "calibration.json").write_text(json.dumps({k: out[k] for k in ("created_at", "cutoff", "n_pairs", "n_players", "thresholds", "params", "notes")},
                                                                    indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return out
