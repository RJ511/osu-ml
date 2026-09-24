"""Catálogo de atributos de mapas para os scores importados dos dumps (`external/scores_dump.py`).

Para cada mapa (os `top_n` mais jogados nos scores) e cada combinação de mods relevante, calcula com **um só
parse** do `.osu`: `stars/aim/speed` (rosu-pp), `reading` (port do lazer, `reading.py`), `density`,
`reading_visual`, `tech_entropy` e AR/CS/OD/HP. Zero pedidos à API. Corre em vários processos (Windows: spawn).

Feito para correr **em várias máquinas e ser retomado** (ex.: RunPod):
1. `plan`  — escolhe os pares (mapa, mods) a partir dos scores → `plan.json` (pequeno; a máquina remota não
   precisa dos scores).
2. `bundle` — extrai do dump só os `.osu` do plano → `osu_subset.tar.gz` (o que se envia para a máquina remota).
3. `run`   — calcula (opcionalmente só o shard `i/K`: `beatmap_id % K == i`), grava **partes** em
   `<out>/<versão>/parts/` de N em N mapas e, se for relançado, salta os mapas que já lá estão.
4. `merge` — junta as partes de todas as máquinas em `map_attributes.parquet` + manifest.

Mods normalizados para o que altera a dificuldade: `NC→DT`, `DC→HT`, e só `DT/HT/HR/EZ/HD/FL` contam
(os restantes, como NF/SD/PF/CL, não mudam nenhum atributo). Custom `speed_change` não é tratado.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import tarfile
import time
from collections import Counter, deque
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acquire import _ID_NAME, OSU_HEADER, _iter_path

RELEVANT = ("DT", "HT", "HR", "EZ", "HD", "FL")
_ALIAS = {"NC": "DT", "DC": "HT"}
PART_MAPS = 2000
MAP_BUDGET_S = 30.0  # tempo máximo por mapa (POSIX); um mapa degenerado não pode prender o lote todo


def attr_mods(mods: str | None) -> str:
    """`"CL,HD,NC"` -> `"DT,HD"` (só mods que mudam atributos, ordenados, NC->DT, DC->HT)."""
    got = {_ALIAS.get(x, x) for x in (mods or "").split(",") if x}
    if "DT" in got and "HT" in got:
        got.discard("HT")
    return ",".join(sorted(got & set(RELEVANT)))


# ------------------------------------------------------------------ plano
def choose_pairs(score_files: list[Path], top_n: int, min_plays: int, map_files: list[Path] | None = None) -> dict[int, list[str]]:
    """beatmap_id -> combinações de mods a calcular: os `top_n` mapas mais jogados; nomod sempre; cada
    combinação com >= `min_plays` scores nesse mapa.

    `map_files` (Parquet de playcount, coluna `beatmap_id`): os mapas tentados entram no plano mesmo sem nenhum passe nos scores.
    Sem isto, os mapas que ninguém da amostra passou (os mais difíceis) ficavam fora do catálogo e, por isso, fora do treino."""
    import pyarrow.parquet as pq

    per_map: Counter = Counter()
    per_pair: Counter = Counter()
    for f in score_files:
        t = pq.read_table(f, columns=["beatmap_id", "mods_effective"])
        g = t.group_by(["beatmap_id", "mods_effective"]).aggregate([([], "count_all")]).to_pydict()
        for bid, mods, n in zip(g["beatmap_id"], g["mods_effective"], g["count_all"]):
            per_map[bid] += n
            per_pair[(bid, attr_mods(mods))] += n
    for f in map_files or []:
        g = pq.read_table(f, columns=["beatmap_id"]).group_by(["beatmap_id"]).aggregate([([], "count_all")]).to_pydict()
        for bid, n in zip(g["beatmap_id"], g["count_all"]):
            per_map[bid] += n
    keep = {bid for bid, _ in per_map.most_common(top_n)}
    out: dict[int, set[str]] = {bid: {""} for bid in keep}
    for (bid, mods), n in per_pair.items():
        if bid in keep and n >= min_plays:
            out[bid].add(mods)
    return {bid: sorted(v) for bid, v in out.items()}


def save_plan(wanted: dict[int, list[str]], path: Path, meta: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta or {}, "maps": {str(k): v for k, v in wanted.items()}}), encoding="utf-8")


def load_plan(path: Path) -> dict[int, list[str]]:
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8"))["maps"].items()}


def in_shard(beatmap_id: int, shard: tuple[int, int] | None) -> bool:
    return shard is None or beatmap_id % shard[1] == shard[0]


def parse_shard(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    i, k = (int(x) for x in text.split("/"))
    if not (k >= 1 and 0 <= i < k):
        raise ValueError("--shard deve ser I/K com 0 <= I < K")
    return i, k


# ----------------------------------------------------------------- bundle
def bundle_osu(dump_path: Path, wanted: dict[int, list[str]], out_path: Path) -> dict[str, Any]:
    """Extrai do dump só os `.osu` do plano para um `.tar.gz` (nomes `<id>.osu`), para enviar a outra máquina."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    found: set[int] = set()
    with tarfile.open(out_path, "w:gz", compresslevel=6) as tf:
        for name, data in _iter_path(dump_path):
            mt = _ID_NAME.match(Path(name).name)
            if not mt or int(mt.group(1)) not in wanted or int(mt.group(1)) in found:
                continue
            if not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER):
                continue
            bid = int(mt.group(1))
            found.add(bid)
            info = tarfile.TarInfo(f"{bid}.osu")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return {"bundle": str(out_path), "maps_in_bundle": len(found), "maps_not_in_dump": len(wanted) - len(found),
            "bytes": out_path.stat().st_size}


# --------------------------------------------------------------- cálculo
def _on_alarm(signum: int, frame: Any) -> None:
    raise TimeoutError("orçamento de tempo por mapa excedido")


def compute_map(item: tuple[int, bytes, list[str]], budget_s: float = MAP_BUDGET_S) -> tuple[list[dict[str, Any]], int | None]:
    """(linhas por combinação de mods, beatmap_id com erro | None). Corre num processo-filho. Em POSIX, passado
    `budget_s` o mapa é abandonado (marcado como falhado) em vez de bloquear o lote."""
    use_alarm = hasattr(signal, "SIGALRM") and budget_s > 0
    if use_alarm:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, budget_s)
    try:
        return _compute_map(item)
    except Exception:
        return [], item[0]
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _compute_map(item: tuple[int, bytes, list[str]]) -> tuple[list[dict[str, Any]], int | None]:
    import rosu_pp_py as rosu

    from .hitfeatures import compute_from_parsed
    from .parser import parse_osu
    from .reading import reading_rating

    bid, data, combos = item
    try:
        text = data.decode("utf-8", errors="replace")
        pb = parse_osu(text)
        if pb.mode != 0:
            return [], bid
        beatmap = rosu.Beatmap(content=text)
        summ, hit = pb.summary(), compute_from_parsed(pb)
        length = None if summ["last_object_end_ms"] is None else summ["last_object_end_ms"] - (summ["first_object_ms"] or 0)
        rows = []
        for mods in combos:
            arg = mods.split(",") if mods else []
            a = rosu.Difficulty(mods=arg).calculate(beatmap)
            rows.append({"beatmap_id": bid, "mods": mods, "stars": a.stars, "aim": a.aim, "speed": a.speed,
                         "reading": reading_rating(pb, arg), "ar_mod": a.ar, "hp_mod": a.hp, "max_combo": a.max_combo,
                         "n_objects": summ["n_objects"], "n_circles": summ["n_circles"], "n_sliders": summ["n_sliders"],
                         "n_spinners": summ["n_spinners"], "cs": summ["cs"], "od": summ["od"], "ar": summ["ar"],
                         "hp": summ["hp"], "length_ms": length, **hit})
        return rows, None
    except Exception:
        return [], bid


def _done_ids(parts_dir: Path) -> set[int]:
    import pyarrow.parquet as pq

    done: set[int] = set()
    for f in parts_dir.glob("part_*.parquet"):
        done.update(pq.read_table(f, columns=["beatmap_id"]).column("beatmap_id").to_pylist())
    marker = parts_dir / "failed.json"
    if marker.exists():
        done.update(json.loads(marker.read_text(encoding="utf-8")))
    return done


def _write_progress(path: Path, **fields: Any) -> None:
    """Escrita atómica de `progress.json` (lida por uma janela de progresso, local ou por SSH)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**fields, "updated_at": time.time(), "pid": os.getpid()}), encoding="utf-8")
    os.replace(tmp, path)


def run_catalog(source: Path, wanted: dict[int, list[str]], out_dir: Path, version: str, *, shard: tuple[int, int] | None = None,
                workers: int = 3, part_maps: int = PART_MAPS, max_maps: int | None = None) -> dict[str, Any]:
    """Calcula os mapas do plano (só o shard pedido) a partir de `source` (dump `.tar.bz2`, `bundle` `.tar.gz`
    ou pasta), guardando partes; relançar salta o que já está guardado."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    parts_dir = out_dir / version / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    mine = {b: c for b, c in wanted.items() if in_shard(b, shard)}
    done = _done_ids(parts_dir)
    tag = f"s{shard[0]}of{shard[1]}" if shard else "all"
    progress_path = Path(os.environ["OSUML_PROGRESS_FILE"]) if os.environ.get("OSUML_PROGRESS_FILE") else parts_dir.parent / f"progress_{tag}.json"
    started = time.time()
    already = len(done & set(mine))
    rows: list[dict[str, Any]] = []
    failed: list[int] = []
    seen: set[int] = set()
    pending: deque[Future] = deque()
    n_part = len(list(parts_dir.glob(f"part_{tag}_*.parquet")))
    computed = 0

    def flush() -> None:
        nonlocal rows, n_part
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), parts_dir / f"part_{tag}_{n_part:05d}.parquet", compression="zstd")
            n_part += 1
            rows = []
        if failed:
            marker = parts_dir / "failed.json"
            old = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else []
            marker.write_text(json.dumps(sorted(set(old) | set(failed))), encoding="utf-8")

    def drain(limit: int) -> None:
        nonlocal computed
        while len(pending) > limit:
            r, err = pending.popleft().result()
            computed += 1
            if computed % 100 == 0:
                _write_progress(progress_path, label=f"Catálogo de mapas ({tag})", status="running", done=already + computed,
                                total=len(mine), unit="mapas", started_at=started, computed_now=computed)
            rows.extend(r)
            if err is not None:
                failed.append(err)
            if computed % part_maps == 0:
                flush()

    with ProcessPoolExecutor(max_workers=workers) as ex:
        for name, data in _iter_path(source):
            mt = _ID_NAME.match(Path(name).name)
            if not mt:
                continue
            bid = int(mt.group(1))
            if bid not in mine or bid in done or bid in seen:
                continue
            if not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER):
                continue
            seen.add(bid)
            pending.append(ex.submit(compute_map, (bid, data, mine[bid])))
            drain(workers * 8)
            if max_maps and len(seen) >= max_maps:
                break
        drain(0)
    flush()
    _write_progress(progress_path, label=f"Catálogo de mapas ({tag})", status="done", done=already + computed, total=len(mine),
                    unit="mapas", started_at=started, computed_now=computed)
    return {"shard": tag, "maps_in_shard": len(mine), "already_done": len(done & set(mine)), "computed_now": computed,
            "not_found_in_source": len(set(mine) - done - seen), "failed_or_not_std": len(failed), "parts_dir": str(parts_dir)}


def merge_catalog(out_dir: Path, version: str, wanted: dict[int, list[str]] | None = None) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    target = out_dir / version
    tables = [pq.read_table(f) for f in sorted((target / "parts").glob("part_*.parquet"))]
    if not tables:
        raise FileNotFoundError(f"sem partes em {target / 'parts'}")
    table = pa.concat_tables(tables)
    ids = table.column("beatmap_id").to_pylist()
    keys = list(zip(ids, table.column("mods").to_pylist()))
    dup = len(keys) - len(set(keys))
    if dup:  # o mesmo par calculado em duas máquinas: fica o primeiro
        seen: set = set()
        keep = [i for i, k in enumerate(keys) if not (k in seen or seen.add(k))]
        table = table.take(keep)
    path = target / "map_attributes.parquet"
    pq.write_table(table, path, compression="zstd")
    maps = set(table.column("beatmap_id").to_pylist())
    manifest = {
        "dataset_version": version, "file": path.name, "rows": table.num_rows, "maps": len(maps), "parts": len(tables),
        "duplicate_pairs_dropped": dup,
        "maps_in_plan": len(wanted) if wanted else None,
        "maps_missing_vs_plan": len(set(wanted) - maps) if wanted else None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Atributos por (mapa, mods relevantes: DT/HT/HR/EZ/HD/FL; NC=DT, DC=HT). Reading = port do lazer "
                 "(reading.py). Sem pedidos à API. 'missing' = fora do dump, não-std ou falhou. Ver beatmaps/catalog.py.",
    }
    (target / "manifest_map_attributes.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def build_catalog(dump_path: Path, score_files: list[Path], out_dir: Path, version: str, *, top_n: int = 50_000,
                  min_plays: int = 3, workers: int = 3, max_maps: int | None = None) -> dict[str, Any]:
    """Tudo numa máquina: plano -> cálculo (com partes) -> merge."""
    wanted = choose_pairs(score_files, top_n, min_plays)
    save_plan(wanted, out_dir / version / "plan.json", {"top_n": top_n, "min_plays": min_plays,
                                                        "score_files": [f.name for f in score_files]})
    run = run_catalog(dump_path, wanted, out_dir, version, workers=workers, max_maps=max_maps)
    return {**merge_catalog(out_dir, version, wanted), "run": run}
