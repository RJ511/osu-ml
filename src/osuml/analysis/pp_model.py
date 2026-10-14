"""Modelo do **pp oficial**: pp(mapa, mods, accuracy, regime de pontuação), aprendido dos scores dos dumps.

Porquê: o pp calculado localmente com o rosu-pp 4.0.2 fica em média −8 % (até −20 %) abaixo do pp oficial de 2026 (que já inclui a skill
Reading). Para estimar o **ganho de pp** de uma sugestão precisamos do pp que o jogo atribuiria, não do nosso. Os dumps têm milhões de scores com o
pp oficial; aprende-se `log(pp)` a partir dos atributos do mapa (já calculados no catálogo, por combinação de mods), da accuracy e do regime
(`is_legacy`: stable vs lazer). Sem misses nem combo de propósito: no uso (recomendar) só se conhece a accuracy esperada.

Limites: só mods DT/NC, HT, HR, EZ, HD, FL (os do catálogo); sem rate changes; accuracy como único sinal de misses/combo; validado com jogadores de teste
nunca vistos (split por `user_id`).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress

MOD_FLAGS = ("HD", "HR", "EZ", "FL", "DT", "HT")
MAP_COLS = ["stars", "aim", "speed", "reading", "ar", "ar_mod", "cs", "od", "hp", "hp_mod", "n_objects", "n_sliders", "length_ms", "max_combo",
            "density", "reading_visual", "tech_entropy"]
FEATURES = ["acc", "is_legacy", *[f"m_{m}" for m in MOD_FLAGS], *MAP_COLS]
ALLOWED = set(MOD_FLAGS)
SCORE_COLS = ["score_id", "user_id", "beatmap_id", "accuracy", "pp", "mods_effective", "speed_change", "is_legacy", "passed"]
PP_BANDS = ((0, 100), (100, 200), (200, 300), (300, 450), (450, 10_000))


def mods_key(mods_effective: str | None) -> str | None:
    """`mods_effective` do score → chave do catálogo ("DT,HD"; NC=DT; sem CL); None se tiver mods fora do catálogo."""
    ms: set[str] = set()
    for m in (mods_effective or "").split(","):
        m = {"NC": "DT"}.get(m, m)
        if m in ("", "CL"):
            continue
        if m not in ALLOWED:
            return None
        ms.add(m)
    return ",".join(sorted(ms))


def load_catalog(path: Path):
    """(chaves ordenadas, matriz de atributos, {mods: código}); chave = beatmap_id*64 + código dos mods."""
    import numpy as np
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=["beatmap_id", "mods", *MAP_COLS])
    mods = t.column("mods").to_pylist()
    codes = {m: i for i, m in enumerate(sorted(set(mods)))}
    key = t.column("beatmap_id").to_numpy().astype(np.int64) * 64 + np.array([codes[m] for m in mods], dtype=np.int64)
    x = np.column_stack([np.nan_to_num(t.column(c).to_numpy(zero_copy_only=False).astype(np.float64), nan=np.nan) for c in MAP_COLS]).astype(np.float32)
    order = np.argsort(key)
    return key[order], x[order], codes


def flags_of(mods: str) -> list[int]:
    s = set(filter(None, mods.split(",")))
    return [int(m in s) for m in MOD_FLAGS]


def build_dataset(score_files: list[Path], catalog: Path, *, modulus_random: int = 20, modulus_top: int = 100, progress: Progress | None = None):
    """Amostra determinística (`score_id % módulo`) dos passes com pp oficial, ligada aos atributos do catálogo."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    keys, cat_x, codes = load_catalog(catalog)
    code_flags = {c: np.array(flags_of(m), dtype=np.float32) for m, c in codes.items()}
    xs, ys, users = [], [], []
    for f in score_files:
        modulus = modulus_top if "top_10000" in f.name else modulus_random
        for batch in pq.ParquetFile(f).iter_batches(batch_size=400_000, columns=SCORE_COLS):
            col = lambda n: batch.column(n).to_numpy(zero_copy_only=False)  # noqa: E731
            mask = (col("score_id") % modulus == 0) & col("passed").astype(bool)
            pp, acc = col("pp").astype(np.float64), col("accuracy").astype(np.float64)
            mask &= ~np.isnan(pp) & (pp > 0) & ~np.isnan(acc) & np.isnan(col("speed_change").astype(np.float64))
            idx = np.nonzero(mask)[0]
            if len(idx):
                sub = batch.take(idx)
                enc = pc.dictionary_encode(sub.column("mods_effective"))
                dmods = [mods_key(m) for m in enc.dictionary.to_pylist()]
                dcode = np.array([codes.get(m, -1) if m is not None else -1 for m in dmods], dtype=np.int64)
                mcode = dcode[enc.indices.to_numpy(zero_copy_only=False)]
                bid = sub.column("beatmap_id").to_numpy().astype(np.int64)
                k = bid * 64 + mcode
                pos = np.searchsorted(keys, k)
                pos[pos >= len(keys)] = len(keys) - 1
                ok = (mcode >= 0) & (keys[pos] == k)
                if ok.any():
                    a = sub.column("accuracy").to_numpy(zero_copy_only=False).astype(np.float32)[ok]
                    leg = sub.column("is_legacy").to_numpy(zero_copy_only=False).astype(np.float32)[ok]
                    fl = np.stack([code_flags[c] for c in mcode[ok]]) if ok.sum() else np.zeros((0, len(MOD_FLAGS)), dtype=np.float32)
                    xs.append(np.column_stack([a, leg, fl, cat_x[pos[ok]]]))
                    ys.append(np.log(sub.column("pp").to_numpy(zero_copy_only=False).astype(np.float64)[ok]).astype(np.float32))
                    users.append(sub.column("user_id").to_numpy().astype(np.int64)[ok])
            if progress:
                progress.update(add=batch.num_rows)
    return np.vstack(xs), np.concatenate(ys), np.concatenate(users)


def _splitmix(x):
    import numpy as np

    z = (x.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15))
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return z ^ (z >> np.uint64(31))


def split_users(users, seed: int = 42):
    """0 treino (70 %) · 1 validação (10 %) · 2 teste (20 %), por jogador: o teste são jogadores nunca vistos."""
    import numpy as np

    r = (_splitmix(users.astype(np.int64) ^ np.int64(seed)) % np.uint64(100)).astype(np.int64)
    return np.where(r < 70, 0, np.where(r < 80, 1, 2))


def rel_metrics(pp_true, pp_pred) -> dict[str, float]:
    import numpy as np

    err = pp_pred / pp_true - 1.0
    a = np.abs(err)
    return {"n": int(len(err)), "mean_bias_pct": round(float(err.mean()) * 100, 2), "median_abs_err_pct": round(float(np.median(a)) * 100, 2),
            "p90_abs_err_pct": round(float(np.quantile(a, 0.9)) * 100, 2), "within_5pct": round(float((a <= 0.05).mean()), 3),
            "within_10pct": round(float((a <= 0.10).mean()), 3)}


def run_pp_model(score_files: list[Path], catalog: Path, out_dir: Path, models_dir: Path | None, version: str = "v1", *, rounds: int = 700,
                 threads: int = 4, seed: int = 42, modulus_random: int = 20, modulus_top: int = 100,
                 progress_path: Path | None = None) -> dict[str, Any]:
    import lightgbm as lgb
    import numpy as np

    t0 = time.time()
    prog = Progress(progress_path, "pp oficial — a ler scores dos dumps", 1.0, "passos", min_interval=2.0)
    files = [f for f in score_files if "top_1000." not in f.name]  # o top_1000 está contido no top_10000
    prog.total = sum(__import__("pyarrow.parquet", fromlist=["x"]).ParquetFile(f).metadata.num_rows for f in files) + rounds
    x, y, users = build_dataset(files, catalog, modulus_random=modulus_random, modulus_top=modulus_top, progress=prog)
    prog.update(label="pp oficial — a treinar", force=True)
    g = split_users(users, seed)
    tr, va, te = (np.nonzero(g == i)[0] for i in range(3))
    params = {"objective": "regression", "metric": "l2", "learning_rate": 0.06, "num_leaves": 255, "min_data_in_leaf": 100, "feature_fraction": 0.9,
              "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 3.0, "verbose": -1, "seed": seed, "num_threads": threads}
    model = lgb.train(params, lgb.Dataset(x[tr], y[tr], feature_name=FEATURES), num_boost_round=rounds,
                      valid_sets=[lgb.Dataset(x[va], y[va], feature_name=FEATURES)],
                      callbacks=[lgb.early_stopping(40, verbose=False), lambda env: prog.update(add=1)])
    pp_t, pp_p = np.exp(y[te]), np.exp(model.predict(x[te]))
    col = {n: i for i, n in enumerate(FEATURES)}
    nomod = (x[te][:, [col[f"m_{m}"] for m in MOD_FLAGS]].sum(axis=1) == 0)
    results: dict[str, Any] = {
        "all": rel_metrics(pp_t, pp_p), "nomod": rel_metrics(pp_t[nomod], pp_p[nomod]),
        "lazer": rel_metrics(pp_t[x[te][:, col["is_legacy"]] == 0], pp_p[x[te][:, col["is_legacy"]] == 0]),
        "legacy": rel_metrics(pp_t[x[te][:, col["is_legacy"]] == 1], pp_p[x[te][:, col["is_legacy"]] == 1]),
        "by_pp": {f"{lo}-{hi}": rel_metrics(pp_t[(pp_t >= lo) & (pp_t < hi)], pp_p[(pp_t >= lo) & (pp_t < hi)]) for lo, hi in PP_BANDS
                  if ((pp_t >= lo) & (pp_t < hi)).sum() > 200},
        "by_mod": {m: rel_metrics(pp_t[x[te][:, col[f"m_{m}"]] == 1], pp_p[x[te][:, col[f"m_{m}"]] == 1]) for m in MOD_FLAGS
                   if (x[te][:, col[f"m_{m}"]] == 1).sum() > 200},
    }
    imp = model.feature_importance(importance_type="gain")
    results["top_features_gain"] = sorted(zip(FEATURES, (imp / max(imp.sum(), 1e-9)).round(4).tolist()), key=lambda t: -t[1])[:10]
    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "data": {"rows": int(len(y)), "players": int(len(set(users.tolist()))), "train_rows": int(len(tr)), "test_rows": int(len(te)),
                    "test_players": int(len(set(users[te].tolist()))), "best_iteration": int(model.best_iteration or rounds)},
           "results": results, "features": FEATURES, "mods_supported": list(MOD_FLAGS),
           "notes": "alvo = log(pp oficial) de passes do dump; sem misses/combo (só accuracy); jogadores de teste nunca vistos; baseline rosu-pp: média −7,8 %, mediana |erro| 8,2 %."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    model.save_model(str(target / "pp_official.txt"))
    if models_dir is not None:
        models_dir.mkdir(parents=True, exist_ok=True)
        model.save_model(str(models_dir / "pp_official.txt"))
        (models_dir / "pp_official.json").write_text(json.dumps({"features": FEATURES, "mods_supported": list(MOD_FLAGS), "trained_at": out["created_at"],
                                                                  "metrics": results["all"], "metrics_nomod": results["nomod"]}, indent=2), encoding="utf-8")
    prog.finish()
    return out
