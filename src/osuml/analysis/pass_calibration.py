"""Validação e calibração de P(passar) e da accuracy esperada SE PASSAR com jogadores da API (scores da BD local, 0 pedidos).

Pares jogados **depois** do snapshot dos dumps por jogadores da API; o perfil usa só passes anteriores. Para cada par:
- `y_pass` = passou pelo menos uma vez (na janela); `p_pass` = P(passar) do modelo `pass_model_A`;
- se passou, `acc_real` = melhor accuracy dos passes; `acc_pred` = mediana prevista por `acc_pass_A`.

Saídas (`results.json`): AUC e tabela de fiabilidade de P(passar) (previsto vs observado), calibração logística `logit(p_cal) = a + b·logit(p_bruto)`
com validação cruzada por jogador, e o viés da accuracy prevista (deslocamento aditivo, também validado por jogador).
Limites: os mapas são os que o jogador escolheu jogar (viés de selecção); poucos jogadores (~70).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm
from .profile_form import ages_in_days, best_pass_per_map, build_profile
from .reach_calibration import ece, fit_platt, logit, sigmoid


def collect_pairs(store, index_dir: Path, models_dir: Path, cutoff: str = "2026-09-01", progress: Progress | None = None, profile_mode: str = "form"):
    """(p_pass bruto (n,), acc_pred (n,) ou NaN, y_pass (n,), acc_real (n,) ou NaN, user (n,), par_lazer (n,) bool).

    **Falhas só existem no lazer**: o stable não envia falhas, por isso quem joga no stable só tem passes na BD e a "taxa de passar" fica inflacionada (medido: 85 %
    contra ~60-65 % nos jogadores do lazer). `par_lazer` marca os pares que têm pelo menos uma tentativa do lazer depois do corte; `y_pass` conta só tentativas do lazer
    e a calibração de P(passar) usa apenas esses pares. A accuracy dos passes usa todos os passes (lazer e stable)."""
    import lightgbm as lgb
    import numpy as np
    from sqlalchemy import select

    from ..recommend.core import Recommender
    from ..storage import models as m

    models_dir = Path(models_dir)
    pass_m = lgb.Booster(model_file=str(models_dir / "pass_model_A.txt"))
    acc_f = models_dir / "acc_pass_A.txt"
    acc_m = lgb.Booster(model_file=str(acc_f)) if acc_f.exists() else None
    rec = Recommender(store, index_dir, models_dir, predictor=object())
    rec._load()
    ids, x, axis = rec._index["ids"], rec._index["x"], rec._index["axis"]
    cut = np.datetime64(cutoff)
    pidx = [pm.MAP_FEATS.index(a) for a in pm.PROF_ATTRS]
    with store.engine.connect() as c:
        rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.pp,
                                m.scores.c.mod_acronyms, m.scores.c.ended_at, m.scores.c.legacy_score_id).where(m.scores.c.beatmap_id.isnot(None))).all()
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(int(r[0]), []).append(r)
    P, A, Y, R, U, L = [], [], [], [], [], []
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
        lazer = np.array([r[7] is None for r in plays])
        before = passed & np.array([(not np.isnat(e)) and e < cut for e in end])
        after = np.array([(not np.isnat(e)) and e >= cut for e in end]) & inc
        if before.sum() < pm.MIN_PASSES or not after.any():
            continue
        keep = best_pass_per_map(pos, pp, before)
        res = build_profile(x[pos[keep]][:, pidx], axis[pos[keep]], pp[keep], acc[keep], fl[keep], ages_in_days(end, keep), int(before.sum()), 1.0,
                            mode=profile_mode)
        if res is None:
            continue
        prof = res[0]
        targ = sorted({int(b) for b in bid[after]})
        tpos = np.searchsorted(ids, np.array(targ, dtype=np.int64))
        feats = np.hstack([x[tpos], np.repeat(prof[None, :], len(tpos), axis=0), pm.gaps(x[tpos], prof)]).astype(np.float32)
        pp_ = pass_m.predict(feats)
        ap_ = np.clip(acc_m.predict(feats), 0, 1) if acc_m is not None else np.full(len(targ), np.nan)
        for k, b in enumerate(targ):
            sel = after & passed & (bid == b)
            sel_l = sel & lazer
            has_l = bool((after & lazer & (bid == b)).any())
            P.append(float(pp_[k])), A.append(float(ap_[k])), Y.append(1.0 if sel_l.any() else 0.0), L.append(has_l)
            R.append(float(acc[sel].max()) if sel.any() else float("nan")), U.append(uid)
    return np.array(P), np.array(A), np.array(Y), np.array(R), np.array(U), np.array(L, dtype=bool)


def reliability(y, p, edges=(0, .2, .4, .6, .7, .8, .9, 1.01)):
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() >= 30:
            out.append({"predicted_range": [lo, min(hi, 1.0)], "n": int(m.sum()), "mean_predicted": round(float(p[m].mean()), 3), "observed": round(float(y[m].mean()), 3)})
    return out


def run_pass_calibration(store, index_dir: Path, models_dir: Path, out_dir: Path, version: str = "v1", *, cutoff: str = "2026-09-01", folds: int = 5,
                         progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np

    from .pass_model import auc

    prog = Progress(progress_path, "Calibração P(passar) e accuracy (jogadores da API)", 100, "jogadores")
    P, A, Y, R, U, L = collect_pairs(store, index_dir, models_dir, cutoff, prog)
    Pl, Yl, Ul = P[L], Y[L], U[L]  # P(passar) só com pares do lazer (onde as falhas existem)
    if len(Pl) < 300:
        raise RuntimeError(f"poucos pares do lazer fora do tempo ({len(Pl)}) para calibrar")
    a, b = fit_platt(Yl, Pl)
    cal_cv = np.zeros(len(Pl))
    for f in range(folds):
        tr, te = (Ul % folds) != f, (Ul % folds) == f
        if te.any() and tr.any():
            af, bf = fit_platt(Yl[tr], Pl[tr])
            cal_cv[te] = sigmoid(af + bf * logit(Pl[te]))
    out: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(), "cutoff": cutoff, "n_pairs": int(len(P)), "n_players": int(len(set(U.tolist()))),
        "n_pairs_lazer": int(L.sum()), "n_players_lazer": int(len(set(Ul.tolist()))),
        "pass": {"params": {"a": round(a, 4), "b": round(b, 4)}, "base_rate": round(float(Yl.mean()), 4), "mean_raw": round(float(Pl.mean()), 4),
                 "auc": auc(Yl, Pl), "ece_raw": round(ece(Yl, Pl), 4), "ece_calibrated_cv": round(ece(Yl, cal_cv), 4),
                 "brier_raw": round(float(np.mean((Pl - Yl) ** 2)), 4), "brier_calibrated_cv": round(float(np.mean((cal_cv - Yl) ** 2)), 4),
                 "reliability_raw": reliability(Yl, Pl), "reliability_calibrated_cv": reliability(Yl, cal_cv)}}
    ok = ~np.isnan(A) & ~np.isnan(R)
    if ok.sum() >= 200:
        y_, p_, u_ = R[ok], A[ok], U[ok]
        shift = float(np.median(y_ - p_))
        cv_pred = np.zeros(len(y_))
        for f in range(folds):
            tr, te = (u_ % folds) != f, (u_ % folds) == f
            if te.any() and tr.any():
                cv_pred[te] = p_[te] + float(np.median(y_[tr] - p_[tr]))
        out["acc"] = {"n": int(ok.sum()), "shift": round(shift, 4), "mae_raw": round(float(np.mean(np.abs(p_ - y_))), 4),
                      "mae_shifted_cv": round(float(np.mean(np.abs(cv_pred - y_))), 4), "bias_raw": round(float(np.mean(p_ - y_)), 4),
                      "median_actual": round(float(np.median(y_)), 4), "median_predicted": round(float(np.median(p_)), 4),
                      "by_predicted_band": [{"predicted_range": [lo, min(hi, 1.0)], "n": int(m.sum()), "actual_median": round(float(np.median(y_[m])), 4),
                                             "share_actual_ge_88": round(float((y_[m] >= 0.88).mean()), 3), "share_actual_ge_93": round(float((y_[m] >= 0.93).mean()), 3)}
                                            for lo, hi in ((0, .85), (.85, .88), (.88, .90), (.90, .93), (.93, .95), (.95, 1.01))
                                            for m in [(p_ >= lo) & (p_ < hi)] if m.sum() >= 30]}
    target = Path(out_dir) / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    # ficheiro lido pelo recomendador (PassAccPredictor): logit(p_cal) = a + b·logit(p) e deslocamento aditivo da accuracy
    cal = {"created_at": out["created_at"], "cutoff": cutoff, "n_pairs": out["n_pairs"], "n_players": out["n_players"], "pass": out["pass"]["params"],
           "acc_shift": out.get("acc", {}).get("shift", 0.0),
           "notes": "calibrado com pares jogados por jogadores da API depois do snapshot (mapas escolhidos pelo jogador: tende a ser otimista)"}
    (Path(models_dir) / "calibration_pass_acc.json").write_text(json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return out
