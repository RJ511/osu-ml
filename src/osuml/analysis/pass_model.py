"""Modelo P(passar alguma vez | jogador, mapa), treinado com as tentativas do dump (`osu_user_beatmap_playcount`).

Dados (0 pedidos à API): pares (jogador, mapa) com nº de tentativas (playcount) + passes de `scores.sql`.
`y = 1` se o par tem algum passe guardado, `y = 0` se foi tentado e nunca passado. **Atenção ao que isto NÃO é**: o
playcount inclui quits/retries e não tem datas, por isso `y = 0` é "tentou e não passou", não "falhou N vezes".
É condicionado a o jogador ter escolhido tentar o mapa (seleção), e o mapa entra com os atributos **nomod** do catálogo
(o playcount não diz que mods se usaram).

Sem fuga de informação:
- Cada jogador tem os mapas divididos em duas metades (hash do `beatmap_id`). Nas duas "vistas", o perfil do jogador é
  calculado só com uma metade e os alvos são os pares da OUTRA. O perfil = P50/P90/máx da dificuldade dos seus melhores passes
  (top 200 por pp, como o `best` da API), accuracy média, mods habituais; (modelo B) taxa de passe e tentativas médias.
- Jogadores divididos em treino/validação/teste por hash do id (o teste nunca aparece no treino).
- Estatísticas por mapa (modelo C) só com jogadores de treino, *leave-one-out* nas linhas de treino.
Avaliações: jogadores de teste (aleatórios e top), por tentativas, repetição com várias sementes, sanidade com rótulos baralhados,
fora-do-tempo com jogadores da API (plays depois do snapshot) e PXD Vieira / gaaGOD só com dados da API.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress

BM_BITS = 23
ATTR_COLS = ["stars", "aim", "speed", "reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_ms"]
MAP_FEATS = ["stars", "aim", "speed", "log_reading", "density", "ar", "cs", "od", "hp", "n_objects", "length_s"]
PROF_ATTRS = ["stars", "aim", "speed", "log_reading", "density"]
PROFILE_FEATS = [f"p_{stat}_{a}" for a in PROF_ATTRS for stat in ("p50", "p90", "max")] + \
    ["p_mean_acc", "p_n_pass", "p_share_dt", "p_share_hd", "p_share_hr"]
GAP_FEATS = [f"gap_{a}" for a in PROF_ATTRS]
B_FEATS = ["p_pass_rate", "p_mean_log_attempts"]
C_FEATS = ["map_loo_rate", "map_log_n"]
FEATURE_SETS = {
    "M": MAP_FEATS,
    "A": MAP_FEATS + PROFILE_FEATS + GAP_FEATS,
    "B": MAP_FEATS + PROFILE_FEATS + GAP_FEATS + B_FEATS,
    "C": MAP_FEATS + PROFILE_FEATS + GAP_FEATS + C_FEATS,
}
ALL_FEATS = MAP_FEATS + PROFILE_FEATS + GAP_FEATS + B_FEATS + C_FEATS
TOP_PASSES = 200
MIN_PASSES = 10


def _key(user, bid):
    import numpy as np

    return user.astype(np.int64) * (1 << BM_BITS) + bid.astype(np.int64)


def _hash01(x, seed: int, mod: int = 100):
    """Hash inteiro com boa mistura de bits (splitmix64): sementes diferentes dão hashes independentes."""
    import numpy as np

    with np.errstate(over="ignore"):
        v = x.astype(np.uint64) + np.uint64((seed * 0x9E3779B97F4A7C15 + 0x1234567) & 0xFFFFFFFFFFFFFFFF)
        v = (v ^ (v >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        v = (v ^ (v >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        v = v ^ (v >> np.uint64(31))
    return (v % np.uint64(mod)).astype(np.int64)


# ---------------------------------------------------------------------------------------------- carregar dados
def load_catalog(path: Path):
    """(ids ordenados, matriz float32 (n, 11) já com log_reading e length_s) só com as linhas nomod."""
    import numpy as np
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=["beatmap_id", "mods", *ATTR_COLS])
    mods = np.array(t.column("mods").to_pylist(), dtype=object)
    keep = mods == ""
    ids = t.column("beatmap_id").to_numpy(zero_copy_only=False)[keep].astype(np.int64)
    cols = {c: t.column(c).to_numpy(zero_copy_only=False)[keep].astype(np.float64) for c in ATTR_COLS}
    x = np.column_stack([cols["stars"], cols["aim"], cols["speed"], np.log1p(np.clip(cols["reading"], 0, 1000)), cols["density"],
                         cols["ar"], cols["cs"], cols["od"], cols["hp"], cols["n_objects"], cols["length_ms"] / 1000.0])
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6), -1e6, 1e6).astype(np.float32)  # mapas degenerados
    order = np.argsort(ids)
    return ids[order], x[order]


def load_pairs(playcount_files: list[Path], top_marker: str = "top_1000", sample_pct: int = 100):
    """(user, bid, attempts, is_top) deduplicado por par; `is_top` pelo nome do ficheiro de origem."""
    import numpy as np
    import pyarrow.parquet as pq

    keys, att, top = [], [], []
    for f in playcount_files:
        t = pq.read_table(f, columns=["user_id", "beatmap_id", "playcount"])
        u = t.column("user_id").to_numpy(zero_copy_only=False)
        keep = _hash01(u, 99, 100) < sample_pct
        keys.append(_key(u[keep], t.column("beatmap_id").to_numpy(zero_copy_only=False)[keep]))
        att.append(t.column("playcount").to_numpy(zero_copy_only=False).astype(np.int32)[keep])
        top.append(np.full(len(keys[-1]), top_marker in f.name, dtype=bool))
    k, a, tp = np.concatenate(keys), np.concatenate(att), np.concatenate(top)
    order = np.argsort(k, kind="stable")
    k, a, tp = k[order], a[order], tp[order]
    first = np.concatenate([[True], k[1:] != k[:-1]])
    grp = np.cumsum(first) - 1
    amax = np.zeros(grp[-1] + 1, dtype=np.int32)
    np.maximum.at(amax, grp, a)
    tmax = np.zeros(grp[-1] + 1, dtype=bool)
    np.logical_or.at(tmax, grp, tp)
    uk = k[first]
    return uk >> BM_BITS, uk & ((1 << BM_BITS) - 1), amax, tmax


def load_passes_ex(score_files: list[Path], progress: Progress | None = None, sample_pct: int = 100):
    """Melhor passe (por pp) de cada (jogador, mapa): keys ordenadas, pp, accuracy, flags (1=DT/NC, 2=HD, 4=HR)."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    ks, pps, accs, fl = [], [], [], []
    for f in score_files:
        for batch in pq.ParquetFile(f).iter_batches(batch_size=500_000, columns=["user_id", "beatmap_id", "pp", "accuracy", "mods_effective"]):
            u = batch.column("user_id").to_numpy(zero_copy_only=False)
            keep = _hash01(u, 99, 100) < sample_pct
            ks.append(_key(u[keep], batch.column("beatmap_id").to_numpy(zero_copy_only=False)[keep]))
            pps.append(np.nan_to_num(batch.column("pp").to_numpy(zero_copy_only=False).astype(np.float32), nan=-1.0)[keep])
            accs.append(batch.column("accuracy").to_numpy(zero_copy_only=False).astype(np.float32)[keep])
            m = batch.column("mods_effective")
            dt = np.asarray(pc.or_(pc.match_substring(m, "DT"), pc.match_substring(m, "NC")).fill_null(False))[keep]
            hd = np.asarray(pc.match_substring(m, "HD").fill_null(False))[keep]
            hr = np.asarray(pc.match_substring(m, "HR").fill_null(False))[keep]
            fl.append(dt.astype(np.uint8) | (hd.astype(np.uint8) << 1) | (hr.astype(np.uint8) << 2))
            if progress:
                progress.update(add=batch.num_rows)
    k, pp, acc, f_ = (np.concatenate(x) for x in (ks, pps, accs, fl))
    order = np.lexsort((pp, k))  # por chave e, dentro dela, pp crescente: o último de cada chave é o melhor
    k, pp, acc, f_ = k[order], pp[order], acc[order], f_[order]
    first = np.concatenate([[True], k[1:] != k[:-1]])
    last = np.concatenate([k[1:] != k[:-1], [True]])
    acc_max = np.maximum.reduceat(acc, np.nonzero(first)[0])  # melhor accuracy entre todos os passes do par
    return k[last], np.where(pp[last] < 0, np.nan, pp[last]), acc[last], f_[last], acc_max


def load_passes(score_files: list[Path], progress: Progress | None = None, sample_pct: int = 100):
    """(keys, pp, accuracy, flags) do melhor passe (por pp) de cada par — ver `load_passes_ex` para a melhor accuracy."""
    return load_passes_ex(score_files, progress, sample_pct)[:4]


# ---------------------------------------------------------------------------------------------- perfil do jogador
def profile_vector(attrs, pp, acc, flags, n_pairs: int, mean_log_attempts: float):
    """(vetor de perfil A, extras B) a partir dos passes do jogador (attrs = (k, 5): stars, aim, speed, log_reading, density)."""
    import numpy as np

    k = len(pp)
    if k < MIN_PASSES:
        return None
    order = np.argsort(-np.nan_to_num(pp, nan=-1.0), kind="stable")[:TOP_PASSES]
    a = attrs[order].astype(np.float64)
    p50, p90, mx = np.percentile(a, 50, axis=0), np.percentile(a, 90, axis=0), a.max(axis=0)
    per_attr = np.column_stack([p50, p90, mx]).ravel()  # p50,p90,max de cada atributo, por ordem
    fl = flags[order]
    prof = np.concatenate([per_attr, [float(np.nanmean(acc[order])), float(k), float((fl & 1).mean()), float(((fl >> 1) & 1).mean()),
                                      float(((fl >> 2) & 1).mean())]])
    return prof.astype(np.float32), np.array([k / max(n_pairs, 1), mean_log_attempts], dtype=np.float32)


def gaps(map_x, prof):
    """Distância do mapa ao P90 do jogador (stars, aim, speed, log_reading, density)."""
    import numpy as np

    idx90 = [PROFILE_FEATS.index(f"p_p90_{a}") for a in PROF_ATTRS]
    m = [MAP_FEATS.index(a) for a in PROF_ATTRS]
    return (map_x[:, m] - prof[idx90][None, :]).astype(np.float32)


# ---------------------------------------------------------------------------------------------- dataset
def build_rows(users, bids, attempts, is_top, pass_keys, pass_pp, pass_acc, pass_flags, cat_ids, cat_x, *, seed: int = 42,
               cap_rows: int = 800, min_pairs: int = 40, progress: Progress | None = None, pass_accmax=None) -> dict[str, Any]:
    import numpy as np

    keys = _key(users, bids)
    pos = np.searchsorted(pass_keys, keys)
    pos[pos >= len(pass_keys)] = max(len(pass_keys) - 1, 0)
    passed = (pass_keys[pos] == keys) if len(pass_keys) else np.zeros(len(keys), dtype=bool)
    cpos = np.searchsorted(cat_ids, bids)
    cpos[cpos >= len(cat_ids)] = 0
    valid = cat_ids[cpos] == bids
    ymax = np.where(passed, pass_accmax[pos], 0.0).astype(np.float32) if pass_accmax is not None else np.zeros(len(keys), dtype=np.float32)
    users, bids, attempts, is_top, passed, pos, cpos, ymax = (x[valid] for x in (users, bids, attempts, is_top, passed, pos, cpos, ymax))
    order = np.argsort(users, kind="stable")
    users, bids, attempts, is_top, passed, pos, cpos, ymax = (x[order] for x in (users, bids, attempts, is_top, passed, pos, cpos, ymax))
    half = _hash01(bids, seed, 2)
    ustart = np.concatenate([[0], np.nonzero(users[1:] != users[:-1])[0] + 1, [len(users)]])

    out: dict[str, list] = {k: [] for k in ("x", "y", "user", "bid", "attempts", "top", "b_extra", "ymax")}
    n_players = len(ustart) - 1
    kept = 0
    for i in range(n_players):
        s, e = ustart[i], ustart[i + 1]
        if e - s < min_pairs:
            continue
        for view in (0, 1):
            feat = np.arange(s, e)[half[s:e] == view]
            targ = np.arange(s, e)[half[s:e] != view]
            fp = feat[passed[feat]]
            if len(fp) < MIN_PASSES or len(targ) < 5:
                continue
            res = profile_vector(cat_x[cpos[fp]][:, [MAP_FEATS.index(a) for a in PROF_ATTRS]], pass_pp[pos[fp]], pass_acc[pos[fp]],
                                 pass_flags[pos[fp]], len(feat), float(np.log1p(attempts[feat]).mean()))
            if res is None:
                continue
            prof, b_extra = res
            if len(targ) > cap_rows:  # subamostra determinística por hash do mapa
                targ = targ[np.argsort(_hash01(bids[targ], seed + 7, 1_000_003), kind="stable")[:cap_rows]]
            mx = cat_x[cpos[targ]]
            x = np.hstack([mx, np.repeat(prof[None, :], len(targ), axis=0), gaps(mx, prof)])
            out["x"].append(x)
            out["y"].append(passed[targ].astype(np.int8))
            out["user"].append(np.full(len(targ), users[s], dtype=np.int64))
            out["bid"].append(bids[targ])
            out["attempts"].append(attempts[targ])
            out["ymax"].append(ymax[targ])
            out["top"].append(is_top[targ])
            out["b_extra"].append(np.repeat(b_extra[None, :], len(targ), axis=0))
            kept += 1
        if progress and i % 200 == 0:
            progress.update(add=200)
    if not out["x"]:
        raise RuntimeError("poucos dados: nenhum jogador com pares suficientes")
    return {"x": np.vstack(out["x"]).astype(np.float32), "y": np.concatenate(out["y"]), "user": np.concatenate(out["user"]),
            "bid": np.concatenate(out["bid"]), "attempts": np.concatenate(out["attempts"]), "top": np.concatenate(out["top"]),
            "b_extra": np.vstack(out["b_extra"]).astype(np.float32), "player_views": kept,
            "best_acc": np.concatenate(out["ymax"]).astype(np.float32)}


def split_groups(users, seed: int):
    """0=treino (70 %), 1=validação (10 %), 2=teste (20 %), por jogador."""
    import numpy as np

    h = _hash01(users, seed * 31 + 5, 100)
    return np.where(h < 70, 0, np.where(h < 80, 1, 2))


def map_stats(bids_train, y_train):
    """(bids únicos, nº de pares, nº de passes) por mapa só com linhas de treino."""
    import numpy as np

    ub, inv = np.unique(bids_train, return_inverse=True)
    n = np.bincount(inv, minlength=len(ub)).astype(np.float64)
    p = np.bincount(inv, weights=y_train.astype(np.float64), minlength=len(ub))
    return ub, n, p, inv


def add_map_features(rows: dict[str, Any], groups, prior: float | None = None, alpha: float = 5.0):
    """Acrescenta `map_loo_rate` e `map_log_n` (leave-one-out nas linhas de treino, estatísticas completas nas restantes)."""
    import numpy as np

    train = groups == 0
    ub, n, p, inv = map_stats(rows["bid"][train], rows["y"][train])
    prior = float(rows["y"][train].mean()) if prior is None else prior
    rate = np.zeros(len(rows["y"]), dtype=np.float32)
    logn = np.zeros(len(rows["y"]), dtype=np.float32)
    ti = np.nonzero(train)[0]
    yt = rows["y"][train].astype(np.float64)
    nn = n[inv] - 1
    rate[ti] = ((p[inv] - yt + alpha * prior) / (nn + alpha)).astype(np.float32)
    logn[ti] = np.log1p(np.maximum(nn, 0)).astype(np.float32)
    oi = np.nonzero(~train)[0]
    pos = np.searchsorted(ub, rows["bid"][oi])
    pos[pos >= len(ub)] = 0
    found = ub[pos] == rows["bid"][oi] if len(ub) else np.zeros(len(oi), dtype=bool)
    nn2 = np.where(found, n[pos], 0.0)
    pp2 = np.where(found, p[pos], 0.0)
    rate[oi] = ((pp2 + alpha * prior) / (nn2 + alpha)).astype(np.float32)
    logn[oi] = np.log1p(nn2).astype(np.float32)
    return np.column_stack([rate, logn]), {"bids": ub, "n": n, "p": p, "prior": prior, "alpha": alpha}


# ---------------------------------------------------------------------------------------------- métricas
def auc(y, p) -> float | None:
    import numpy as np
    from scipy.stats import rankdata

    y = np.asarray(y)
    n1 = int((y == 1).sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(p)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def calibration(y, p, bins: int = 10) -> dict[str, Any]:
    import numpy as np

    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    edges[-1] += 1e-9
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, bins - 1)
    rows, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append({"mean_pred": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4), "n": int(m.sum())})
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
    return {"ece": round(float(ece), 4), "bins": rows}


def metrics(y, p) -> dict[str, Any]:
    import numpy as np

    y = np.asarray(y)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    a = auc(y, p)
    return {"n": int(len(y)), "base_rate": round(float(y.mean()), 4), "auc": None if a is None else round(a, 4),
            "logloss": round(float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), 4),
            "brier": round(float(((p - y) ** 2).mean()), 4), "mean_pred": round(float(p.mean()), 4)}


def bootstrap_auc(y, p, n: int = 500, seed: int = 0) -> list[float] | None:
    import numpy as np

    y, p = np.asarray(y), np.asarray(p)
    if len(np.unique(y)) < 2:
        return None
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        a = auc(y[i], p[i])
        if a is not None:
            vals.append(a)
    return [round(float(np.percentile(vals, 2.5)), 3), round(float(np.percentile(vals, 97.5)), 3)] if vals else None


# ---------------------------------------------------------------------------------------------- treino
def _matrix(rows, extra_map, names):
    import numpy as np

    cols = []
    for n in names:
        if n in ALL_FEATS[:len(MAP_FEATS) + len(PROFILE_FEATS) + len(GAP_FEATS)]:
            cols.append(rows["x"][:, ALL_FEATS.index(n)])
        elif n in B_FEATS:
            cols.append(rows["b_extra"][:, B_FEATS.index(n)])
        else:
            cols.append(extra_map[:, C_FEATS.index(n)])
    return np.column_stack(cols).astype(np.float32)


def train_model(x_tr, y_tr, x_va, y_va, names, *, rounds: int, threads: int, seed: int, progress: Progress | None):
    import lightgbm as lgb

    params = {"objective": "binary", "metric": "auc", "learning_rate": 0.08, "num_leaves": 127, "min_data_in_leaf": 200,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1,
              "seed": seed, "num_threads": threads}
    cbs = [lgb.early_stopping(40, verbose=False)]
    if progress:
        cbs.append(lambda env: progress.update(add=1))
    return lgb.train(params, lgb.Dataset(x_tr, y_tr, feature_name=names), num_boost_round=rounds,
                     valid_sets=[lgb.Dataset(x_va, y_va, feature_name=names)], callbacks=cbs)


def run_pass_model(inputs: Path, out_dir: Path, version: str = "v1", *, seeds: tuple[int, ...] = (42, 43, 44), rounds: int = 600,
                   threads: int = 4, cap_rows: int = 800, cap_train: int = 4_000_000, progress_path: Path | None = None,
                   api_plays: Path | None = None, players: dict[str, int] | None = None, sample_pct: int = 100) -> dict[str, Any]:
    import numpy as np

    t0 = time.time()
    playcounts = sorted(inputs.glob("osu_user_beatmap_playcount_*.parquet"))
    scores = sorted(inputs.glob("dump_scores_*.parquet"))
    catalog = inputs / "map_attributes.parquet"
    if not playcounts or not scores or not catalog.exists():
        raise FileNotFoundError("faltam ficheiros em " + str(inputs))
    n_models = 4 + len(seeds) + 2
    prog = Progress(progress_path, "Modelo pass/fail — 1/5 a ler dados", 100 + n_models * rounds, "passos")
    cat_ids, cat_x = load_catalog(catalog)
    users, bids, attempts, is_top = load_pairs(playcounts, sample_pct=sample_pct)
    prog.update(10, label="Modelo pass/fail — 1/5 a ler passes do dump", force=True)
    pkeys, ppp, pacc, pfl = load_passes(scores, sample_pct=sample_pct)
    prog.update(25, label="Modelo pass/fail — 2/5 a construir perfis e linhas", force=True)
    rows = build_rows(users, bids, attempts, is_top, pkeys, ppp, pacc, pfl, cat_ids, cat_x, cap_rows=cap_rows)
    y = rows["y"]
    prog.update(45, label="Modelo pass/fail — 3/5 a treinar", force=True)
    seed0 = seeds[0]
    groups = split_groups(rows["user"], seed0)
    map_feat, mstats = add_map_features(rows, groups)

    def subset(g, cap=None):
        idx = np.nonzero(groups == g)[0]
        if cap and len(idx) > cap:
            idx = np.sort(np.random.default_rng(seed0).choice(idx, cap, replace=False))
        return idx

    tr, va, te = subset(0, cap_train), subset(1, 600_000), subset(2)
    results: dict[str, Any] = {}
    models = {}
    for name in ("M", "A", "B", "C"):
        names = FEATURE_SETS[name]
        x = _matrix(rows, map_feat, names)
        prog.update(label=f"Modelo pass/fail — 3/5 a treinar o modelo {name}", force=True)
        models[name] = train_model(x[tr], y[tr], x[va], y[va], names, rounds=rounds, threads=threads, seed=seed0, progress=prog)
        results[name] = _evaluate(models[name], x, y, te, rows)
        if name == "A":
            imp = models[name].feature_importance(importance_type="gain")
            results["A"]["top_features_gain"] = sorted(zip(names, (imp / imp.sum()).round(4).tolist()), key=lambda t: -t[1])[:12]
            xa = x
    # baselines triviais no teste
    rows_te = {"y": y[te]}
    results["baselines"] = {
        "map_loo_rate": metrics(y[te], map_feat[te, 0]),
        "player_pass_rate": metrics(y[te], rows["b_extra"][te, 0]),
        "constant_train_rate": metrics(y[te], np.full(len(te), y[tr].mean()))}

    # repetição com outras divisões de jogadores (só o modelo A) + rótulos baralhados
    repeats = []
    for s in seeds:
        g = split_groups(rows["user"], s)
        tr_s, va_s, te_s = (np.nonzero(g == k)[0] for k in (0, 1, 2))
        if len(tr_s) > cap_train:
            tr_s = np.sort(np.random.default_rng(s).choice(tr_s, cap_train, replace=False))
        prog.update(label=f"Modelo pass/fail — 4/5 repetição (semente {s})", force=True)
        m = train_model(xa[tr_s], y[tr_s], xa[va_s[:600_000]], y[va_s[:600_000]], FEATURE_SETS["A"], rounds=rounds, threads=threads, seed=s, progress=prog)
        repeats.append({"seed": s, "test_players": int(len(set(rows["user"][te_s].tolist()))), **metrics(y[te_s], m.predict(xa[te_s]))})
    aucs = [r["auc"] for r in repeats if r["auc"] is not None]
    results["repeats_A"] = {"runs": repeats, "auc_mean": round(float(np.mean(aucs)), 4), "auc_sd": round(float(np.std(aucs)), 4)}
    ysh = np.random.default_rng(1).permutation(y[tr][:1_000_000])
    prog.update(label="Modelo pass/fail — 4/5 sanidade (rótulos baralhados)", force=True)
    msh = train_model(xa[tr][:1_000_000], ysh, xa[va][:200_000], y[va][:200_000], FEATURE_SETS["A"], rounds=min(rounds, 100), threads=threads,
                      seed=seed0, progress=prog)
    results["shuffled_labels_A"] = metrics(y[te], msh.predict(xa[te]))

    api_out = None
    if api_plays is not None and Path(api_plays).exists():
        prog.update(label="Modelo pass/fail — 5/5 jogadores da API (fora do tempo, PXD Vieira, gaaGOD)", force=True)
        api_out = evaluate_api(Path(api_plays), models, users, bids, attempts, pkeys, ppp, pacc, pfl, cat_ids, cat_x, mstats, players or {})

    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "data": {"pairs_with_catalog": int(len(rows["y"])), "player_views": rows["player_views"], "base_rate": round(float(y.mean()), 4),
                    "players": int(len(set(rows["user"].tolist()))), "train_rows": int(len(tr)), "val_rows": int(len(va)), "test_rows": int(len(te)),
                    "test_players": int(len(set(rows["user"][te].tolist())))},
           "results": results, "api": api_out, "features": {k: v for k, v in FEATURE_SETS.items()},
           "notes": "y=1: passou alguma vez; y=0: tentou e não passou (inclui quits). Atributos nomod. Condicionado a o jogador ter tentado o mapa. "
                    "Perfil do jogador com uma metade dos mapas, alvos na outra. Ver docstring de pass_model.py."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    for n in ("A", "C"):
        models[n].save_model(str(target / f"pass_model_{n}.txt"))
    prog.finish()
    return out


def _evaluate(model, x_all, y, te, rows) -> dict[str, Any]:
    import numpy as np

    p = model.predict(x_all[te])
    yt = y[te]
    stars = rows["x"][te, 0]
    att = rows["attempts"][te]
    top = rows["top"][te]
    out = {"all": metrics(yt, p), "calibration": calibration(yt, p),
           "by_source": {"random": metrics(yt[~top], p[~top]) if (~top).any() else None, "top": metrics(yt[top], p[top]) if top.any() else None},
           "by_attempts": {lab: metrics(yt[m], p[m]) for lab, m in
                           (("1", att == 1), ("2-4", (att >= 2) & (att <= 4)), ("5-19", (att >= 5) & (att < 20)), ("20+", att >= 20)) if m.sum() > 200},
           "by_stars": {lab: metrics(yt[m], p[m]) for lab, m in
                        (("<3", stars < 3), ("3-5", (stars >= 3) & (stars < 5)), ("5-7", (stars >= 5) & (stars < 7)), (">=7", stars >= 7)) if m.sum() > 200}}
    return out


# ---------------------------------------------------------------------------------------------- jogadores da API
def export_api_plays(store, out_path: Path) -> dict[str, Any]:
    """Scores da BD (API) → Parquet pequeno para avaliar o modelo (só leitura da BD local)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import select

    from ..dataset.derived import mods_effective
    from ..storage import models as m

    with store.engine.connect() as c:
        names = dict(c.execute(select(m.users.c.user_id, m.users.c.username)).all())
        rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.ended_at, m.scores.c.mod_acronyms,
                                m.scores.c.pp, m.scores.c.accuracy, m.scores.c.first_source).where(m.scores.c.beatmap_id.isnot(None))).all()
    data = [{"user_id": int(u), "username": names.get(u), "beatmap_id": int(b), "passed": bool(p), "ended_at": e,
             "mods_effective": mods_effective(mo), "pp": pp, "accuracy": acc, "first_source": fs} for u, b, p, e, mo, pp, acc, fs in rows]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(data), out_path)
    return {"rows": len(data), "users": len({d["user_id"] for d in data}), "out": str(out_path)}


def evaluate_api(api_path: Path, models, users, bids, attempts, pkeys, ppp, pacc, pfl, cat_ids, cat_x, mstats, players: dict[str, int],
                 snapshot: str = "2026-09-01") -> dict[str, Any]:
    import numpy as np
    import pyarrow.parquet as pq

    t = pq.read_table(api_path).to_pylist()
    by_user: dict[int, list[dict]] = {}
    for r in t:
        by_user.setdefault(r["user_id"], []).append(r)
    dump_users = set(np.unique(users).tolist())
    snap = np.datetime64(snapshot)
    x_idx = {n: i for i, n in enumerate(MAP_FEATS)}

    def map_attrs(bid):
        i = int(np.searchsorted(cat_ids, bid))
        return cat_x[i] if i < len(cat_ids) and cat_ids[i] == bid else None

    def map_c(bid):
        i = int(np.searchsorted(mstats["bids"], bid))
        n, p = (mstats["n"][i], mstats["p"][i]) if i < len(mstats["bids"]) and mstats["bids"][i] == bid else (0.0, 0.0)
        return [(p + mstats["alpha"] * mstats["prior"]) / (n + mstats["alpha"]), np.log1p(n)]

    def predict(prof, b_extra, bid_list):
        xs, cs, ok = [], [], []
        for b in bid_list:
            mx = map_attrs(b)
            if mx is None:
                ok.append(False)
                continue
            ok.append(True)
            xs.append(np.concatenate([mx, prof, gaps(mx[None, :], prof)[0]]))
            cs.append(map_c(b))
        if not xs:
            return np.array(ok), {}
        x = np.array(xs, dtype=np.float32)
        c = np.array(cs, dtype=np.float32)
        b = np.repeat(b_extra[None, :], len(x), axis=0)
        rows = {"x": x, "b_extra": b}
        return np.array(ok), {n: models[n].predict(_matrix(rows, c, FEATURE_SETS[n])) for n in models if n in ("A", "B", "C")}

    out: dict[str, Any] = {"dump_players": {}, "api_only": {}}
    # (a) jogadores da API que existem nos dumps: fora-do-tempo (plays depois do snapshot)
    rows_a: list[tuple] = []
    for uid, plays in by_user.items():
        if uid not in dump_users:
            continue
        sel = np.nonzero(users == uid)[0]
        ub, ua, uat = bids[sel], attempts[sel], None
        pk = _key(np.full(len(ub), uid), ub)
        pos = np.searchsorted(pkeys, pk)
        pos[pos >= len(pkeys)] = 0
        passed = pkeys[pos] == pk
        cpos = np.searchsorted(cat_ids, ub)
        cpos[cpos >= len(cat_ids)] = 0
        valid = cat_ids[cpos] == ub
        fp = np.nonzero(passed & valid)[0]
        if len(fp) < MIN_PASSES:
            continue
        res = profile_vector(cat_x[cpos[fp]][:, [x_idx[a] for a in PROF_ATTRS]], ppp[pos[fp]], pacc[pos[fp]], pfl[pos[fp]], int(valid.sum()),
                             float(np.log1p(ua[valid]).mean()))
        if res is None:
            continue
        prof, bx = res
        state = {int(b): ("passed" if pa else "never") for b, pa in zip(ub, passed)}
        recent = [r for r in plays if r["ended_at"] is not None and np.datetime64(r["ended_at"]) >= snap]
        if not recent:
            continue
        ok, preds = predict(prof, bx, [r["beatmap_id"] for r in recent])
        kept = [r for r, o in zip(recent, ok) if o]
        for i, r in enumerate(kept):
            rows_a.append((uid, r["beatmap_id"], int(r["passed"]), state.get(r["beatmap_id"], "new"), {n: float(p[i]) for n, p in preds.items()},
                           not (r["mods_effective"] or "")))
    if rows_a:
        yv = np.array([r[2] for r in rows_a])
        cats = np.array([r[3] for r in rows_a])
        nomod = np.array([r[5] for r in rows_a])
        for name in ("A", "B", "C"):
            pv = np.array([r[4][name] for r in rows_a])
            out["dump_players"][name] = {"all": {**metrics(yv, pv), "auc_ci95": bootstrap_auc(yv, pv)},
                                         "by_state": {c: metrics(yv[cats == c], pv[cats == c]) for c in ("passed", "never", "new") if (cats == c).sum() >= 20},
                                         # o modelo só vê atributos nomod: com mods (DT/HD/HR) a comparação é injusta
                                         "by_mods": {lab: {**metrics(yv[m], pv[m]), "auc_ci95": bootstrap_auc(yv[m], pv[m])}
                                                     for lab, m in (("nomod", nomod), ("modded", ~nomod)) if m.sum() >= 30}}
        out["dump_players"]["n_players"] = len({r[0] for r in rows_a})
        out["dump_players"]["note"] = "plays da API depois de " + snapshot + "; y = passou nessa tentativa; estado = situação do par no dump"

    # (b) jogadores só da API (PXD Vieira, gaaGOD, ...): perfil dos seus passes (top 200 por pp) sem o mapa-alvo
    for label, uid in players.items():
        plays = by_user.get(uid, [])
        if not plays or uid in dump_users:
            continue
        passes = [r for r in plays if r["passed"] and map_attrs(r["beatmap_id"]) is not None]
        best_by_map: dict[int, dict] = {}
        for r in passes:
            cur = best_by_map.get(r["beatmap_id"])
            if cur is None or (r["pp"] or -1) > (cur["pp"] or -1):
                best_by_map[r["beatmap_id"]] = r
        recent = [r for r in plays if r["first_source"] == "recent" and map_attrs(r["beatmap_id"]) is not None]
        targets = []  # (mapa, y_play, origem)
        nomod_map = {r["beatmap_id"]: not (r["mods_effective"] or "") for r in recent}
        for r in recent:
            targets.append((r["beatmap_id"], int(r["passed"]), "recent_nomod" if not (r["mods_effective"] or "") else "recent"))
        ever = {r["beatmap_id"] for r in plays if r["passed"]}
        pair_targets = [(b, 1 if b in ever else 0) for b in {r["beatmap_id"] for r in plays if map_attrs(r["beatmap_id"]) is not None}]
        res_rows: list[tuple] = []
        for tgt, ylab, src in targets + [(b, y_, "pair") for b, y_ in pair_targets]:
            others = [r for r in best_by_map.values() if r["beatmap_id"] != tgt]
            if len(others) < MIN_PASSES:
                continue
            attrs = np.array([map_attrs(r["beatmap_id"])[[x_idx[a] for a in PROF_ATTRS]] for r in others])
            flags = np.array([(1 if any(m in (r["mods_effective"] or "") for m in ("DT", "NC")) else 0)
                              | (2 if "HD" in (r["mods_effective"] or "") else 0) | (4 if "HR" in (r["mods_effective"] or "") else 0) for r in others], dtype=np.uint8)
            prof, bx = profile_vector(attrs, np.array([r["pp"] if r["pp"] is not None else np.nan for r in others], dtype=np.float32),
                                      np.array([r["accuracy"] or 0.9 for r in others], dtype=np.float32), flags, len(others), 1.0)
            ok, preds = predict(prof, bx, [tgt])
            if ok[0]:
                res_rows.append((tgt, ylab, src, {n: float(p[0]) for n, p in preds.items() if n in ("A", "C")}))
        block: dict[str, Any] = {"user_id": uid, "n_plays_in_db": len(plays), "n_passes_used_for_profile": len(best_by_map)}
        for src in ("recent_nomod", "recent", "pair"):
            sub = [r for r in res_rows if r[2] == src]
            if len(sub) < 5:
                continue
            yv = np.array([r[1] for r in sub])
            block[src] = {n: {**metrics(yv, np.array([r[3][n] for r in sub])), "auc_ci95": bootstrap_auc(yv, np.array([r[3][n] for r in sub]))}
                          for n in ("A", "C")}
        fails = sorted([r for r in res_rows if r[2] == "pair" and r[1] == 0], key=lambda r: r[3]["C"])[:10]
        block["lowest_predictions_on_never_passed"] = [{"beatmap_id": int(r[0]), "pA": round(r[3]["A"], 3), "pC": round(r[3]["C"], 3)} for r in fails]
        out["api_only"][label] = block
    return out
