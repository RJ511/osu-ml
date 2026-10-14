"""Validação e ajuste do DT com jogadas REAIS de jogadores da API (BD local, 0 pedidos).

Os modelos P(passar) e accuracy-se-passar foram treinados com atributos nomod. Para o DT usam-se as features efetivas (`recommend/modsview.py`).
Aqui mede-se o quão bem isso funciona e ajusta-se: para cada jogador da API, o perfil é o de **antes** de `as_of` e avalia-se nas tentativas lazer
**só com DT** depois dessa data (o lazer é o único regime com falhas). Compara-se a vista DT com a vista nomod (as mesmas jogadas, features nomod) para ver se corrigir
as features ajuda. O ajuste global (logit `a + b·logit p` e deslocamento da accuracy) é validado com validação cruzada **por jogador**.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AS_OF = datetime(2026, 9, 17)
MIN_PAIRS = 150


def _auc(y, s) -> float | None:
    import numpy as np

    y = np.asarray(y, dtype=bool)
    if y.all() or (~y).all():
        return None
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    # empates: rank médio
    vals, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    if len(vals) < len(s):
        sums = np.bincount(inv, weights=ranks)
        ranks = (sums / cnt)[inv]
    n1, n0 = y.sum(), (~y).sum()
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _logit(p):
    import numpy as np

    q = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(q / (1 - q))


def fit_platt(p, y):
    """(a, b) de logit(p') = a + b·logit(p) por máxima verosimilhança."""
    import numpy as np
    from scipy.optimize import minimize

    z = _logit(np.asarray(p, dtype=np.float64))
    y = np.asarray(y, dtype=np.float64)

    def nll(w):
        t = w[0] + w[1] * z
        return float(np.sum(np.logaddexp(0, t) - y * t)) + 1e-3 * (w[1] - 1.0) ** 2  # ridge mínimo para a inclinação

    r = minimize(nll, np.array([0.0, 1.0]), method="L-BFGS-B")
    return float(r.x[0]), float(r.x[1])


def apply_platt(p, a, b):
    import numpy as np

    return 1.0 / (1.0 + np.exp(-(a + b * _logit(np.asarray(p, dtype=np.float64)))))


def ece(p, y, bins: int = 10) -> float:
    import numpy as np

    p, y = np.asarray(p), np.asarray(y, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.any():
            tot += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(tot)


def collect_pairs(store, rec, mods: str = "DT", as_of: datetime = AS_OF) -> dict[str, Any]:
    """Pares (jogador, mapa) com tentativas só-DT depois de `as_of`, com previsões (vista DT e vista nomod) e resultado."""
    import numpy as np
    from sqlalchemy import select

    from ..analysis import pass_model as pm
    from ..storage import models as m
    from .. import __name__ as _pkg  # noqa: F401

    rec._load()
    view = rec._mods_view(mods)
    if view is None:
        raise RuntimeError("sem vista " + mods)
    ids, x_nm = rec._index["ids"], rec._index["x"]
    with store.engine.connect() as c:
        rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.mod_acronyms,
                                m.scores.c.legacy_score_id, m.scores.c.ended_at)
                         .where(m.scores.c.beatmap_id.isnot(None), m.scores.c.ended_at >= as_of)).all()
    by_user: dict[int, dict[int, dict[str, Any]]] = {}
    for uid, bid, passed, acc, mod, legacy, ended in rows:
        from ..recommend.modsview import stable_key

        if stable_key(mod) != mods:
            continue
        pos = int(np.searchsorted(ids, bid))
        if pos >= len(ids) or ids[pos] != bid or not view["valid"][pos]:
            continue
        d = by_user.setdefault(int(uid), {}).setdefault(pos, {"att": 0, "pass": False, "best": None})
        if legacy is None:  # só o lazer regista falhas
            d["att"] += 1
            d["pass"] = d["pass"] or bool(passed)
        if passed and acc is not None:
            d["best"] = max(d["best"] or 0.0, float(acc))
    recs: list[dict[str, Any]] = []
    for uid, maps in by_user.items():
        try:
            pdata = rec.load_player(uid, as_of=as_of)
        except ValueError:
            continue  # sem passes suficientes antes de as_of
        pos = np.array(sorted(maps), dtype=np.int64)
        outs = {}
        for name, x in (("dt", view["x"]), ("nm", x_nm)):
            xs = x[pos]
            feats = np.hstack([xs, np.repeat(pdata.profile[None, :], len(xs), axis=0), pm.gaps(xs, pdata.profile)]).astype(np.float32)
            outs[name] = rec._predictor.predict_both(feats)
        for i, p in enumerate(pos):
            d = maps[int(p)]
            recs.append({"user": uid, "pos": int(p), "att": d["att"], "pass": d["pass"], "best": d["best"],
                         "p_dt": float(outs["dt"]["p_pass"][i]), "acc_dt": float(outs["dt"]["acc"][i]),
                         "p_nm": float(outs["nm"]["p_pass"][i]), "acc_nm": float(outs["nm"]["acc"][i])})
    return {"pairs": recs}


def run_dt_check(store, rec, models_dir: Path, *, mods: str = "DT", as_of: datetime = AS_OF, apply: bool = False, folds: int = 5) -> dict[str, Any]:
    import numpy as np

    pairs = collect_pairs(store, rec, mods, as_of)["pairs"]
    lz = [r for r in pairs if r["att"] > 0]
    out: dict[str, Any] = {"mods": mods, "as_of": as_of.isoformat(), "n_pairs": len(lz), "n_players": len({r["user"] for r in lz}),
                           "n_attempts": int(sum(r["att"] for r in lz))}
    if len(lz) < MIN_PAIRS:
        out["error"] = f"poucos pares ({len(lz)} < {MIN_PAIRS}): sem ajuste"
        return out
    y = np.array([r["pass"] for r in lz], dtype=float)
    p_dt, p_nm = np.array([r["p_dt"] for r in lz]), np.array([r["p_nm"] for r in lz])
    users = np.array([r["user"] for r in lz])
    out["base_rate_pass"] = round(float(y.mean()), 3)
    out["predicted_mean_dt_view"] = round(float(p_dt.mean()), 3)
    out["predicted_mean_nomod_view"] = round(float(p_nm.mean()), 3)
    out["auc_dt_view"] = _auc(y, p_dt)
    out["auc_nomod_view"] = _auc(y, p_nm)
    out["ece_dt_view"], out["ece_nomod_view"] = round(ece(p_dt, y), 4), round(ece(p_nm, y), 4)
    a, b = fit_platt(p_dt, y)
    # validação cruzada por jogador
    uniq = np.unique(users)
    rng = np.random.default_rng(42)
    fold = dict(zip(uniq, rng.integers(0, folds, size=len(uniq))))
    f = np.array([fold[u] for u in users])
    cv = np.zeros(len(y))
    for k in range(folds):
        tr, te = f != k, f == k
        if te.any() and tr.any():
            ak, bk = fit_platt(p_dt[tr], y[tr])
            cv[te] = apply_platt(p_dt[te], ak, bk)
    out["pass"] = {"a": round(a, 4), "b": round(b, 4)}
    out["auc_after_cv"] = _auc(y, cv)
    out["brier_before"] = round(float(np.mean((p_dt - y) ** 2)), 4)
    out["brier_after_cv"] = round(float(np.mean((cv - y) ** 2)), 4)
    out["ece_after_cv"] = round(ece(cv, y), 4)
    # accuracy se passar (todos os passes DT, lazer + stable)
    ac = [r for r in pairs if r["best"] is not None]
    if ac:
        d_dt = np.array([r["best"] - r["acc_dt"] for r in ac])
        d_nm = np.array([r["best"] - r["acc_nm"] for r in ac])
        shift_dt, shift_nm = float(np.median(d_dt)), float(np.median(d_nm))
        mae_dt, mae_nm = float(np.mean(np.abs(d_dt - shift_dt))), float(np.mean(np.abs(d_nm - shift_nm)))
        out["acc"] = {"n": len(ac), "pred_dt_mean": round(float(np.mean([r["acc_dt"] for r in ac])), 4), "pred_nomod_mean": round(float(np.mean([r["acc_nm"] for r in ac])), 4),
                      "actual_mean": round(float(np.mean([r["best"] for r in ac])), 4),
                      "dt_view": {"bias_median_pts": round(shift_dt * 100, 2), "mae_raw_pts": round(float(np.mean(np.abs(d_dt))) * 100, 2), "mae_after_shift_pts": round(mae_dt * 100, 2)},
                      "nomod_view": {"bias_median_pts": round(shift_nm * 100, 2), "mae_raw_pts": round(float(np.mean(np.abs(d_nm))) * 100, 2), "mae_after_shift_pts": round(mae_nm * 100, 2)}}
        # escolhe a vista que dá o menor erro depois do deslocamento (decisão guiada pelos dados; n pequeno: ver o resultado)
        out["acc_source"] = "dt" if mae_dt <= mae_nm else "nomod"
        out["acc_shift"] = round(shift_dt if out["acc_source"] == "dt" else shift_nm, 4)
    else:
        out["acc_shift"] = 0.0
        out["acc_source"] = "dt"
    out["created_at"] = datetime.now(timezone.utc).isoformat()
    out["calibration_reliable"] = bool(out.get("auc_dt_view") is not None and out["ece_after_cv"] < out["ece_dt_view"])  # só se a validação cruzada por jogador melhora a calibração
    if apply and out.get("auc_dt_view") is not None and not out["calibration_reliable"]:
        out["applied"] = False
        out["not_applied_reason"] = "a validação cruzada não melhora a calibração (poucos dados/pouca informação): fica sem ajuste para estes mods"
    elif apply and out.get("auc_dt_view") is not None:
        cal = {k: out[k] for k in ("mods", "as_of", "n_pairs", "n_players", "n_attempts", "pass", "acc_shift", "acc_source", "created_at")}
        cal["auc_before"], cal["auc_after"] = out["auc_dt_view"], out["auc_after_cv"]
        fname = models_dir / f"calibration_{mods.lower().replace(',', '_')}.json"
        fname.write_text(json.dumps(cal, indent=2), encoding="utf-8")
        out["applied"] = str(fname)
    return out
