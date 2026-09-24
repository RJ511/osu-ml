"""Modelo "alcançável": o jogador chega a **≥ 88 %** (ou **≥ 93 %**) de accuracy neste mapa, na sua melhor jogada?

Definição do utilizador para "alcançável" (2026-09-24): chegar a pelo menos 88 % de accuracy, 93 % seria melhor. O alvo é por par
(jogador, mapa): `y = 1` se a MELHOR accuracy entre os passes do par é ≥ limiar; `y = 0` se nunca passou ou a melhor ficou abaixo.
Mesmo dataset e mesma disciplina anti-fuga de `pass_model.py` (perfil com metade dos mapas, alvos na outra; jogadores de teste nunca vistos).

**O que isto NÃO é**: "melhor jogada" é o melhor de todas as tentativas (o playcount dá o nº de tentativas mas não as accuracies
das falhadas), sem mods (atributos nomod) e sem saber a forma do dia. Por isso a tabela de fiabilidade separa por nº de tentativas.
O objetivo é responder "quão correta é a nossa assunção de que o jogador chega ao limiar?": calibração + precisão por escalão de confiança.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm

THRESHOLDS = {"acc88": 0.88, "acc93": 0.93}
CONF_LEVELS = (0.3, 0.5, 0.6, 0.7, 0.8, 0.9)


def confidence_table(y, p) -> list[dict[str, Any]]:
    """Para cada nível de confiança: que fração das linhas a atinge e quantas dessas realmente chegam ao limiar."""
    import numpy as np

    y, p = np.asarray(y), np.asarray(p)
    base = float(y.mean())
    out = []
    for lv in CONF_LEVELS:
        m = p >= lv
        if m.sum() < 50:
            continue
        obs = float(y[m].mean())
        out.append({"predicted_at_least": lv, "share_of_rows": round(float(m.mean()), 4), "observed_rate": round(obs, 4),
                    "lift_vs_base": round(obs / base, 2) if base else None, "n": int(m.sum())})
    return out


def evaluate_target(model, x, y, te, rows) -> dict[str, Any]:
    import numpy as np

    p = model.predict(x[te])
    yt = y[te]
    att, stars, top = rows["attempts"][te], rows["x"][te, 0], rows["top"][te]
    gap_stars = rows["x"][te, len(pm.MAP_FEATS) + len(pm.PROFILE_FEATS)]  # stars - P90 stars do jogador
    bins = (("muito_abaixo (<-1★)", gap_stars < -1), ("abaixo (-1..0★)", (gap_stars >= -1) & (gap_stars < 0)),
            ("ligeiramente_acima (0..+0,5★)", (gap_stars >= 0) & (gap_stars < 0.5)), ("acima (+0,5..+1★)", (gap_stars >= 0.5) & (gap_stars < 1)),
            ("muito_acima (>+1★)", gap_stars >= 1))
    return {"all": pm.metrics(yt, p), "calibration": pm.calibration(yt, p), "confidence": confidence_table(yt, p),
            "by_attempts": {lab: {**pm.metrics(yt[m], p[m]), "confidence": confidence_table(yt[m], p[m])} for lab, m in
                            (("1_tentativa", att == 1), ("2-4", (att >= 2) & (att <= 4)), ("5-19", (att >= 5) & (att < 20)), ("20+", att >= 20))
                            if m.sum() > 300},
            "by_source": {lab: pm.metrics(yt[m], p[m]) for lab, m in (("random", ~top), ("top", top)) if m.sum() > 300},
            "by_gap_to_player_p90_stars": {lab: {"n": int(m.sum()), "observed_rate": round(float(yt[m].mean()), 4),
                                                  "mean_pred": round(float(p[m].mean()), 4),
                                                  "auc": pm.metrics(yt[m], p[m])["auc"]} for lab, m in bins if m.sum() > 300},
            "by_stars": {lab: pm.metrics(yt[m], p[m]) for lab, m in
                         (("<3", stars < 3), ("3-5", (stars >= 3) & (stars < 5)), ("5-7", (stars >= 5) & (stars < 7)), (">=7", stars >= 7)) if m.sum() > 300}}


def run_reach_model(inputs: Path, out_dir: Path, version: str = "v1", *, rounds: int = 500, threads: int = 4, cap_rows: int = 800,
                    cap_train: int = 4_000_000, seed: int = 42, progress_path: Path | None = None, sample_pct: int = 100,
                    thresholds: dict[str, float] | None = None, only_a: bool = False) -> dict[str, Any]:
    import numpy as np

    t0 = time.time()
    thresholds = thresholds or THRESHOLDS
    playcounts = sorted(inputs.glob("osu_user_beatmap_playcount_*.parquet"))
    scores = sorted(inputs.glob("dump_scores_*.parquet"))
    catalog = inputs / "map_attributes.parquet"
    if not playcounts or not scores or not catalog.exists():
        raise FileNotFoundError("faltam ficheiros em " + str(inputs))
    prog = Progress(progress_path, "Alcançável (limiares de accuracy) — 1/3 a ler dados", 60 + len(thresholds) * (1 if only_a else 2) * rounds, "passos")
    cat_ids, cat_x = pm.load_catalog(catalog)
    users, bids, attempts, is_top = pm.load_pairs(playcounts, sample_pct=sample_pct)
    prog.update(10, label="Alcançável — 1/3 a ler passes", force=True)
    pkeys, ppp, pacc, pfl, pmax = pm.load_passes_ex(scores, sample_pct=sample_pct)
    prog.update(25, label="Alcançável — 2/3 a construir perfis e linhas", force=True)
    rows = pm.build_rows(users, bids, attempts, is_top, pkeys, ppp, pacc, pfl, cat_ids, cat_x, cap_rows=cap_rows, pass_accmax=pmax)
    groups = pm.split_groups(rows["user"], seed)
    idx = {g: np.nonzero(groups == g)[0] for g in (0, 1, 2)}
    tr = idx[0] if len(idx[0]) <= cap_train else np.sort(np.random.default_rng(seed).choice(idx[0], cap_train, replace=False))
    va = idx[1][:600_000]
    te = idx[2]
    targets = {"pass": rows["y"].astype(np.int8),
               **{name: ((rows["best_acc"] >= thr) & (rows["y"] == 1)).astype(np.int8) for name, thr in thresholds.items()}}
    results: dict[str, Any] = {}
    models = {}
    prog.update(45, label="Alcançável — 3/3 a treinar", force=True)
    for tname in thresholds:
        y = targets[tname]
        # estatística por mapa do PRÓPRIO alvo (leave-one-out no treino), como no modelo C de `pass_model`
        map_feat, _ = pm.add_map_features({"bid": rows["bid"], "y": y}, groups)
        res: dict[str, Any] = {"base_rate": round(float(y[te].mean()), 4), "base_rate_train": round(float(y[tr].mean()), 4)}
        for name in (("A",) if only_a else ("A", "C")):
            names = pm.FEATURE_SETS[name]
            x = pm._matrix(rows, map_feat, names)
            prog.update(label=f"Alcançável — a treinar {tname} / modelo {name}", force=True)
            model = pm.train_model(x[tr], y[tr], x[va], y[va], names, rounds=rounds, threads=threads, seed=seed, progress=prog)
            models[(tname, name)] = model
            res[name] = evaluate_target(model, x, y, te, rows)
            if name == "A":
                imp = model.feature_importance(importance_type="gain")
                res["A"]["top_features_gain"] = sorted(zip(names, (imp / imp.sum()).round(4).tolist()), key=lambda t: -t[1])[:10]
        res["baselines"] = {"player_mean_accuracy": pm.metrics(y[te], np.clip(rows["x"][te, len(pm.MAP_FEATS) + pm.PROFILE_FEATS.index("p_mean_acc")], 0, 1)),
                            "map_loo_rate": pm.metrics(y[te], map_feat[te, 0]),
                            "pass_target_reference": pm.metrics(targets["pass"][te], np.full(len(te), targets["pass"][tr].mean()))}
        results[tname] = res
    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "definition": {n: f"melhor accuracy do par >= {t:.2f}" for n, t in thresholds.items()},
           "data": {"pairs_with_catalog": int(len(rows["y"])), "players": int(len(set(rows["user"].tolist()))), "train_rows": int(len(tr)),
                    "test_rows": int(len(te)), "test_players": int(len(set(rows["user"][te].tolist()))),
                    "base_rate_pass": round(float(targets["pass"].mean()), 4)},
           "results": results,
           "notes": "Melhor jogada entre TODAS as tentativas, sem mods, sem forma do dia. Perfil com metade dos mapas, alvos na outra; jogadores de teste nunca vistos."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    for (tname, name), m in models.items():
        m.save_model(str(target / f"reach_{tname}_{name}.txt"))
    prog.finish()
    return out
