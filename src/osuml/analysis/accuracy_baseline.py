"""Baseline de **accuracy esperada** (dos passes) a partir dos scores dos dumps + catálogo de mapas.

Pergunta: dado um jogador (o que ele fez ANTES) e um mapa+mods, que accuracy fará? Desenho com split temporal
e sem fuga de informação:
- **Janela de histórico** `[hist_start, cutoff)` → só serve para calcular as características do jogador
  (accuracy média/desvio e P90 da dificuldade jogada: ★, aim, speed, reading, densidade);
- **Treino** = scores em `[cutoff, split)`; **Teste** = scores em `[split, fim)`. As características do jogador
  são as mesmas nos dois (calculadas só com o passado), por isso nada do que se prevê entra nas features.
- Limites por jogador (`per_player`) e reservoir sampling → nenhum jogador domina e a memória fica baixa
  (colunas em `array('f')`, não tuplos Python).
- Só jogadores com ≥ `min_hist` scores no histórico. 20 % dos jogadores ficam **fora do treino** (hold-out) e o teste é
  reportado para jogadores vistos e nunca vistos; jogadores sem histórico continuam por cobrir.
- O dump só tem **passes** (accuracy condicionada a passar), e a accuracy legacy vs lazer não é comparável (`is_legacy` é feature).
Modelos: média global, média do jogador, LightGBM só com o mapa, LightGBM completo. Métricas: MAE, RMSE, R².
"""

from __future__ import annotations

import json
import math
import random
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..beatmaps.catalog import attr_mods
from ..progress import Progress

MAP_COLS = ["stars", "aim", "speed", "reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_ms"]
MOD_FLAGS = ["DT", "HT", "HR", "EZ", "HD", "FL"]
HIST_COLS = ["stars", "aim", "speed", "reading", "density", "acc"]
FEATURES_MAP = ["stars", "aim", "speed", "log_reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_s",
                "is_legacy", *[f"mod_{m}" for m in MOD_FLAGS]]
FEATURES_PLAYER = ["p_n_hist", "p_mean_acc", "p_std_acc", "p_p90_stars", "p_p90_aim", "p_p90_speed", "p_p90_log_reading",
                   "p_p90_density"]
FEATURES_GAP = ["gap_stars", "gap_aim", "gap_speed", "gap_log_reading", "gap_density"]
ROW_COLS = ["user_id", "acc", *MAP_COLS, "is_legacy", *MOD_FLAGS]


def load_catalog(path: Path) -> dict[tuple[int, str], tuple[float, ...]]:
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=["beatmap_id", "mods", *MAP_COLS])
    ids, mods = t.column("beatmap_id").to_pylist(), t.column("mods").to_pylist()
    cols = [t.column(c).to_pylist() for c in MAP_COLS]
    return {(b, m): tuple(float(x) if x is not None else float("nan") for x in vals)
            for b, m, *vals in zip(ids, mods, *cols)}


class _Reservoir:
    """Reservoir por jogador sobre colunas `array('f')` partilhadas (índices por jogador)."""

    def __init__(self, ncols: int, cap: int, rng: random.Random, int_first: bool = False) -> None:
        # a 1.ª coluna pode ser o user_id em int64 (em float32 perdia-se precisão: ids > 2^24)
        self.cols = [array("q" if (int_first and i == 0) else "f") for i in range(ncols)]
        self.cap, self.rng = cap, rng
        self.idx: dict[int, list[int]] = {}
        self.seen: dict[int, int] = {}

    def add(self, uid: int, row: tuple[float, ...]) -> None:
        n = self.seen.get(uid, 0)
        self.seen[uid] = n + 1
        slots = self.idx.setdefault(uid, [])
        if len(slots) < self.cap:
            slots.append(len(self.cols[0]))
            for c, v in zip(self.cols, row):
                c.append(int(v) if c.typecode == "q" else v)
            return
        j = self.rng.randint(0, n)
        if j < self.cap:
            at = slots[j]
            for c, v in zip(self.cols, row):
                c[at] = int(v) if c.typecode == "q" else v

    def __len__(self) -> int:
        return len(self.cols[0])


def scan(score_files: list[Path], catalog: dict, *, hist_start: str, cutoff: str, split: str, per_player: int,
         hist_cap: int, seed: int, progress: Progress) -> tuple[_Reservoir, _Reservoir, _Reservoir, dict[str, int]]:
    import numpy as np
    import pyarrow.parquet as pq

    rng = random.Random(seed)
    hist_res = _Reservoir(len(HIST_COLS), hist_cap, rng)  # user_id fica no índice `idx`
    train = _Reservoir(len(ROW_COLS), per_player, rng, int_first=True)
    test = _Reservoir(len(ROW_COLS), per_player, rng, int_first=True)
    t_hist, t_cut, t_split = (np.datetime64(x) for x in (hist_start, cutoff, split))
    cache: dict[str, tuple[str, tuple[float, ...]]] = {}
    stats = {"rows_read": 0, "rows_in_window": 0, "no_catalog": 0, "history_rows": 0, "train_seen": 0, "test_seen": 0}
    for f in score_files:
        for batch in pq.ParquetFile(f).iter_batches(
                batch_size=250_000, columns=["user_id", "beatmap_id", "accuracy", "mods_effective", "is_legacy", "ended_at", "speed_change"]):
            ended = batch.column("ended_at").to_numpy(zero_copy_only=False).astype("datetime64[s]")
            sel = np.nonzero(ended >= t_hist)[0]
            stats["rows_read"] += batch.num_rows
            if len(sel):
                uid = batch.column("user_id").to_numpy(zero_copy_only=False)[sel]
                bid = batch.column("beatmap_id").to_numpy(zero_copy_only=False)[sel]
                acc = batch.column("accuracy").to_numpy(zero_copy_only=False)[sel]
                leg = batch.column("is_legacy").to_numpy(zero_copy_only=False)[sel]
                spd = batch.column("speed_change").to_numpy(zero_copy_only=False).astype(float)[sel]
                mods = batch.column("mods_effective").to_pylist()
                when = ended[sel]
                for k, i in enumerate(sel):
                    if not math.isnan(spd[k]):
                        continue
                    stats["rows_in_window"] += 1
                    hit = cache.get(mods[i])
                    if hit is None:
                        am = attr_mods(mods[i])
                        hit = cache[mods[i]] = (am, tuple(1.0 if x in am.split(",") else 0.0 for x in MOD_FLAGS))
                    m, flags = hit
                    attrs = catalog.get((int(bid[k]), m))
                    if attrs is None:
                        stats["no_catalog"] += 1
                        continue
                    u, a = int(uid[k]), float(acc[k])
                    if when[k] < t_cut:
                        stats["history_rows"] += 1
                        hist_res.add(u, (attrs[0], attrs[1], attrs[2], attrs[3], attrs[4], a))
                    else:
                        row = (u, a, *attrs, 1.0 if leg[k] else 0.0, *flags)
                        if when[k] < t_split:
                            stats["train_seen"] += 1
                            train.add(u, row)
                        else:
                            stats["test_seen"] += 1
                            test.add(u, row)
            progress.update(add=batch.num_rows)
    return hist_res, train, test, stats


def player_features(hist: _Reservoir, min_hist: int) -> dict[int, list[float]]:
    import numpy as np

    cols = [np.frombuffer(c, dtype=np.float32) for c in hist.cols]
    out = {}
    for u, idx in hist.idx.items():
        if len(idx) < min_hist:
            continue
        stars, aim, speed, reading, dens, acc = (c[idx].astype(float) for c in cols)
        out[u] = [float(len(idx)), float(acc.mean()), float(acc.std()), float(np.nanpercentile(stars, 90)),
                  float(np.nanpercentile(aim, 90)), float(np.nanpercentile(speed, 90)),
                  float(np.nanpercentile(np.log1p(np.clip(reading, 0, 1000)), 90)), float(np.nanpercentile(dens, 90))]
    return out


def assemble(res: _Reservoir, pf: dict[int, list[float]]):
    import numpy as np

    uid = np.frombuffer(res.cols[0], dtype=np.int64)
    rest = np.column_stack([np.frombuffer(c, dtype=np.float32) for c in res.cols[1:]])
    keep = np.isin(uid, np.fromiter(pf, dtype=np.int64))
    uid, rest = uid[keep], rest[keep]
    if not len(uid):
        z = np.zeros
        return (z((0, len(FEATURES_MAP)), np.float32), z((0, len(FEATURES_PLAYER)), np.float32),
                z((0, len(FEATURES_GAP)), np.float32), z(0, np.float32), uid)
    c = {n: rest[:, i] for i, n in enumerate(ROW_COLS[1:])}
    uniq, inv = np.unique(uid, return_inverse=True)
    pfx = np.array([pf[int(u)] for u in uniq], dtype=np.float32)[inv]
    lr = np.log1p(np.clip(c["reading"], 0, 1000))
    x_map = np.column_stack([c["stars"], c["aim"], c["speed"], lr, c["density"], c["ar"], c["cs"], c["od"], c["hp"],
                             c["n_objects"], c["length_ms"] / 1000.0, c["is_legacy"], *[c[m] for m in MOD_FLAGS]]).astype(np.float32)
    gaps = np.column_stack([c["stars"] - pfx[:, 3], c["aim"] - pfx[:, 4], c["speed"] - pfx[:, 5], lr - pfx[:, 6],
                            c["density"] - pfx[:, 7]]).astype(np.float32)
    return x_map, pfx, gaps, c["acc"].astype(np.float32), uid


def metrics(y, p) -> dict[str, float]:
    import numpy as np

    err = p - y
    ss = float(((y - y.mean()) ** 2).sum())
    return {"mae": round(float(np.abs(err).mean()), 5), "rmse": round(float(np.sqrt((err ** 2).mean())), 5),
            "r2": round(1 - float((err ** 2).sum()) / ss, 4) if ss else 0.0}


def is_holdout(uid, pct: int = 20, seed: int = 42):
    """Jogadores de hold-out (determinístico por id): o modelo nunca os vê no treino."""
    import numpy as np

    return ((uid.astype(np.int64) * 2654435761 + seed) % 100) < pct


def run_baseline(score_files: list[Path], catalog_path: Path, out_dir: Path, version: str = "v1", *,
                 hist_start: str = "2023-01-01", cutoff: str = "2025-01-01", split: str = "2025-07-01",
                 per_player: int = 100, hist_cap: int = 300, min_hist: int = 20, rounds: int = 300, seed: int = 42,
                 holdout_pct: int = 20, progress_path: Path | None = None) -> dict[str, Any]:
    import lightgbm as lgb
    import numpy as np
    import pyarrow.parquet as pq

    t0 = time.time()
    total_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in score_files)
    prog = Progress(progress_path, "Baseline de accuracy — 1/3 a ler scores e mapas", total_rows + rounds * 4 + 20, "passos")
    catalog = load_catalog(catalog_path)
    hist, train, test, stats = scan(score_files, catalog, hist_start=hist_start, cutoff=cutoff, split=split,
                                      per_player=per_player, hist_cap=hist_cap, seed=seed, progress=prog)
    prog.update(label="Baseline de accuracy — 2/3 a montar features", force=True)
    pf = player_features(hist, min_hist)
    if not pf:
        prog.finish("error", "Baseline de accuracy — poucos dados (nenhum jogador com histórico)")
        raise RuntimeError("poucos dados: nenhum jogador com histórico suficiente")
    xm_tr, xp_tr, xg_tr, y_tr, u_tr = assemble(train, pf)
    xm_te, xp_te, xg_te, y_te, u_te = assemble(test, pf)
    if len(y_tr) < 100 or len(y_te) < 100:
        prog.finish("error", "Baseline de accuracy — poucos dados")
        raise RuntimeError(f"poucos dados: treino={len(y_tr)} teste={len(y_te)}")

    # hold-out de jogadores: fora do treino; o teste divide-se em jogadores vistos e nunca vistos
    keep_tr = ~is_holdout(u_tr, holdout_pct, seed)
    xm_tr, xp_tr, xg_tr, y_tr, u_tr = xm_tr[keep_tr], xp_tr[keep_tr], xg_tr[keep_tr], y_tr[keep_tr], u_tr[keep_tr]
    hold_te = is_holdout(u_te, holdout_pct, seed)
    if len(y_tr) < 100:
        prog.finish("error", "Baseline de accuracy — poucos dados no treino")
        raise RuntimeError(f"poucos dados: treino={len(y_tr)} depois do hold-out")

    names_full = FEATURES_MAP + FEATURES_PLAYER + FEATURES_GAP
    x_tr, x_te = np.hstack([xm_tr, xp_tr, xg_tr]), np.hstack([xm_te, xp_te, xg_te])
    cols = {"lgbm_map_only": list(range(len(FEATURES_MAP))), "lgbm_full": list(range(len(names_full))),
            "lgbm_no_reading": [i for i, n in enumerate(names_full) if "reading" not in n],
            "lgbm_no_gap": [i for i, n in enumerate(names_full) if not n.startswith("gap_")]}
    params = {"objective": "regression", "learning_rate": 0.1, "num_leaves": 63, "min_data_in_leaf": 50,
              "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1, "seed": seed, "num_threads": 4}
    labels = {"lgbm_map_only": "só mapa", "lgbm_full": "mapa + jogador", "lgbm_no_reading": "sem features de Reading",
              "lgbm_no_gap": "sem distâncias mapa-jogador"}

    preds, models = {}, {}
    for name, idx in cols.items():
        prog.update(label=f"Baseline de accuracy — 3/3 LightGBM ({labels[name]})", force=True)
        models[name] = lgb.train(params, lgb.Dataset(x_tr[:, idx], y_tr, feature_name=[names_full[i] for i in idx]),
                                 num_boost_round=rounds, callbacks=[lambda env: prog.update(add=1)])
        preds[name] = models[name].predict(x_te[:, idx])

    stars = xm_te[:, 0]
    bands = {"stars_lt4": stars < 4, "stars_4_6": (stars >= 4) & (stars < 6), "stars_6_8": (stars >= 6) & (stars < 8), "stars_ge8": stars >= 8}
    results: dict[str, Any] = {}
    improvement: dict[str, Any] = {}
    by_band: dict[str, Any] = {}
    for group, mask in (("seen_players", ~hold_te), ("unseen_players", hold_te)):
        if int(mask.sum()) < 30:
            continue
        y = y_te[mask]
        r = {"global_mean": metrics(y, np.full_like(y, y_tr.mean())), "player_mean": metrics(y, xp_te[mask, 1])}
        r.update({name: metrics(y, p[mask]) for name, p in preds.items()})
        results[group] = {"n": int(mask.sum()), "players": int(len(set(u_te[mask].tolist()))), **r}
        base = r["player_mean"]["mae"]
        improvement[group] = {k: round((base - v["mae"]) / base * 100, 2) for k, v in r.items()}
        by_band[group] = {b: {"n": int((m & mask).sum()),
                              "player_mean_mae": round(float(np.abs(xp_te[m & mask, 1] - y_te[m & mask]).mean()), 5),
                              "lgbm_full_mae": round(float(np.abs(preds["lgbm_full"][m & mask] - y_te[m & mask]).mean()), 5)}
                          for b, m in bands.items() if (m & mask).sum() > 50}
    gain = models["lgbm_full"].feature_importance(importance_type="gain")
    imp = sorted(zip(names_full, (gain / gain.sum()).round(4).tolist()), key=lambda t: -t[1])[:12]
    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "window": {"hist_start": hist_start, "cutoff": cutoff, "split": split, "holdout_pct": holdout_pct},
           "sizes": {"train": int(len(y_tr)), "test": int(len(y_te)), "players_with_history": len(pf),
                     "train_players": int(len(set(u_tr.tolist()))), "test_players": int(len(set(u_te.tolist())))},
           "scan": stats, "results": results, "improvement_vs_player_mean_mae_pct": improvement,
           "target": {"test_mean": round(float(y_te.mean()), 4), "test_std": round(float(y_te.std()), 4)},
           "by_stars_band": by_band, "top_features_gain": imp, "score_files": [f.name for f in score_files],
           "params": {**params, "rounds": rounds, "per_player": per_player, "min_hist": min_hist},
           "notes": "Só passes; jogadores com histórico; hold-out de jogadores (unseen_players nunca entraram no treino); features do "
                    "jogador só do passado (< cutoff); MAE em fração de accuracy (0,01 = 1 ponto percentual). Ablações: "
                    "lgbm_no_reading (sem features de Reading) e lgbm_no_gap (sem distâncias mapa-jogador)."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    models["lgbm_full"].save_model(str(target / "lgbm_full.txt"))
    prog.finish()
    return out
