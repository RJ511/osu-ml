"""Semelhança de estilo: que método recupera melhor os mapas que um jogador joga mas que lhe escondemos?

Pergunta do utilizador (2026-09-24): "estilo parecido" pode ser (1) jogadores com perfil parecido (se um joga um mapa, o outro
provavelmente também quer jogá-lo) ou (2) mapa a mapa, comparando os atributos dos mapas. Aqui medem-se os dois (e a combinação)
contra uma baseline de popularidade.

Protocolo (sem fuga): os jogadores de teste são uma amostra com muitos mapas; 20 % dos seus pares (jogador, mapa) — escolhidos por hash
do par — ficam escondidos. Os vizinhos/semelhanças/popularidade vêm só de jogadores que NÃO são de teste. Para cada jogador de teste
ordenam-se todos os mapas do catálogo que ele não tem visíveis e mede-se a fração dos escondidos que fica no top-K (recall@K), também só
nos escondidos "de cauda" (fora dos 1000 mais populares), que é onde há personalização a sério.

O sinal é "o jogador TENTOU o mapa" (interesse), não "conseguiu": a alcançabilidade trata-se noutro modelo (`reach_model.py`).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress
from . import pass_model as pm

KS = (50, 200)
METHODS = ("popularity", "content_profile", "user_cf", "item_cf", "hybrid_user_content")


def _zscore_rows(a):
    import numpy as np

    return (a - a.mean(axis=1, keepdims=True)) / (a.std(axis=1, keepdims=True) + 1e-9)


def _recalls(scores, visible_mask, hidden_lists, pop_rank, ks=KS):
    """recall@K por jogador (linhas de `scores`); `hidden_lists[i]` = índices dos mapas escondidos."""
    import numpy as np

    scores = scores.copy()
    scores[visible_mask] = -np.inf
    kmax = max(ks)
    top = np.argpartition(-scores, kmax, axis=1)[:, :kmax]
    order = np.argsort(-np.take_along_axis(scores, top, axis=1), axis=1)
    top = np.take_along_axis(top, order, axis=1)
    out = {f"recall@{k}": [] for k in ks} | {f"tail_recall@{k}": [] for k in ks}
    for i, hid in enumerate(hidden_lists):
        if len(hid) == 0:
            continue
        hset = set(hid.tolist())
        tail = {h for h in hset if pop_rank[h] >= 1000}
        for k in ks:
            got = set(top[i, :k].tolist())
            out[f"recall@{k}"].append(len(got & hset) / len(hset))
            if tail:
                out[f"tail_recall@{k}"].append(len(got & tail) / len(tail))
    return out


def run_similarity_eval(inputs: Path, out_dir: Path, version: str = "v1", *, n_test_users: int = 1500, k_neighbors: int = 50,
                        top_items: int = 6000, min_pairs: int = 150, seed: int = 42, chunk: int = 150,
                        progress_path: Path | None = None, sample_pct: int = 100) -> dict[str, Any]:
    import numpy as np
    import scipy.sparse as sp

    t0 = time.time()
    playcounts = sorted(inputs.glob("osu_user_beatmap_playcount_*.parquet"))
    catalog = inputs / "map_attributes.parquet"
    if not playcounts or not catalog.exists():
        raise FileNotFoundError("faltam ficheiros em " + str(inputs))
    prog = Progress(progress_path, "Semelhança de estilo — a ler dados", 20 + n_test_users, "jogadores")
    cat_ids, cat_x = pm.load_catalog(catalog)
    users, bids, attempts, is_top = pm.load_pairs(playcounts, sample_pct=sample_pct)
    cpos = np.searchsorted(cat_ids, bids)
    cpos[cpos >= len(cat_ids)] = 0
    valid = cat_ids[cpos] == bids
    users, bids, cpos = users[valid], bids[valid], cpos[valid]
    uniq, uidx = np.unique(users, return_inverse=True)
    n_items = len(cat_ids)
    X = sp.csr_matrix((np.ones(len(uidx), dtype=np.float32), (uidx, cpos)), shape=(len(uniq), n_items))
    counts = np.asarray(X.sum(axis=1)).ravel()
    rng = np.random.default_rng(seed)
    eligible = np.nonzero(counts >= min_pairs)[0]
    if len(eligible) < 20:
        raise RuntimeError("poucos dados: jogadores elegíveis insuficientes")
    test_rows = np.sort(rng.choice(eligible, min(n_test_users, len(eligible) // 2), replace=False))
    is_test = np.zeros(len(uniq), dtype=bool)
    is_test[test_rows] = True
    train_rows = np.nonzero(~is_test)[0]
    Xtr = X[train_rows].tocsr()

    # esconder 20 % dos pares dos jogadores de teste (hash do par)
    pair_keys = pm._key(users, bids)
    hidden_pair = pm._hash01(pair_keys, seed + 11, 100) < 20
    test_pair = is_test[uidx]
    vis_mask_pairs = test_pair & ~hidden_pair
    hid_mask_pairs = test_pair & hidden_pair
    Xvis = sp.csr_matrix((np.ones(int(vis_mask_pairs.sum()), dtype=np.float32), (uidx[vis_mask_pairs], cpos[vis_mask_pairs])), shape=X.shape).tocsr()
    hid_by_user: dict[int, list[int]] = {}
    for u, c in zip(uidx[hid_mask_pairs], cpos[hid_mask_pairs]):
        hid_by_user.setdefault(int(u), []).append(int(c))

    pop = np.asarray(Xtr.sum(axis=0)).ravel()
    pop_order = np.argsort(-pop)
    pop_rank = np.empty(n_items, dtype=np.int64)
    pop_rank[pop_order] = np.arange(n_items)

    # atributos padronizados por mapa (conteúdo)
    z = cat_x.astype(np.float64).copy()
    lg = [pm.MAP_FEATS.index("n_objects")]
    z[:, lg] = np.log1p(z[:, lg])
    z = (z - z.mean(axis=0)) / (z.std(axis=0) + 1e-9)
    zn = z / (np.linalg.norm(z, axis=1, keepdims=True) + 1e-9)

    # item-item (só os `top_items` mais populares)
    top_idx = pop_order[:top_items]
    Xt = Xtr[:, top_idx].tocsc()
    co = (Xt.T @ Xt).toarray().astype(np.float32)
    npop = np.diag(co).copy()
    item_sim = co / (np.sqrt(np.outer(npop, npop)) + 1e-9)
    np.fill_diagonal(item_sim, 0.0)
    del co
    ntr = np.sqrt(np.asarray(Xtr.sum(axis=1)).ravel()) + 1e-9

    per_method: dict[str, dict[str, list]] = {m: {} for m in METHODS}
    prog.update(20, label="Semelhança de estilo — a avaliar", force=True)
    tests = test_rows
    for s in range(0, len(tests), chunk):
        rows_ = tests[s:s + chunk]
        V = Xvis[rows_]
        vis = V.toarray().astype(bool)
        hidden = [np.array(hid_by_user.get(int(r), []), dtype=np.int64) for r in rows_]
        nv = np.sqrt(np.asarray(V.sum(axis=1)).ravel()) + 1e-9
        scores: dict[str, Any] = {}
        scores["popularity"] = np.tile(pop.astype(np.float32), (len(rows_), 1))
        # (2) mapa a mapa: perfil médio dos mapas visíveis vs atributos padronizados de cada mapa
        prof = (V @ z) / np.maximum(np.asarray(V.sum(axis=1)), 1)
        pn = prof / (np.linalg.norm(prof, axis=1, keepdims=True) + 1e-9)
        scores["content_profile"] = (pn @ zn.T).astype(np.float32)
        # (1) jogadores parecidos: cosseno com jogadores de treino, top-k vizinhos
        sims = (V @ Xtr.T).toarray() / (nv[:, None] * ntr[None, :])
        kk = min(k_neighbors, sims.shape[1] - 1)
        nb = np.argpartition(-sims, kk, axis=1)[:, :kk]
        W = sp.csr_matrix((np.take_along_axis(sims, nb, axis=1).ravel(), (np.repeat(np.arange(len(rows_)), kk), nb.ravel())),
                          shape=(len(rows_), Xtr.shape[0]))
        scores["user_cf"] = (W @ Xtr).toarray().astype(np.float32)
        # item-item
        ic = np.zeros((len(rows_), n_items), dtype=np.float32)
        ic[:, top_idx] = V[:, top_idx].toarray() @ item_sim
        scores["item_cf"] = ic
        scores["hybrid_user_content"] = (_zscore_rows(scores["user_cf"]) + _zscore_rows(scores["content_profile"])).astype(np.float32)
        for m in METHODS:
            for k, v in _recalls(scores[m], vis, hidden, pop_rank).items():
                per_method[m].setdefault(k, []).extend(v)
        prog.update(20 + min(s + chunk, len(tests)), force=False)

    results = {m: {k: round(float(np.mean(v)), 4) for k, v in d.items() if v} for m, d in per_method.items()}
    n_hidden = [len(v) for v in hid_by_user.values()]
    out = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
           "data": {"players_total": int(len(uniq)), "train_players": int(len(train_rows)), "test_players": int(len(test_rows)),
                    "items_in_catalog": int(n_items), "pairs": int(len(uidx)), "hidden_per_test_player_mean": round(float(np.mean(n_hidden)), 1),
                    "top_items_for_item_cf": int(top_items), "k_neighbors": k_neighbors},
           "results": results,
           "notes": "sinal = tentou o mapa; 20 % dos pares dos jogadores de teste escondidos; popularidade/vizinhos só de jogadores de treino; "
                    "tail_recall = só os escondidos fora dos 1000 mais populares."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return out
