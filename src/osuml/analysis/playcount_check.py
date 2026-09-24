"""Testa se `osu_user_beatmap_playcount` (tentativas por jogador e mapa) + os passes de `scores.sql` permitem estimar
FAILS sem a API: `fails_est = playcount − nº de passes guardados` por (jogador, mapa).

Hipóteses a verificar (nada é assumido):
1. `playcount >= passes` na grande maioria dos pares (senão a tabela não conta o que se pensa, ou `scores` tem repetidos);
2. pares com `playcount > passes` existem em número relevante (= tentativas falhadas/abandonadas);
3. pares em `playcount` sem qualquer score = mapas tentados e nunca passados (ou não ranked).
Limites conhecidos à partida: não há data nas tentativas (agregado de toda a história) e o `playcount` inclui quits.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..progress import Progress

BM_BITS = 23  # beatmap_id < 2^23 (8,4 M)


def _key(user, beatmap):
    import numpy as np

    return user.astype(np.int64) * (1 << BM_BITS) + beatmap.astype(np.int64)


def load_keys_and_counts(files: list[Path], user_col: str, map_col: str, count_col: str | None, progress: Progress):
    import numpy as np
    import pyarrow.parquet as pq

    keys, counts = [], []
    for f in files:
        cols = [user_col, map_col] + ([count_col] if count_col else [])
        for batch in pq.ParquetFile(f).iter_batches(batch_size=1_000_000, columns=cols):
            u = batch.column(user_col).to_numpy(zero_copy_only=False)
            b = batch.column(map_col).to_numpy(zero_copy_only=False)
            keys.append(_key(u, b))
            counts.append(batch.column(count_col).to_numpy(zero_copy_only=False).astype(np.int64) if count_col
                          else np.ones(len(u), dtype=np.int64))
            progress.update(add=batch.num_rows)
    return np.concatenate(keys), np.concatenate(counts)


def run_playcount_check(score_files: list[Path], playcount_files: list[Path], out_dir: Path, version: str = "v1",
                        progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np
    import pyarrow.parquet as pq

    t0 = time.time()
    n_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in [*score_files, *playcount_files])
    prog = Progress(progress_path, "Playcount vs passes — a ler", n_rows + 10, "linhas")
    pk, pc = load_keys_and_counts(playcount_files, "user_id", "beatmap_id", "playcount", prog)
    sk, sc = load_keys_and_counts(score_files, "user_id", "beatmap_id", None, prog)
    prog.update(label="Playcount vs passes — a cruzar", force=True)

    # playcount: um valor por par (se repetido entre dumps, fica o maior); passes: nº de scores por par
    order = np.argsort(pk, kind="stable")
    pk, pc = pk[order], pc[order]
    first = np.concatenate([[True], pk[1:] != pk[:-1]])
    group = np.cumsum(first) - 1
    pc_max = np.zeros(group[-1] + 1, dtype=np.int64)
    np.maximum.at(pc_max, group, pc)
    pk_u, pc_u = pk[first], pc_max
    sk_u, s_cnt = np.unique(sk, return_counts=True)

    # só os jogadores presentes nos dois lados (um dump pode ter a tabela de jogadores que o outro não tem)
    users_scores = np.unique(sk_u >> BM_BITS)
    in_users = np.isin(pk_u >> BM_BITS, users_scores)
    pk_u, pc_u = pk_u[in_users], pc_u[in_users]

    idx = np.searchsorted(sk_u, pk_u)
    idx[idx >= len(sk_u)] = len(sk_u) - 1
    matched = sk_u[idx] == pk_u
    passes = np.where(matched, s_cnt[idx], 0)
    only_scores = int(len(sk_u) - matched.sum())

    both = matched
    delta = (pc_u - passes)[both]
    res: dict[str, Any] = {
        "pairs_playcount": int(len(pk_u)), "pairs_scores": int(len(sk_u)), "pairs_matched": int(both.sum()),
        "pairs_only_in_playcount": int((~both).sum()), "pairs_only_in_scores": only_scores,
        "matched": {
            "playcount_ge_passes_share": round(float((delta >= 0).mean()), 4),
            "playcount_eq_passes_share": round(float((delta == 0).mean()), 4),
            "extra_attempts_share": round(float((delta > 0).mean()), 4),
            "delta_quantiles_p50_p90_p99": [int(np.percentile(delta, q)) for q in (50, 90, 99)],
            "delta_mean": round(float(delta.mean()), 3),
            "passes_per_pair_mean": round(float(passes[both].mean()), 3),
            "pairs_with_multiple_scores_share": round(float((passes[both] > 1).mean()), 4),
            "first_try_pass_share": round(float((pc_u[both] == 1).mean()), 4)},
        "never_passed_pairs": {"n": int((~both).sum()), "share_of_playcount_pairs": round(float((~both).mean()), 4),
                               "mean_attempts": round(float(pc_u[~both].mean()), 3) if (~both).any() else None},
        "estimated_fails_total": int(np.maximum(pc_u - passes, 0).sum()),
        "total_attempts": int(pc_u.sum()), "total_passes": int(passes.sum()),
        "players": int(len(np.unique(pk_u >> BM_BITS)))}
    manifest = {"dataset_version": version, "created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
                "result": res, "score_files": [f.name for f in score_files], "playcount_files": [f.name for f in playcount_files],
                "notes": "fails_est = playcount - passes guardados (aproximação; playcount inclui quits; sem datas)."}
    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    (target / "playcount_check.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return manifest
