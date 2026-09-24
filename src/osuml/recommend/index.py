"""Índice do recomendador (construído uma vez, lido por `core.Recommender`), tudo local, 0 pedidos à API:

- `index.npz`: `ids` (mapas do catálogo, ordenados), `x` (atributos nomod, ordem de `pass_model.MAP_FEATS`) e `axis` (nota aberta 50+20·z por
  eixo — aim, speed, stamina, reading, stars — contra a pool de referência v3);
- `cf_matrix.npz` + `cf_users.npy`: matriz jogadores×mapas (tentou/não tentou) dos dumps, para "jogadores parecidos";
- `labels.parquet`: artista, título, dificuldade e mapper de cada mapa (lidos do cabeçalho dos `.osu` do pacote do catálogo);
- `meta.json`: contagens, versão da pool, limiares dos modelos.
"""

from __future__ import annotations

import json
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..analysis import pass_model as pm
from ..progress import Progress

AXES = ("aim", "speed", "stamina", "reading", "stars")
_FIELDS = ("Title", "Artist", "Version", "Creator", "BeatmapSetID")


def axis_scores(catalog_path: Path, reference_pool: Path):
    """(ids ordenados, matriz (n, 5) de notas por eixo) para os mapas nomod do catálogo."""
    import numpy as np
    import pyarrow.parquet as pq

    from ..beatmaps.skills import ReferenceScale

    t = pq.read_table(catalog_path, columns=["beatmap_id", "mods", "stars", "aim", "speed", "reading", "density"]).to_pydict()
    keep = [i for i, m in enumerate(t["mods"]) if m == ""]
    ids = np.array([t["beatmap_id"][i] for i in keep], dtype=np.int64)
    scale = ReferenceScale.from_parquet(reference_pool)
    cols = {c: [t[c][i] for i in keep] for c in ("aim", "speed", "density", "reading", "stars")}
    field = {"aim": "aim", "speed": "speed", "stamina": "density", "reading": "reading", "stars": "stars"}
    out = np.array([[scale.score(field[a], float(v)) for v in cols[field[a]]] for a in AXES], dtype=np.float32).T
    order = np.argsort(ids)
    return ids[order], out[order]


def read_labels(bundle: Path, wanted: set[int], progress: Progress | None = None) -> dict[int, dict[str, Any]]:
    """Título/artista/dificuldade/mapper a partir do cabeçalho dos `.osu` (lê só os primeiros KB de cada ficheiro)."""
    labels: dict[int, dict[str, Any]] = {}
    with tarfile.open(bundle, "r|*") as tf:
        for n, member in enumerate(tf, 1):
            if not member.isfile() or not member.name.endswith(".osu"):
                continue
            try:
                bid = int(Path(member.name).stem)
            except ValueError:
                continue
            if bid not in wanted:
                continue
            head = tf.extractfile(member).read(6000).decode("utf-8", errors="replace")
            d: dict[str, Any] = {}
            for line in head.splitlines():
                k, _, v = line.partition(":")
                if k in _FIELDS and k not in d:
                    d[k] = v.strip()
            labels[bid] = {"title": d.get("Title", ""), "artist": d.get("Artist", ""), "version": d.get("Version", ""),
                           "creator": d.get("Creator", ""), "set_id": int(d["BeatmapSetID"]) if d.get("BeatmapSetID", "").lstrip("-").isdigit() else None}
            if progress and n % 2000 == 0:
                progress.update(n)
    return labels


def build_index(inputs: Path, reference_pool: Path, bundle: Path | None, out_dir: Path, *, sample_pct: int = 70,
                progress_path: Path | None = None) -> dict[str, Any]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    import scipy.sparse as sp

    t0 = time.time()
    playcounts = sorted(inputs.glob("osu_user_beatmap_playcount_*.parquet"))
    catalog = inputs / "map_attributes.parquet"
    if not playcounts or not catalog.exists():
        raise FileNotFoundError("faltam ficheiros em " + str(inputs))
    prog = Progress(progress_path, "Recomendador — a construir o índice", 100, "passos")
    out_dir.mkdir(parents=True, exist_ok=True)

    ids, axis = axis_scores(catalog, reference_pool)
    cat_ids, cat_x = pm.load_catalog(catalog)
    assert (cat_ids == ids).all(), "catálogo e notas por eixo não coincidem"
    np.savez_compressed(out_dir / "index.npz", ids=ids, x=cat_x, axis=axis)
    prog.update(10, label="Recomendador — a construir a matriz de jogadores parecidos", force=True)

    users, bids, attempts, is_top = pm.load_pairs(playcounts, sample_pct=sample_pct)
    pos = np.searchsorted(ids, bids)
    pos[pos >= len(ids)] = 0
    ok = ids[pos] == bids
    users, pos = users[ok], pos[ok]
    uniq, uidx = np.unique(users, return_inverse=True)
    m = sp.csr_matrix((np.ones(len(uidx), dtype=np.float32), (uidx, pos)), shape=(len(uniq), len(ids)))
    sp.save_npz(out_dir / "cf_matrix.npz", m)
    np.save(out_dir / "cf_users.npy", uniq)
    prog.update(50, label="Recomendador — a ler os nomes dos mapas", force=True)

    labels: dict[int, dict[str, Any]] = {}
    if bundle is not None and Path(bundle).exists():
        labels = read_labels(Path(bundle), set(ids.tolist()))
    rows = [{"beatmap_id": int(b), **labels.get(int(b), {"title": "", "artist": "", "version": "", "creator": "", "set_id": None})} for b in ids]
    pq.write_table(pa.Table.from_pylist(rows), out_dir / "labels.parquet")
    meta = {"created_at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1), "maps": int(len(ids)),
            "cf_players": int(len(uniq)), "cf_pairs": int(len(uidx)), "cf_sample_pct": sample_pct, "labels": len(labels),
            "reference_pool": str(reference_pool), "axes": list(AXES)}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    prog.finish()
    return meta
