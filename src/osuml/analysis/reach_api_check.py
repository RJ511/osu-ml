"""Validação dos modelos "alcançável" (P(melhor accuracy ≥ t)) com jogadores da API (scores da BD local, 0 pedidos).

Duas leituras, ambas honestas quanto aos limites:
- **metades** (como no treino): perfil com os passes de uma metade dos mapas (hash do beatmap_id), alvos na outra. Serve para ver a calibração
  nos jogadores acompanhados (PXD Vieira, gaaGOD, os 75 do painel). **Enviesado**: a BD só guarda os melhores scores (best ≤ 200) + recentes,
  por isso há muito mais passes bons do que "nunca passou/ficou abaixo" e o nº de passes do perfil é bem menor do que nos jogadores dos dumps.
- **temporal**: perfil só com passes anteriores a `cutoff` (snapshot do dump), alvos = mapas jogados depois. Poucos dados (só os `recent` do `poll`).

Resultado por limiar: n, taxa observada, previsão média, AUC e a taxa observada nas previsões mais altas.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm

THRESHOLDS = (0.85, 0.88, 0.90, 0.93, 0.95, 0.97)


def _summarize(y, p, thresholds) -> dict[str, Any]:
    import numpy as np

    out: dict[str, Any] = {}
    for j, t in enumerate(thresholds):
        yt = y[:, j]
        pj = p[:, j]
        if len(yt) < 30:
            continue
        top = pj >= np.quantile(pj, 0.75)
        out[f"acc{int(round(t * 100))}"] = {
            "n": int(len(yt)), "observed_rate": round(float(yt.mean()), 4), "mean_predicted": round(float(pj.mean()), 4),
            "auc": pm.metrics(yt, pj)["auc"], "observed_in_top_quartile_of_predictions": round(float(yt[top].mean()), 4) if top.any() else None,
            "observed_in_bottom_half": round(float(yt[pj <= np.median(pj)].mean()), 4),
            # fiabilidade: taxa observada por escalão de probabilidade prevista (para escolher o limiar de "alcançável" com dados)
            "reliability": [{"predicted_range": [lo, hi], "n": int(m.sum()), "mean_predicted": round(float(pj[m].mean()), 3), "observed": round(float(yt[m].mean()), 3)}
                            for lo, hi in ((0, .1), (.1, .2), (.2, .3), (.3, .4), (.4, .5), (.5, .7), (.7, 1.01))
                            for m in [(pj >= lo) & (pj < hi)] if m.sum() >= 20]}
    return out


def run_reach_api_check(store, index_dir: Path, models_dir: Path, out_dir: Path, version: str = "v1", *, cutoff: str = "2026-09-01",
                        seed: int = 42, progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np
    from sqlalchemy import select

    from ..recommend.core import Recommender
    from ..storage import models as m

    from ..recommend.core import LightGbmPredictor

    rec = Recommender(store, index_dir, models_dir, predictor=LightGbmPredictor(models_dir))
    ok, why = rec.ready()
    if not ok:
        raise RuntimeError(why)
    rec._load()
    ids, x, pred = rec._index["ids"], rec._index["x"], rec._predictor
    cut = np.datetime64(cutoff)
    with store.engine.connect() as c:
        names = dict(c.execute(select(m.users.c.user_id, m.users.c.username)).all())
        rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.pp,
                                m.scores.c.mod_acronyms, m.scores.c.ended_at, m.scores.c.first_source).where(m.scores.c.beatmap_id.isnot(None))).all()
    by_user: dict[int, list] = {}
    for r in rows:
        by_user.setdefault(int(r[0]), []).append(r)
    prog = Progress(progress_path, "Validação ≥88/93 % com jogadores da API", len(by_user), "jogadores")
    pidx = [pm.MAP_FEATS.index(a) for a in pm.PROF_ATTRS]

    def prep(plays):
        bid = np.array([r[1] for r in plays], dtype=np.int64)
        pos = np.searchsorted(ids, bid)
        pos[pos >= len(ids)] = 0
        inc = ids[pos] == bid
        passed = np.array([bool(r[2]) for r in plays]) & inc
        acc = np.array([r[3] if r[3] is not None else 0.0 for r in plays], dtype=np.float32)
        pp = np.array([r[4] if r[4] is not None else np.nan for r in plays], dtype=np.float32)
        mods = [r[5] or "" for r in plays]
        fl = np.array([(1 if ("DT" in md or "NC" in md) else 0) | (2 if "HD" in md else 0) | (4 if "HR" in md else 0) for md in mods], dtype=np.uint8)
        end = np.array([np.datetime64(r[6]) if r[6] is not None else np.datetime64("NaT") for r in plays])
        return bid, pos, inc, passed, acc, pp, fl, end

    def profile(sel, pos, pp, acc, fl, n_pairs):
        res = pm.profile_vector(x[pos[sel]][:, pidx], pp[sel], acc[sel], fl[sel], n_pairs, 1.0)
        return None if res is None else res[0]

    def predict(prof, tpos):
        mx = x[tpos]
        feats = np.hstack([mx, np.repeat(prof[None, :], len(tpos), axis=0), pm.gaps(mx, prof)]).astype(np.float32)
        return np.asarray(pred.predict(feats))

    def best_acc_by_map(bid, inc, passed, acc, mask):
        best: dict[int, float] = {}
        seen: set[int] = set()
        for i in np.nonzero(mask & inc)[0]:
            seen.add(int(bid[i]))
            if passed[i]:
                best[int(bid[i])] = max(best.get(int(bid[i]), 0.0), float(acc[i]))
        return seen, best

    half_y, half_p, half_recent, half_users = [], [], [], []
    temp_y, temp_p, temp_users = [], [], []
    per_player: dict[str, Any] = {}
    for n, (uid, plays) in enumerate(by_user.items(), 1):
        prog.update(n)
        bid, pos, inc, passed, acc, pp, fl, end = prep(plays)
        recent = np.array([r[7] == "recent" for r in plays])
        seen, best = best_acc_by_map(bid, inc, passed, acc, np.ones(len(plays), dtype=bool))
        if len(best) < pm.MIN_PASSES * 2:
            continue
        # --- metades (como no treino)
        half = pm._hash01(bid, seed, 2)
        ys, ps, rs = [], [], []
        for view in (0, 1):
            fsel = np.nonzero(passed & (half == view))[0]
            if len(fsel) < pm.MIN_PASSES:
                continue
            prof = profile(fsel, pos, pp, acc, fl, int(((half == view) & inc).sum()))
            if prof is None:
                continue
            targ = sorted({int(b) for b in seen if int(pm._hash01(np.array([b]), seed, 2)[0]) != view})
            if len(targ) < 5:
                continue
            tpos = np.searchsorted(ids, np.array(targ, dtype=np.int64))
            p = predict(prof, tpos)
            y = np.array([[1.0 if best.get(b, 0.0) >= t else 0.0 for t in THRESHOLDS] for b in targ])
            rec_maps = {int(bid[i]) for i in np.nonzero(recent)[0]}
            ys.append(y), ps.append(p), rs.append(np.array([b in rec_maps for b in targ]))
        if ys:
            Y, P, R = np.vstack(ys), np.vstack(ps), np.concatenate(rs)
            half_y.append(Y), half_p.append(P), half_recent.append(R), half_users.append(np.full(len(Y), uid))
            per_player[str(names.get(uid) or uid)] = {"n_pairs": int(len(Y)), "observed_88": round(float(Y[:, 1].mean()), 3), "mean_pred_88": round(float(P[:, 1].mean()), 3),
                                                     "observed_93": round(float(Y[:, 3].mean()), 3), "mean_pred_93": round(float(P[:, 3].mean()), 3)}
        # --- temporal
        before = passed & np.array([(not np.isnat(e)) and e < cut for e in end])
        after_mask = np.array([(not np.isnat(e)) and e >= cut for e in end])
        if before.sum() >= pm.MIN_PASSES and after_mask.any():
            seen_a, best_a = best_acc_by_map(bid, inc, passed, acc, after_mask)
            if seen_a:
                prof = profile(np.nonzero(before)[0], pos, pp, acc, fl, int(before.sum()))
                if prof is not None:
                    targ = sorted(seen_a)
                    p = predict(prof, np.searchsorted(ids, np.array(targ, dtype=np.int64)))
                    y = np.array([[1.0 if best_a.get(b, 0.0) >= t else 0.0 for t in THRESHOLDS] for b in targ])
                    temp_y.append(y), temp_p.append(p), temp_users.append(np.full(len(y), uid))
    res: dict[str, Any] = {"cutoff": cutoff, "thresholds": THRESHOLDS}
    if half_y:
        Y, P, R = np.vstack(half_y), np.vstack(half_p), np.concatenate(half_recent)
        res["halves_all_pairs"] = {"players": len(half_users), **_summarize(Y, P, THRESHOLDS)}
        res["halves_pairs_with_recent_play"] = _summarize(Y[R], P[R], THRESHOLDS) if R.sum() >= 30 else None
    if temp_y:
        Yt, Pt = np.vstack(temp_y), np.vstack(temp_p)
        res["temporal_after_cutoff"] = {"players": len(temp_users), **_summarize(Yt, Pt, THRESHOLDS)}
    res["per_player_halves"] = per_player
    res["notes"] = ("BD da API guarda best (≤200) + recent: pares 'nunca passou' são raros e o nº de passes do perfil é bem menor do que nos dumps. "
                    "Ler a previsão média vs taxa observada como enviesada e a AUC/ordenação como o sinal útil.")
    res["created_at"] = datetime.now(timezone.utc).isoformat()
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(res, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    prog.finish()
    return res
