"""Modelo "accuracy esperada SE PASSAR" (a quantidade à Tillerino) para um par (jogador, mapa).

Diferença para os modelos `reach`: aqueles dão P(a melhor tentativa passa E chega a X %), onde uma falha conta como zero, e a "accuracy esperada" que se
tirava deles misturava a probabilidade de passar com a accuracy obtida. Aqui o alvo é só a accuracy **dos pares que passaram** (`best_acc`: a melhor
accuracy entre os passes do par), prevista com as mesmas variáveis do modelo A (atributos do mapa + perfil do jogador com metade dos mapas + distâncias).
A previsão é a **mediana** (`regression_l1`): metade dos passes ficam acima. Junta-se a P(passar) (`pass_model`) no recomendador:
P(passar) >= 80 % e accuracy esperada ao passar >= 88 % (ideal ~93 %).

Só há passes nos dumps (todos `passed = 1`), o que é exatamente o que este alvo pede. Limites: sem mods, sem forma do dia, "melhor passe do par"
(um pouco acima de uma jogada avulsa), jogadores de teste nunca vistos.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm

BANDS = ((0.0, 0.85), (0.85, 0.88), (0.88, 0.90), (0.90, 0.93), (0.93, 0.95), (0.95, 1.01))


def train_regressor(x_tr, y_tr, x_va, y_va, names, *, rounds: int, threads: int, seed: int, progress: Progress | None):
    import lightgbm as lgb

    params = {"objective": "regression_l1", "metric": "l1", "learning_rate": 0.08, "num_leaves": 127, "min_data_in_leaf": 200,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1,
              "seed": seed, "num_threads": threads}
    cbs = [lgb.early_stopping(40, verbose=False)]
    if progress:
        cbs.append(lambda env: progress.update(add=1))
    return lgb.train(params, lgb.Dataset(x_tr, y_tr, feature_name=names), num_boost_round=rounds,
                     valid_sets=[lgb.Dataset(x_va, y_va, feature_name=names)], callbacks=cbs)


def reg_metrics(y, p) -> dict[str, float]:
    import numpy as np

    y, p = np.asarray(y, dtype=np.float64), np.asarray(p, dtype=np.float64)
    err = p - y
    var = float(np.var(y))
    return {"n": int(len(y)), "mae": round(float(np.mean(np.abs(err))), 5), "rmse": round(float(np.sqrt(np.mean(err ** 2))), 5),
            "r2": round(1 - float(np.mean(err ** 2)) / var, 4) if var > 0 else 0.0, "bias": round(float(np.mean(err)), 5)}


def band_table(y, p) -> list[dict[str, Any]]:
    """Por escalão da accuracy PREVISTA: que accuracy tiveram de facto e que fração chegou a >= 88 % / >= 93 % (a mediana prevista >= 88 % deve dar >= 50 %)."""
    import numpy as np

    y, p = np.asarray(y), np.asarray(p)
    out = []
    for lo, hi in BANDS:
        m = (p >= lo) & (p < hi)
        if m.sum() >= 30:
            out.append({"predicted_range": [lo, min(hi, 1.0)], "n": int(m.sum()), "mean_predicted": round(float(p[m].mean()), 4),
                        "actual_median": round(float(np.median(y[m])), 4), "actual_mean": round(float(y[m].mean()), 4),
                        "share_actual_ge_88": round(float((y[m] >= 0.88).mean()), 3), "share_actual_ge_93": round(float((y[m] >= 0.93).mean()), 3)})
    return out


def run_acc_model(inputs: Path, out_dir: Path, version: str = "v1", *, rounds: int = 500, threads: int = 4, cap_rows: int = 800,
                  cap_train: int = 8_000_000, seed: int = 42, progress_path: Path | None = None, sample_pct: int = 100) -> dict[str, Any]:
    import numpy as np

    t0 = time.time()
    playcounts = sorted(inputs.glob("osu_user_beatmap_playcount_*.parquet"))
    scores = sorted(inputs.glob("dump_scores_*.parquet"))
    catalog = inputs / "map_attributes.parquet"
    if not playcounts or not scores or not catalog.exists():
        raise FileNotFoundError("faltam ficheiros em " + str(inputs))
    prog = Progress(progress_path, "Accuracy se passar — 1/3 a ler dados", 60 + rounds, "passos")
    cat_ids, cat_x = pm.load_catalog(catalog)
    users, bids, attempts, is_top = pm.load_pairs(playcounts, sample_pct=sample_pct)
    prog.update(10, label="Accuracy se passar — 1/3 a ler passes do dump", force=True)
    pkeys, ppp, pacc, pfl, pmax = pm.load_passes_ex(scores, sample_pct=sample_pct)
    prog.update(25, label="Accuracy se passar — 2/3 a construir perfis e linhas", force=True)
    rows = pm.build_rows(users, bids, attempts, is_top, pkeys, ppp, pacc, pfl, cat_ids, cat_x, cap_rows=cap_rows, pass_accmax=pmax)
    idx = np.nonzero(rows["y"] == 1)[0]  # só pares que passaram: o alvo é a accuracy desses passes
    names = pm.FEATURE_SETS["A"]
    x = pm._matrix({"x": rows["x"][idx], "b_extra": rows["b_extra"][idx]}, None, names)
    y = np.clip(rows["best_acc"][idx], 0.0, 1.0).astype(np.float32)
    user, top = rows["user"][idx], rows["top"][idx]
    del rows
    groups = pm.split_groups(user, seed)
    tr, va, te = (np.nonzero(groups == g)[0] for g in (0, 1, 2))
    if len(tr) > cap_train:
        tr = np.sort(np.random.default_rng(seed).choice(tr, cap_train, replace=False))
    va = va[:600_000]
    prog.update(45, label="Accuracy se passar — 3/3 a treinar", force=True)
    model = train_regressor(x[tr], y[tr], x[va], y[va], names, rounds=rounds, threads=threads, seed=seed, progress=prog)
    p = np.clip(model.predict(x[te]), 0.0, 1.0)
    yt = y[te]
    mean_col = len(pm.MAP_FEATS) + pm.PROFILE_FEATS.index("p_mean_acc")
    stars = x[te, 0]
    results: dict[str, Any] = {
        "model": reg_metrics(yt, p),
        "baselines": {"player_mean_accuracy": reg_metrics(yt, np.clip(x[te, mean_col], 0, 1)),
                      "global_median": reg_metrics(yt, np.full(len(te), float(np.median(y[tr]))))},
        "by_source": {"random": reg_metrics(yt[~top[te]], p[~top[te]]) if (~top[te]).any() else None,
                      "top": reg_metrics(yt[top[te]], p[top[te]]) if top[te].any() else None},
        "by_stars": {lab: reg_metrics(yt[m], p[m]) for lab, m in (("<3", stars < 3), ("3-5", (stars >= 3) & (stars < 5)), ("5-7", (stars >= 5) & (stars < 7)),
                                                                  (">=7", stars >= 7)) if m.sum() > 200},
        "by_predicted_band": band_table(yt, p)}
    imp = model.feature_importance(importance_type="gain")
    results["top_features_gain"] = sorted(zip(names, (imp / max(imp.sum(), 1e-9)).round(4).tolist()), key=lambda t: -t[1])[:10]
    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "data": {"passed_pairs": int(len(idx)), "players": int(len(set(user.tolist()))), "train_rows": int(len(tr)), "test_rows": int(len(te)),
                    "test_players": int(len(set(user[te].tolist()))), "target_median": round(float(np.median(y)), 4)},
           "results": results, "features": names,
           "notes": "alvo = melhor accuracy entre os passes do par (só pares que passaram); previsão = mediana; sem mods; jogadores de teste nunca vistos."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    model.save_model(str(target / "acc_pass_A.txt"))
    prog.finish()
    return out
