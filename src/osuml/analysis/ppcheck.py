"""Validação do nosso cálculo de pp (rosu-pp) contra o **pp oficial** guardado no dump, numa amostra.

Só scores **legacy** (stable: estatísticas great/ok/meh/miss completas), sem RX/AP/speed_change, com pp oficial e
mapa disponível no bundle do catálogo. Amostragem determinística por `score_id`, sem carregar os 10 M de linhas
em memória. Objetivo: confirmar que o pipeline (mods, `.osu` do dump, versão do rosu-pp) reproduz o pp oficial —
NÃO é para recalcular o pp de tudo. Diferenças de alguns % são esperadas se o pp do dump foi recalculado com uma
versão do algoritmo diferente da do `rosu-pp-py` instalado.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..beatmaps.acquire import _ID_NAME, _iter_path
from ..progress import Progress

COLUMNS = ["score_id", "beatmap_id", "is_legacy", "pp", "n_great", "n_ok", "n_meh", "n_miss", "max_combo",
           "mods_effective", "speed_change", "accuracy"]
SKIP_MODS = {"RX", "AP", "TD"}


def sample_candidates(score_files: list[Path], wanted: set[int], progress: Progress, modulus: int = 250) -> list[dict[str, Any]]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    wanted_arr = np.fromiter(wanted, dtype=np.int64)
    out: list[dict[str, Any]] = []
    for f in score_files:
        for batch in pq.ParquetFile(f).iter_batches(batch_size=250_000, columns=COLUMNS):
            col = lambda n: batch.column(n).to_numpy(zero_copy_only=False)  # noqa: E731
            mask = (col("score_id") % modulus == 0) & col("is_legacy").astype(bool)
            for name in ("pp", "n_great", "n_ok", "n_meh", "n_miss", "max_combo"):
                mask &= ~np.isnan(col(name).astype(float))
            mask &= np.isnan(col("speed_change").astype(float)) & np.isin(col("beatmap_id"), wanted_arr)
            idx = np.nonzero(mask)[0]
            if len(idx):
                out.extend(batch.take(pa.array(idx)).to_pylist())
            progress.update(add=batch.num_rows)
    return [r for r in out if not (set(filter(None, r["mods_effective"].split(","))) & SKIP_MODS)]


def read_maps(bundle: Path, ids: set[int], progress: Progress, total_files: int) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    for n, (name, data) in enumerate(_iter_path(bundle), 1):
        mt = _ID_NAME.match(Path(name).name)
        if mt and int(mt.group(1)) in ids:
            found[int(mt.group(1))] = data
        if n % 500 == 0:
            progress.update(add=500)
        if len(found) == len(ids):
            break
    progress.update(add=0, force=True)
    return found


def official_vs_local(rows: list[dict[str, Any]], maps: dict[int, bytes], progress: Progress) -> list[dict[str, Any]]:
    import rosu_pp_py as rosu

    beatmaps: dict[int, Any] = {}
    out = []
    for r in rows:
        data = maps.get(r["beatmap_id"])
        if data is None:
            progress.update(add=1)
            continue
        try:
            bm = beatmaps.get(r["beatmap_id"])
            if bm is None:
                if len(beatmaps) > 200:
                    beatmaps.clear()
                bm = beatmaps[r["beatmap_id"]] = rosu.Beatmap(content=data.decode("utf-8", errors="replace"))
            mods = [m for m in r["mods_effective"].split(",") if m]
            perf = rosu.Performance(mods=mods, n300=int(r["n_great"]), n100=int(r["n_ok"] or 0), n50=int(r["n_meh"] or 0),
                                    misses=int(r["n_miss"] or 0), combo=int(r["max_combo"]), lazer=False)
            ours = perf.calculate(bm).pp
        except Exception:
            progress.update(add=1)
            continue
        out.append({"score_id": r["score_id"], "beatmap_id": r["beatmap_id"], "mods": r["mods_effective"],
                    "official_pp": float(r["pp"]), "our_pp": float(ours), "accuracy": r["accuracy"]})
        progress.update(add=1)
    return out


def summarize(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np

    if not pairs:
        return {"n": 0}
    off = np.array([p["official_pp"] for p in pairs])
    our = np.array([p["our_pp"] for p in pairs])
    ok = off > 1
    rel = (our[ok] - off[ok]) / off[ok] * 100
    ab = np.abs(rel)

    def stats(a: np.ndarray, r: np.ndarray) -> dict[str, Any]:
        return {"n": int(len(r)), "median_abs_err_pct": round(float(np.median(np.abs(r))), 3) if len(r) else None,
                "p90_abs_err_pct": round(float(np.percentile(np.abs(r), 90)), 3) if len(r) else None,
                "within_1pct": round(float((np.abs(r) <= 1).mean()), 4) if len(r) else None,
                "within_5pct": round(float((np.abs(r) <= 5).mean()), 4) if len(r) else None,
                "mean_signed_err_pct": round(float(r.mean()), 3) if len(r) else None}

    mods = np.array([p["mods"] for p in pairs])[ok]
    groups = {"nomod": mods == "", "DT/NC": np.char.find(mods.astype(str), "DT") >= 0, "HD": np.char.find(mods.astype(str), "HD") >= 0,
              "HR": np.char.find(mods.astype(str), "HR") >= 0}
    return {"n": int(len(pairs)), "n_pp_over_1": int(ok.sum()), **stats(off[ok], rel),
            "pearson_r": round(float(np.corrcoef(off[ok], our[ok])[0, 1]), 5),
            "by_mods": {k: stats(off[ok][m], rel[m]) for k, m in groups.items() if m.any()},
            "worst": sorted(({"beatmap_id": int(b), "mods": md, "official": round(float(o), 1), "ours": round(float(u), 1)}
                             for b, md, o, u in zip(np.array([p["beatmap_id"] for p in pairs])[ok], mods, off[ok], our[ok])),
                            key=lambda d: -abs(d["ours"] - d["official"]) / max(d["official"], 1))[:8]}


def run_pp_check(score_files: list[Path], plan_path: Path, bundle: Path, out_dir: Path, version: str = "v1",
                 n_scores: int = 5000, seed: int = 42, progress_path: Path | None = None) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from ..beatmaps.catalog import load_plan

    wanted = set(load_plan(plan_path))
    total_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in score_files)
    total = total_rows + len(wanted) + n_scores
    prog = Progress(progress_path, "Validação do pp — 1/3 a amostrar scores", total, "passos")
    t0 = time.time()
    cands = sample_candidates(score_files, wanted, prog)
    random.Random(seed).shuffle(cands)
    chosen = cands[:n_scores]
    prog.update(label="Validação do pp — 2/3 a ler os mapas")
    maps = read_maps(bundle, {r["beatmap_id"] for r in chosen}, prog, len(wanted))
    prog.done = total_rows + len(wanted)  # a leitura pára quando encontra os mapas; salta para a fase seguinte
    prog.update(label="Validação do pp — 3/3 a calcular", force=True)
    pairs = official_vs_local(chosen, maps, prog)
    summary = summarize(pairs)

    target = out_dir / version
    target.mkdir(parents=True, exist_ok=True)
    path = target / "pp_check.parquet"
    pq.write_table(pa.Table.from_pylist(pairs), path)
    manifest = {"dataset_version": version, "file": path.name, "sampled": len(chosen), "candidates_available": len(cands),
                "maps_read": len(maps), "summary": summary, "seconds": round(time.time() - t0, 1),
                "created_at": datetime.now(timezone.utc).isoformat(), "score_files": [f.name for f in score_files],
                "notes": "Só scores legacy sem RX/AP/TD/speed_change. pp oficial = coluna `pp` do dump (2026-09-01). "
                         "rosu-pp-py com lazer=False."}
    (target / "manifest_pp_check.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return manifest
