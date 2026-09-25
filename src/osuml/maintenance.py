"""Manutenção periódica do recomendador (0 pedidos à osu!API): o que as tarefas agendadas correm.

- `monthly-sim`  — 1×/mês: simula a recalibração (não altera nada) e escreve um relatório em `data/reports/manutencao/AAAA-MM.{json,txt}`.
- `dumps-check`  — diário, mas só age na 1.ª semana do mês: procura no data.ppy.sh um dump novo completo (random_10000 + top_10000 + osu_files da mesma data);
                   se houver, escreve `data/control/retrain_pending.json` (o retreino é feito a seguir, ver `docs/retreino_mensal.md`). Só faz 1 pedido HTTP à listagem do
                   data.ppy.sh (não é a osu!API).
- `pack-check`   — semanal: decide se o pacote do S3 está desatualizado de forma que valha a pena (modelo/calibração/índice mudaram, ou a correção de um jogador do pacote
                   mudou de forma material) e, com `--upload`, reconstrói-o e envia-o para o bucket privado.
- `retrain-plan` / `retrain-finish` — as partes locais do retreino (o treino em si corre num pod RunPod).
- `status`       — estado de tudo isto.

Estado em `data/control/maintenance_state.json`; cada execução acrescenta uma linha a `data/logs/maintenance.jsonl`.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

DUMPS_URL = "https://data.ppy.sh/"
WINDOW_DAYS = 7  # o dump sai no dia 1; procura-se na 1.ª semana
DEFAULT_PACK_PLAYERS = ["PXD Vieira", "gaaGOD"]
MIN_DAYS_BETWEEN_UPLOADS = 7  # sem modelo novo, não se reenvia o pacote mais vezes do que isto
ACC_DELTA = 0.01          # 1 ponto de accuracy
PASS_DELTA = 0.15         # no logit de P(passar)
# treinado até aqui (verificado em 2026-09-25): 6 dumps aleatórios + top_10000 de 2026_09_01
SEED_STATE = {"trained_snapshot": "2026_09_01", "random_snapshots": ["2026_04_01", "2026_05_01", "2026_06_01", "2026_07_13", "2026_08_01", "2026_09_01"],
              "top_snapshot": "2026_09_01"}

_PERF = re.compile(r"(\d{4}_\d{2}_\d{2})_performance_osu_(random_10000|top_10000)\.tar\.bz2")
_FILES = re.compile(r"(\d{4}_\d{2}_\d{2})_osu_files\.tar\.bz2")


# ------------------------------------------------------------------ estado e registo
def control_dir(settings) -> Path:
    d = settings.data_dir / "control"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(settings) -> dict[str, Any]:
    f = control_dir(settings) / "maintenance_state.json"
    state: dict[str, Any] = {}
    if f.exists():
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    for k, v in SEED_STATE.items():
        state.setdefault(k, v)
    return state


def save_state(settings, state: dict[str, Any]) -> None:
    (control_dir(settings) / "maintenance_state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def log_event(settings, event: dict[str, Any]) -> None:
    d = settings.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "maintenance.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}, ensure_ascii=False, default=str) + "\n")


def _models_dir(settings) -> Path:
    return settings.processed_dir / "recommend" / "models"


def _index_dir(settings) -> Path:
    return settings.processed_dir / "recommend" / "index"


# ------------------------------------------------------------------ dumps novos
def fetch_listing(url: str = DUMPS_URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "osuml-maintenance/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def complete_snapshots(listing: str) -> list[str]:
    """Datas (AAAA_MM_DD, por ordem) que têm os três ficheiros de que o treino precisa: random_10000, top_10000 e osu_files."""
    perf: dict[str, set[str]] = {}
    for d, kind in _PERF.findall(listing):
        perf.setdefault(d, set()).add(kind)
    files = set(_FILES.findall(listing))
    return sorted(d for d, kinds in perf.items() if kinds >= {"random_10000", "top_10000"} and d in files)


def dumps_check(settings, *, today: date | None = None, window_days: int = WINDOW_DAYS, force: bool = False,
                fetch: Callable[[], str] = fetch_listing) -> dict[str, Any]:
    """Procura um dump novo. Só age na 1.ª semana do mês (a menos que `force`); se já há um retreino pendente para essa data, não repete."""
    today = today or date.today()
    state = load_state(settings)
    pending_f = control_dir(settings) / "retrain_pending.json"
    out: dict[str, Any] = {"today": today.isoformat(), "trained_snapshot": state["trained_snapshot"], "checked": False, "new": False}
    if today.day > window_days and not force:
        out["reason"] = f"fora da janela (só na 1.ª semana do mês: dias 1–{window_days})"
        return out
    listing = fetch()
    snaps = complete_snapshots(listing)
    out["checked"] = True
    latest = snaps[-1] if snaps else None
    out["latest_complete_snapshot"] = latest
    if latest is None or latest <= state["trained_snapshot"]:
        out["reason"] = "sem dump novo completo" if latest else "listagem sem dumps completos"
        state["dumps"] = {"last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "latest": latest}
        save_state(settings, state)
        log_event(settings, {"task": "dumps-check", **out})
        return out
    if pending_f.exists() and json.loads(pending_f.read_text(encoding="utf-8")).get("snapshot") == latest:
        out.update(new=True, reason="retreino já pendente para este dump")
        return out
    pending = {"snapshot": latest, "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "previous_trained": state["trained_snapshot"],
               "random_snapshots": [*state["random_snapshots"], latest], "top_snapshot": latest, "osu_files_snapshot": latest,
               "guide": "docs/retreino_mensal.md"}
    pending_f.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
    out.update(new=True, pending=pending)
    state["dumps"] = {"last_checked": pending["found_at"], "latest": latest}
    save_state(settings, state)
    log_event(settings, {"task": "dumps-check", **{k: v for k, v in out.items() if k != "pending"}, "pending_snapshot": latest})
    return out


# ------------------------------------------------------------------ simulação mensal
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f} %"


def monthly_sim(settings, store, *, now: datetime | None = None) -> dict[str, Any]:
    """Simula a recalibração (nunca grava a calibração) e escreve o relatório do mês."""
    from .recommend.adjust import compute_adjustments, recalibrate
    from .recommend.log import report

    now = now or datetime.now()
    models = _models_dir(settings)
    sim = recalibrate(store, models, apply=False)
    rep = report(store)
    adj = compute_adjustments(store, models)
    out = {"month": now.strftime("%Y-%m"), "generated_at": now.isoformat(timespec="seconds"), "recalibration": sim, "report": rep,
           "player_adjustments": {"players": len(adj), "max_acc_bias_pts": round(max((abs(a["acc_bias"]) for a in adj.values()), default=0.0) * 100, 2),
                                  "max_pass_offset": round(max((abs(a["pass_offset"]) for a in adj.values()), default=0.0), 3)}}
    lines = [f"Manutenção mensal do recomendador — {out['month']} (simulação: nada foi alterado)", "=" * 70]
    p = rep.get("pass") or {}
    if p:
        lines.append(f"P(passar) (lazer, {p['n']} pares): previsto {_pct(p['predicted_mean'])} · observado {_pct(p['observed'])} · Brier {p['brier']}")
    a = rep.get("accuracy") or {}
    if a:
        lines.append(f"Accuracy ao passar ({a['n']} pares): viés previsto−real {a['bias_pred_minus_real'] * 100:+.2f} pts · erro médio {a['mae'] * 100:.2f} pts")
    lines.append(f"Correção por jogador: {len(adj)} jogadores com ajuste (máx. {out['player_adjustments']['max_acc_bias_pts']} pts de accuracy)")
    lines.append("")
    if sim.get("reason") and "pass" not in sim:
        lines.append("Recalibração: " + sim["reason"])
    else:
        pp = sim["pass"]
        lines.append(f"Recalibração de P(passar): Brier {pp['current']['brier']} → {pp['new']['brier_cv']} (validação cruzada), ECE {pp['current']['ece']} → {pp['new']['ece_cv']}")
        if "acc" in sim:
            lines.append(f"Recalibração da accuracy: erro médio {sim['acc']['mae_current'] * 100:.2f} → {sim['acc']['mae_new_cv'] * 100:.2f} pts")
        better = sim["improves"]["pass"] or sim["improves"]["acc"]
        lines.append("DECISÃO: " + ("compensa — correr `python -m osuml recommend recalibrate --apply` (guarda cópia da anterior)" if better
                                    else "não compensa (ganho abaixo do limiar); a calibração atual mantém-se"))
    text = "\n".join(lines) + "\n"
    d = settings.data_dir / "reports" / "manutencao"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{out['month']}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (d / f"{out['month']}.txt").write_text(text, encoding="utf-8")
    out["report_file"] = str(d / f"{out['month']}.txt")
    log_event(settings, {"task": "monthly-sim", "month": out["month"], "improves": sim.get("improves"), "n_pairs_lazer": sim.get("n_pairs_lazer")})
    return out


# ------------------------------------------------------------------ pacote S3
def _index_signature(index_dir: Path) -> str:
    meta = index_dir / "meta.json"
    if not meta.exists():
        return ""
    d = json.loads(meta.read_text(encoding="utf-8"))
    return f"{d.get('created_at')}|{d.get('maps')}"


def pack_snapshot(settings, store, players: list[str]) -> dict[str, Any]:
    """O que define o pacote atual: modelos, calibração global, índice e a correção de cada jogador do pacote."""
    import hashlib

    from sqlalchemy import select

    from .recommend.adjust import load_adjustments
    from .recommend.core import models_fingerprint
    from .storage import models as m

    models = _models_dir(settings)
    cal = models / "calibration_pass_acc.json"
    cal_sha = hashlib.sha256(json.dumps({k: v for k, v in json.loads(cal.read_text(encoding="utf-8")).items() if k in ("pass", "acc_shift")}, sort_keys=True).encode()).hexdigest()[:12] if cal.exists() else ""
    wanted = {p.strip().lower() for p in players}
    with store.engine.connect() as c:
        ids = {int(u): n for u, n in c.execute(select(m.users.c.user_id, m.users.c.username)).all() if n.lower() in wanted or str(u) in wanted}
    adj = load_adjustments(models)
    return {"model": models_fingerprint(models), "calibration": cal_sha, "index": _index_signature(_index_dir(settings)),
            "adjust": {n: {"acc_bias": (adj.get(u) or {}).get("acc_bias", 0.0), "pass_offset": (adj.get(u) or {}).get("pass_offset", 0.0)} for u, n in ids.items()}}


def pack_pertinence(prev: dict[str, Any] | None, cur: dict[str, Any], *, last_upload: datetime | None = None, now: datetime | None = None) -> tuple[bool, list[str]]:
    """(vale a pena enviar?, motivos). Modelo/calibração/índice novos justificam logo; só a correção por jogador exige mudança material e >= 7 dias desde o último envio."""
    if not prev:
        return True, ["o pacote nunca foi enviado por esta rotina"]
    reasons = [f"{label} mudou" for key, label in (("model", "modelo"), ("calibration", "calibração"), ("index", "índice")) if prev.get(key) != cur.get(key)]
    if reasons:
        return True, reasons
    moved = []
    for name, c in cur["adjust"].items():
        p = prev.get("adjust", {}).get(name, {"acc_bias": 0.0, "pass_offset": 0.0})
        da, dp = abs(c["acc_bias"] - p["acc_bias"]), abs(c["pass_offset"] - p["pass_offset"])
        if da >= ACC_DELTA or dp >= PASS_DELTA:
            moved.append(f"{name}: accuracy {da * 100:.1f} pts, P(passar) {dp:.2f}")
    if not moved:
        return False, ["nada mudou de forma material desde o último envio"]
    now = now or datetime.now(timezone.utc)
    if last_upload is not None and (now - last_upload).days < MIN_DAYS_BETWEEN_UPLOADS:
        return False, [f"correção mudou ({'; '.join(moved)}) mas o último envio foi há menos de {MIN_DAYS_BETWEEN_UPLOADS} dias"]
    return True, ["correção por jogador mudou: " + "; ".join(moved)]


def pack_check(settings, store, *, upload: bool = False, players: list[str] | None = None, uploader: Callable[..., dict] | None = None,
               now: datetime | None = None) -> dict[str, Any]:
    """Decide se o pacote do S3 está desatualizado; com `upload`, reconstrói-o e envia-o (bucket privado, AES256, tamanho confirmado)."""
    state = load_state(settings)
    players = players or state.get("pack", {}).get("players") or DEFAULT_PACK_PLAYERS
    cur = pack_snapshot(settings, store, players)
    prev = state.get("pack")
    last = None
    if prev and prev.get("uploaded_at"):
        last = datetime.fromisoformat(prev["uploaded_at"])
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    pertinent, reasons = pack_pertinence((prev or {}).get("snapshot") if prev else None, cur, last_upload=last, now=now)
    out: dict[str, Any] = {"pertinent": pertinent, "reasons": reasons, "current": cur, "uploaded": False}
    if not (pertinent and upload):
        log_event(settings, {"task": "pack-check", "pertinent": pertinent, "reasons": reasons, "uploaded": False})
        return out
    from .recommend.pack import build_pack

    when = (now or datetime.now(timezone.utc))
    stem = f"osuml-pack-{cur['model'] or 'sem-modelo'}-{when:%Y%m%d}"
    dist = Path("dist")
    out_zip = dist / f"{stem}.zip"
    built = build_pack(store, _index_dir(settings), _models_dir(settings), out_zip, players, note=f"pack-check {when:%Y-%m-%d}: " + "; ".join(reasons))
    if uploader is None:
        from .storage.s3 import upload_pack

        def uploader(zip_path, key):  # noqa: E306
            return upload_pack(Path(zip_path), settings.s3_bucket, settings.s3_region, key)
    key = f"recommend/{stem}.zip"
    res = uploader(out_zip, key)
    out.update(uploaded=True, key=key, bytes=built["bytes"], sha256=built["sha256"], upload=res)
    state["pack"] = {"players": players, "snapshot": cur, "key": key, "sha256": built["sha256"], "bytes": built["bytes"], "uploaded_at": when.isoformat(timespec="seconds")}
    save_state(settings, state)
    log_event(settings, {"task": "pack-check", "pertinent": True, "reasons": reasons, "uploaded": True, "key": key, "sha256": built["sha256"]})
    return out


# ------------------------------------------------------------------ retreino (partes locais)
def retrain_plan(settings) -> dict[str, Any]:
    """O que enviar ao pod e com que argumentos correr `scripts/pod_pipeline.py`. Não altera nada."""
    pf = control_dir(settings) / "retrain_pending.json"
    if not pf.exists():
        return {"pending": False}
    pend = json.loads(pf.read_text(encoding="utf-8"))
    state = load_state(settings)
    snap, top_old = pend["snapshot"], state["top_snapshot"]
    dsc, dtb = settings.processed_dir / "dump_scores" / "v1", settings.processed_dir / "dump_tables" / "v1"
    upload = []
    for s in state["random_snapshots"]:  # os aleatórios antigos já tratados seguem do PC; só o novo se descarrega no pod
        upload += [dsc / f"dump_scores_{s}_random_10000.parquet", dtb / f"osu_user_beatmap_playcount_{s}_random_10000.parquet"]
    upload += [dsc / f"dump_scores_{top_old}_top_1000.parquet", dtb / f"osu_user_beatmap_playcount_{top_old}_top_1000.parquet"]
    missing = [str(p) for p in upload if not p.exists()]
    return {"pending": True, "snapshot": snap, "random_snapshots_all": pend["random_snapshots"], "download_on_pod": {"random": snap, "top_10000": snap, "osu_files": snap},
            "upload_to_pod_inputs": [str(p) for p in upload if p.exists()], "missing_local_files": missing,
            "api_plays": "exportar com `python -m osuml analyze export-api-plays --out <pasta>/api_plays.parquet`",
            "pipeline_args": f"--random-snaps {snap} --top-snap {snap} --osu-files-snap {snap}",
            "outputs_expected": "retrain_outputs.tar no pod (ver docs/retreino_mensal.md)"}


def retrain_prepare(settings, store) -> dict[str, Any]:
    """Prepara a pasta a enviar ao pod: `osuml_src.zip` (o código atual), `pod_pipeline.py`, `runpod_full_train.sh` e `inputs/api_plays.parquet` (scores da BD local,
    0 pedidos). Os Parquet grandes vêm de `retrain_plan`. Devolve os caminhos."""
    import zipfile

    from .analysis.pass_model import export_api_plays

    root = Path(__file__).resolve().parents[2]  # raiz do repositório
    out = settings.processed_dir / "maintenance" / "retrain"
    (out / "inputs").mkdir(parents=True, exist_ok=True)
    zpath = out / "osuml_src.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for extra in ("pyproject.toml", "README.md"):
            if (root / extra).exists():
                z.write(root / extra, extra)
        for f in sorted((root / "src").rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f.relative_to(root))
    for name in ("pod_pipeline.py", "runpod_full_train.sh"):
        shutil.copy2(root / "scripts" / name, out / name)
    export_api_plays(store, out / "inputs" / "api_plays.parquet")
    return {"folder": str(out), "files": {"osuml_src.zip": str(zpath), "pod_pipeline.py": str(out / "pod_pipeline.py"), "runpod_full_train.sh": str(out / "runpod_full_train.sh"),
                                          "inputs/api_plays.parquet": str(out / "inputs" / "api_plays.parquet")}}


def _metrics(res_dir: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {"auc": None, "mae": None}
    pf, af = res_dir / "pass_model_results.json", res_dir / "acc_model_results.json"
    if pf.exists():
        r = json.loads(pf.read_text(encoding="utf-8")).get("results", {})
        out["auc"] = (r.get("A") or {}).get("all", {}).get("auc")
    if af.exists():
        r = json.loads(af.read_text(encoding="utf-8")).get("results", {})
        out["mae"] = (r.get("model") or {}).get("mae")
    return out


def retrain_finish(settings, store, outputs: Path, snapshot: str, *, osu_files: Path | None = None, sample_pct: int = 50, max_auc_drop: float = 0.02,
                   max_mae_rise: float = 0.004) -> dict[str, Any]:
    """Instala os modelos novos vindos do pod, com validação e sem apagar nada do que existe:
    1. compara AUC/MAE com o treino atual (recusa se piorar mais do que `max_*`);
    2. reconstrói o índice em `index_new` e junta os modelos em `models_new` (com a calibração atual como ponto de partida);
    3. recalibra P(passar) com os jogadores da API **depois** do dump (senão haveria fuga); se houver poucos pares, mantém a calibração anterior;
    4. troca as pastas (as antigas ficam como `*_prev_<data>`), atualiza o estado e limpa o pedido pendente.
    `outputs`: pasta extraída de `retrain_outputs.tar` (models/, results/, parquet/, catalog/)."""
    from .analysis.pass_calibration import run_pass_calibration
    from .recommend.index import build_index

    outputs = Path(outputs)
    rdir = settings.processed_dir / "recommend"
    models, index = rdir / "models", rdir / "index"
    stamp = datetime.now().strftime("%Y%m%d")
    out: dict[str, Any] = {"snapshot": snapshot, "installed": False}
    for f in ("models/pass_model_A.txt", "models/acc_pass_A.txt"):
        if not (outputs / f).exists():
            raise FileNotFoundError(f"falta {outputs / f}")
    old = json.loads((models / "training.json").read_text(encoding="utf-8")) if (models / "training.json").exists() else {}
    new = _metrics(outputs / "results")
    out["metrics"] = {"new": new, "old": {"auc": (old.get("pass_model") or {}).get("auc"), "mae": (old.get("acc_model") or {}).get("mae")}}
    o_auc, o_mae = out["metrics"]["old"]["auc"], out["metrics"]["old"]["mae"]
    if new["auc"] is None or new["mae"] is None:
        raise RuntimeError("faltam as métricas do treino novo (results/pass_model_results.json e acc_model_results.json)")
    if o_auc is not None and new["auc"] < o_auc - max_auc_drop:
        out["refused"] = f"AUC do modelo novo ({new['auc']:.3f}) pior do que o atual ({o_auc:.3f}) em mais de {max_auc_drop}"
    if o_mae is not None and new["mae"] > o_mae + max_mae_rise:
        out["refused"] = (out.get("refused", "") + f" MAE da accuracy novo ({new['mae']:.4f}) pior do que o atual ({o_mae:.4f}) em mais de {max_mae_rise}").strip()
    if out.get("refused"):
        (control_dir(settings) / "retrain_failed.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        log_event(settings, {"task": "retrain-finish", **{k: out[k] for k in ("snapshot", "refused")}})
        return out
    # dados novos -> pastas do projeto
    for f in sorted((outputs / "parquet").glob("*.parquet")) if (outputs / "parquet").exists() else []:
        dest = settings.processed_dir / ("dump_scores" if f.name.startswith("dump_scores_") else "dump_tables") / "v1" / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    cat = settings.processed_dir / "catalog" / "v2" / "map_attributes.parquet"
    if cat.exists():
        shutil.copy2(cat, cat.with_name(f"map_attributes_prev_{stamp}.parquet"))
    shutil.copy2(outputs / "catalog" / "map_attributes.parquet", cat)  # o Explorar e o índice leem daqui
    inputs = settings.processed_dir / "analysis" / f"inputs_{snapshot}"
    if inputs.exists():
        shutil.rmtree(inputs)
    inputs.mkdir(parents=True)
    for f in sorted((settings.processed_dir / "dump_tables" / "v1").glob("osu_user_beatmap_playcount_*.parquet")):
        if "_top_10000" in f.name and snapshot not in f.name:
            continue  # o top_10000 antigo é substituído pelo novo (mesmos jogadores, scores mais recentes): não contar duas vezes
        _link(f, inputs / f.name)
    _link(cat, inputs / "map_attributes.parquet")
    index_new, models_new = rdir / "index_new", rdir / "models_new"
    for d in (index_new, models_new):
        shutil.rmtree(d, ignore_errors=True)
    pool = settings.processed_dir / "v3" / "reference_pool_v3.parquet"
    out["index"] = build_index(inputs, pool, osu_files, index_new, sample_pct=sample_pct, progress_path=index_new / "progress.json")
    models_new.mkdir(parents=True)
    for f in ("pass_model_A.txt", "acc_pass_A.txt"):
        shutil.copy2(outputs / "models" / f, models_new / f)
    if (models / "calibration_pass_acc.json").exists():
        shutil.copy2(models / "calibration_pass_acc.json", models_new / "calibration_pass_acc.json")  # ponto de partida
    summary = {"pass_model": {"auc": new["auc"]}, "acc_model": {"mae": new["mae"]}, "snapshot": snapshot, "created_at": datetime.now(timezone.utc).isoformat()}
    (models_new / "training.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    cutoff = f"{snapshot[:4]}-{snapshot[5:7]}-{snapshot[8:10]}"
    try:  # jogadores da API só DEPOIS do dump: senão os passes do treino contaminam a calibração
        cal = run_pass_calibration(store, index_new, models_new, settings.processed_dir / "analysis" / "pass_calibrate", f"r{snapshot}", cutoff=cutoff)
        out["calibration"] = {"refit": True, "ece_cv": cal["pass"]["ece_calibrated_cv"], "brier_cv": cal["pass"]["brier_calibrated_cv"]}
    except RuntimeError as exc:  # poucos pares fora do tempo (1.ª semana do mês): fica a calibração anterior
        out["calibration"] = {"refit": False, "reason": str(exc)}
    for cur, new_dir in ((models, models_new), (index, index_new)):
        prev = cur.with_name(f"{cur.name}_prev_{stamp}")
        shutil.rmtree(prev, ignore_errors=True)
        cur.rename(prev)
        new_dir.rename(cur)
    try:  # os ajustes por jogador estimam-se com previsões do MESMO modelo: refaz-se a avaliação-sombra do último trimestre com o modelo novo
        from datetime import timedelta

        from .recommend import Recommender
        from .recommend.adjust import refresh_adjustments
        from .recommend.log import evaluate_pending

        rec = Recommender(store, index, models)
        out["shadow_backfill"] = evaluate_pending(store, rec, since=datetime.now() - timedelta(days=90), batch="day")
        out["player_adjust"] = refresh_adjustments(store, models)
    except Exception as exc:  # noqa: BLE001 — o modelo já está instalado; isto refaz-se com `osuml eval-log --since ... --batch day`
        out["shadow_backfill"] = {"error": f"{type(exc).__name__}: {exc}"}
    state = load_state(settings)
    state.update(trained_snapshot=snapshot, random_snapshots=sorted({*state["random_snapshots"], snapshot}), top_snapshot=snapshot)
    save_state(settings, state)
    (control_dir(settings) / "retrain_pending.json").unlink(missing_ok=True)
    (control_dir(settings) / "retrain_failed.json").unlink(missing_ok=True)
    out["installed"] = True
    log_event(settings, {"task": "retrain-finish", "snapshot": snapshot, "installed": True, "metrics": out["metrics"], "calibration": out["calibration"]})
    return out


def _link(src: Path, dst: Path) -> None:
    import os

    try:
        os.link(src, dst)  # sem copiar (mesmo disco)
    except OSError:
        shutil.copy2(src, dst)


def status(settings, store=None) -> dict[str, Any]:
    state = load_state(settings)
    cd = control_dir(settings)
    out: dict[str, Any] = {"trained_snapshot": state["trained_snapshot"], "dumps": state.get("dumps"), "pack": {k: v for k, v in (state.get("pack") or {}).items() if k != "snapshot"},
                           "retrain_pending": json.loads((cd / "retrain_pending.json").read_text(encoding="utf-8")) if (cd / "retrain_pending.json").exists() else None,
                           "retrain_failed": json.loads((cd / "retrain_failed.json").read_text(encoding="utf-8")) if (cd / "retrain_failed.json").exists() else None}
    reports = sorted((settings.data_dir / "reports" / "manutencao").glob("*.txt")) if (settings.data_dir / "reports" / "manutencao").exists() else []
    out["last_monthly_report"] = str(reports[-1]) if reports else None
    return out
