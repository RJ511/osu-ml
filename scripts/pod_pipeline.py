"""Pipeline completa no pod RunPod (só CPU): descarregar dumps -> importar -> treinar -> empacotar. Nunca toca na osu!API.

Corre dentro do venv do pod (`osuml[parquet,ml]` instalado). Pasta de trabalho `$WORK` (omissão /root/work) com:
  inputs/   Parquet já tratados no PC (dump_scores_*, osu_user_beatmap_playcount_*, map_attributes, api_plays)
Cada tarefa escreve um `progress/<nome>.json` (formato de `osuml.progress.Progress`) que o painel principal lê por SSH:
downloads (bytes), importação de scores e de playcount por dump (bytes comprimidos lidos) e as fases de treino.
Estimativa de tempo: o painel calcula-a a partir da velocidade medida (computed_now / tempo decorrido).

Retomável: um download parcial continua com `Range`; importações cujo Parquet já existe (com manifest) não se repetem;
`--from-stage N` salta fases já concluídas.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from osuml.progress import Progress

WORK = Path(os.environ.get("WORK", "/root/work"))
PROG, LOGS, DUMPS, INPUTS = WORK / "progress", WORK / "logs", WORK / "dumps", WORK / "inputs"
DATA = WORK / "data"  # OSUML_DATA_DIR -> processed em DATA/processed
SNAPSHOTS = ["2026_08_01", "2026_07_13", "2026_06_01", "2026_05_01", "2026_04_01"]
URL = "https://data.ppy.sh/{s}_performance_osu_{k}.tar.bz2"
TOP_SNAP, TOP_KIND = "2026_09_01", "top_10000"  # +9 mil jogadores de topo (autorizado pelo utilizador)
THRESHOLDS = "0.85,0.88,0.90,0.93,0.95,0.97"
ENV = {**os.environ, "OSUML_DATA_DIR": str(DATA), "PYTHONUNBUFFERED": "1"}
VERSION = "full"
STAGES = ["download+import", "catálogo de mapas", "resumo dos dados", "modelo pass/fail", "modelo alcançável", "empacotar"]
_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _lock, (LOGS / "pipeline.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str], name: str, env: dict | None = None) -> None:
    """Corre `python -m osuml ...` num subprocesso com log próprio; levanta erro se falhar."""
    with (LOGS / f"{name}.log").open("ab") as f:
        rc = subprocess.run([sys.executable, "-m", "osuml", *cmd], stdout=f, stderr=subprocess.STDOUT, env={**ENV, **(env or {})}, cwd=WORK).returncode
    if rc != 0:
        raise RuntimeError(f"{name} falhou (código {rc}); ver logs/{name}.log")


def _fetch_chunk(url: str, part: Path, start: int, end: int, prog: Progress, plock: threading.Lock, tries: int = 12) -> None:
    want = end - start + 1
    for attempt in range(tries):
        have = part.stat().st_size if part.exists() else 0
        if have == want:
            return
        try:
            r = urllib.request.Request(url, headers={"User-Agent": "osuml-pod/1.0", "Range": f"bytes={start + have}-{end}"})
            with urllib.request.urlopen(r, timeout=120) as resp, part.open("ab") as out:
                if resp.status != 206:
                    raise RuntimeError(f"o servidor ignorou o Range (HTTP {resp.status})")
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    with plock:
                        prog.update(add=len(chunk))
        except Exception as e:
            log(f"bloco {part.name}: {e!r}; a retomar")
            time.sleep(min(30, 3 * (attempt + 1)))
    raise RuntimeError(f"bloco {part.name} falhou")


def download(snap: str, kind: str = "random_10000", conns: int = 1) -> Path:
    """Download com `conns` ligações paralelas (blocos de 32 MB com Range, retomáveis): a origem limita cada ligação (o top_10000 dá ~1 MB/s por ligação)."""
    url, dest = URL.format(s=snap, k=kind), DUMPS / f"{snap}_performance_osu_{kind}.tar.bz2"
    tag = snap if kind == "random_10000" else f"{snap}_{kind}"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "osuml-pod/1.0"})
    size = int(urllib.request.urlopen(req, timeout=60).headers["Content-Length"])
    prog = Progress(PROG / f"dl_{tag}.json", f"Download {tag}", size, "bytes")
    if dest.exists() and dest.stat().st_size == size:
        prog.finish(label=f"Download {tag} (já existia)")
        return dest
    step = 32 * 1024 * 1024
    parts_dir = dest.with_name(dest.name + ".parts")
    parts_dir.mkdir(exist_ok=True)
    ranges = [(k, k * step, min(size, (k + 1) * step) - 1) for k in range((size + step - 1) // step)]
    prog.update(sum(min(f.stat().st_size, step) for f in parts_dir.iterdir()), label=f"Download {tag} ({conns} ligações)")
    plock = threading.Lock()
    try:
        with ThreadPoolExecutor(conns) as ex:
            for f in [ex.submit(_fetch_chunk, url, parts_dir / f"{k:06d}", a, b, prog, plock) for k, a, b in ranges]:
                f.result()
    except Exception:
        prog.finish("error", f"Download {tag} falhou")
        raise
    prog.update(label=f"Download {tag} — a juntar blocos", force=True)
    with dest.open("wb") as out:
        for k, _, _ in ranges:
            with (parts_dir / f"{k:06d}").open("rb") as f:
                shutil.copyfileobj(f, out, 1 << 24)
    shutil.rmtree(parts_dir)
    if dest.stat().st_size != size:
        prog.finish("error", f"Download {tag}: tamanho final errado")
        raise RuntimeError(f"download {tag}: {dest.stat().st_size} != {size}")
    prog.finish(label=f"Download {tag}")
    return dest


def import_snapshot(snap: str, kind: str = "random_10000", workers: int = 1, conns: int = 1) -> None:
    tar = download(snap, kind, conns)
    stem = f"{snap}_{kind}"
    tag = snap if kind == "random_10000" else f"{snap}_{kind}"
    scores = DATA / "processed" / "dump_scores" / "v1" / f"dump_scores_{stem}.parquet"
    manifest = scores.with_name(f"manifest_dump_scores_{stem}.json")
    pc = DATA / "processed" / "dump_tables" / "v1" / f"osu_user_beatmap_playcount_{stem}.parquet"
    jobs = []
    with ThreadPoolExecutor(2) as ex:
        if not manifest.exists():
            jobs.append(ex.submit(run, ["dump-scores", "--tar", str(tar), "--progress", str(PROG / f"scores_{tag}.json"), "--workers", str(workers)], f"scores_{tag}"))
        if not (pc.exists() and pc.with_suffix(".progress.json").exists() and json.loads(pc.with_suffix(".progress.json").read_text())["status"] == "done"):
            jobs.append(ex.submit(run, ["dump-table", "--tar", str(tar), "--table", "osu_user_beatmap_playcount", "--progress",
                                        str(PROG / f"playcount_{tag}.json")], f"playcount_{tag}"))
        for j in jobs:
            j.result()
    tar.unlink(missing_ok=True)  # poupa disco (5 GB); um novo download só se tudo recomeçar
    log(f"{tag}: importado")
    for sub, pat in (("dump_scores", f"dump_scores_{stem}.parquet"), ("dump_tables", f"osu_user_beatmap_playcount_{stem}.parquet")):
        for f in (DATA / "processed" / sub / "v1").glob(pat):
            link = INPUTS / f.name
            if not link.exists():
                link.symlink_to(f)


def stage_import() -> None:
    with ThreadPoolExecutor(len(SNAPSHOTS)) as ex:
        for f in [ex.submit(import_snapshot, s) for s in SNAPSHOTS]:
            f.result()
    # junta as fontes novas (pod) às já tratadas no PC (inputs/) numa só pasta para os modelos
    for sub, pat in (("dump_scores", "dump_scores_*.parquet"), ("dump_tables", "osu_user_beatmap_playcount_*.parquet")):
        for f in (DATA / "processed" / sub / "v1").glob(pat):
            link = INPUTS / f.name
            if not link.exists():
                link.symlink_to(f)


def stage_catalog() -> None:
    """Atributos (stars/aim/speed/reading/...) de TODOS os mapas dos 7 dumps: os que têm scores + os só tentados (playcount).
    Os mapas que ninguém da amostra passou (os mais difíceis) só aparecem no playcount; sem eles o treino ficava enviesado para mapas fáceis."""
    osu_dump = WORK / "osu_files.tar.bz2"
    if not osu_dump.exists():
        raise FileNotFoundError("falta /root/work/osu_files.tar.bz2 (dump de .osu, enviado do PC)")
    import rosu_pp_py  # noqa: F401  (falha cedo se o extra `difficulty` não estiver instalado)

    plan = DATA / "processed" / "catalog" / "v2" / "plan.json"
    scores = sorted(str(f) for f in INPUTS.glob("dump_scores_*.parquet"))
    pcs = sorted(str(f) for f in INPUTS.glob("osu_user_beatmap_playcount_*.parquet"))
    if not plan.exists():
        pp = Progress(PROG / "catalog_1_plano.json", "Catálogo 1/3 — a escolher mapas (scores + playcount)", 1, "passo")
        run(["map-catalog", "plan", "--scores", *scores, "--playcounts", *pcs, "--top-n", "1000000", "--min-plays", "3", "--version", "v2"], "catalog_plan")
        pp.finish()
    log("plano: " + json.loads(plan.read_text())["meta"].__repr__()[:200] + f" ({len(json.loads(plan.read_text())['maps'])} mapas)")
    run(["map-catalog", "run", "--source", str(osu_dump), "--plan", str(plan), "--version", "v2", "--workers", str(os.cpu_count() or 4)], "catalog_run",
        env={"OSUML_PROGRESS_FILE": str(PROG / "catalog_2_calculo.json")})
    failed_f = DATA / "processed" / "catalog" / "v2" / "parts" / "failed.json"
    n_failed = len(json.loads(failed_f.read_text())) if failed_f.exists() else 0
    n_maps = len(json.loads(plan.read_text())["maps"])
    log(f"catálogo: {n_failed} mapas falhados/não-std de {n_maps}")
    # o playcount de osu! inclui converts de taiko/catch/mania (~35 % do plano), que o catálogo exclui por desenho (só osu!standard);
    # se falhar mais de metade é sinal de ambiente partido (p.ex. rosu-pp em falta: falha tudo em silêncio e o cálculo "acaba" em segundos)
    if n_failed > 0.5 * n_maps:
        raise RuntimeError(f"{n_failed}/{n_maps} mapas falharam no catálogo (>50 %): ver o ambiente (rosu-pp-py?)")
    pm_ = Progress(PROG / "catalog_3_juntar.json", "Catálogo 3/3 — a juntar partes", 1, "passo")
    run(["map-catalog", "merge", "--plan", str(plan), "--version", "v2"], "catalog_merge")
    pm_.finish()
    merged = DATA / "processed" / "catalog" / "v2" / "map_attributes.parquet"
    old = INPUTS / "map_attributes.parquet"
    if old.exists() and not old.is_symlink():
        old.rename(WORK / "map_attributes_v1.parquet")
    if old.exists() or old.is_symlink():
        old.unlink()
    old.symlink_to(merged)
    log(f"catálogo v2: {merged.stat().st_size / 1e6:.0f} MB")


def stage_catalog_and_top() -> None:
    """Catálogo de mapas e importação do `top_10000` ao mesmo tempo (o catálogo usa os 16 processos; o parsing do top usa 6)."""
    with ThreadPoolExecutor(2) as ex:
        fs = [ex.submit(stage_catalog), ex.submit(import_snapshot, TOP_SNAP, TOP_KIND, 6)]
        for f in fs:
            f.result()


def stage_summary() -> None:
    """Quantos jogadores/linhas há por fonte e quanto se repete entre dumps (as amostras aleatórias podem coincidir)."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    out: dict = {"scores": {}, "playcount": {}}
    users_by: dict[str, set] = {}
    for f in sorted(INPUTS.glob("dump_scores_*.parquet")):
        t = pq.read_table(f, columns=["user_id"])
        u = set(pc.unique(t.column("user_id")).to_pylist())
        users_by[f.stem] = u
        out["scores"][f.stem] = {"rows": t.num_rows, "users": len(u)}
    allu = set().union(*users_by.values()) if users_by else set()
    out["scores_unique_users"] = len(allu)
    rnd = [k for k in users_by if "random" in k]
    out["random_users_pairwise_overlap"] = {f"{a[-22:]} x {b[-22:]}": len(users_by[a] & users_by[b]) for i, a in enumerate(rnd) for b in rnd[i + 1:]}
    for f in sorted(INPUTS.glob("osu_user_beatmap_playcount_*.parquet")):
        out["playcount"][f.stem] = {"rows": pq.ParquetFile(f).metadata.num_rows}
    (WORK / "results").mkdir(exist_ok=True)
    (WORK / "results" / "data_summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    log("resumo: " + json.dumps({k: out[k] for k in ("scores_unique_users", "random_users_pairwise_overlap")}))


def stage_pass(threads: int, cap_train: int, cap_rows: int) -> None:
    run(["analyze", "pass-model", "--inputs", str(INPUTS), "--out-dir", str(WORK / "results" / "pass_model"), "--version", VERSION,
         "--progress", str(PROG / f"pass_model_{VERSION}.json"), "--threads", str(threads), "--seeds", "42,43,44", "--rounds", "600",
         "--cap-train", str(cap_train), "--cap-rows", str(cap_rows), "--players", "PXD Vieira=13745526", "gaaGOD=23994179"], "pass_model")


def stage_reach(threads: int, cap_train: int, cap_rows: int) -> None:
    run(["analyze", "reach-model", "--inputs", str(INPUTS), "--out-dir", str(WORK / "results" / "reach_model"), "--version", VERSION,
         "--progress", str(PROG / f"reach_model_{VERSION}.json"), "--threads", str(threads), "--rounds", "500", "--thresholds", THRESHOLDS,
         "--only-a", "--cap-train", str(cap_train), "--cap-rows", str(cap_rows)], "reach_model")


def stage_pack() -> None:
    subprocess.run(["tar", "-czf", str(WORK / "train_results.tar.gz"), "-C", str(WORK), "results"], check=True)
    # Parquet novos (playcount) voltam ao PC para reconstruir o índice de "jogadores parecidos"
    subprocess.run(["tar", "-cf", str(WORK / "new_playcount.tar"), "-C", str(DATA / "processed" / "dump_tables" / "v1")]
                   + [p.name for p in (DATA / "processed" / "dump_tables" / "v1").glob("osu_user_beatmap_playcount_*_random_10000.parquet")], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-stage", type=int, default=1)
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--cap-train", type=int, default=10_000_000)
    ap.add_argument("--cap-rows", type=int, default=800)
    ap.add_argument("--version", default="full", help="nome da pasta dos resultados (ex.: full, full_top)")
    ap.add_argument("--only-top", action="store_true", help="só descarrega+importa o top_10000 (processo à parte do treino)")
    a = ap.parse_args()
    global VERSION
    VERSION = a.version
    for d in (PROG, LOGS, DUMPS, INPUTS, DATA):
        d.mkdir(parents=True, exist_ok=True)
    if a.only_top:
        ov = Progress(PROG / "00_top10000.json", "Top 10000 — a começar", 1, "fase")
        try:
            import_snapshot(TOP_SNAP, TOP_KIND, workers=6, conns=24)
        except Exception as e:
            log(f"ERRO no top_10000: {e}")
            ov.finish("error", f"Top 10000 — ERRO: {e}")
            return 1
        ov.finish(label="Top 10000 — concluído")
        log("top_10000 importado (parquet em inputs/)")
        return 0
    overall = Progress(PROG / f"00_pipeline_{VERSION}.json" if VERSION != "full" else PROG / "00_pipeline.json", "Pipeline — a começar", len(STAGES), "fases")
    fns = [stage_import, stage_catalog, stage_summary, lambda: stage_pass(a.threads, a.cap_train, a.cap_rows),
           lambda: stage_reach(a.threads, a.cap_train, a.cap_rows), stage_pack]
    for i, fn in enumerate(fns, 1):
        if i < a.from_stage:
            continue
        overall.update(i - 1, label=f"Pipeline — fase {i}/{len(STAGES)}: {STAGES[i - 1]}", force=True)
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            log(f"ERRO na fase {i} ({STAGES[i - 1]}): {e}")
            overall.finish("error", f"Pipeline — ERRO na fase {i} ({STAGES[i - 1]}): {e}")
            return 1
        log(f"fase {i} ({STAGES[i - 1]}) concluída em {time.time() - t0:.0f}s")
    overall.finish(label="Pipeline — concluída")
    log("tudo pronto: train_results.tar.gz e new_playcount.tar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
