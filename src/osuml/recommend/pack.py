"""Pacote de dados do recomendador (`osuml recommend pack`): tudo o que a aplicação precisa para funcionar noutra máquina, sem API e sem
credenciais, num único `.zip` que se descompacta numa pasta (`pack/`).

Conteúdo: `index/` (notas por eixo dos mapas, matriz de "jogadores parecidos", nomes dos mapas), `models/` (modelos de accuracy) e
`players.db` com **só os jogadores pedidos** e **só as colunas de que o recomendador precisa** (sem o JSON cru da API).

**Licença**: o índice e os modelos derivam dos dumps do data.ppy.sh, cuja licença só permite análise estatística e não exposição pública
sem autorização do ppy — por isso o pacote é para uso privado e NÃO deve ir como ficheiro de uma release pública do repositório.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INDEX_FILES = ("index.npz", "cf_matrix.npz", "cf_users.npy", "labels.parquet", "meta.json")

README = """Pacote de dados do recomendador osu!ml
======================================
Uso privado: deriva dos dumps do data.ppy.sh (licença: só análise estatística; nada público sem autorização do ppy).

1. Descompacta esta pasta ao lado do código (ex.: <repo>/pack).
2. python -m osuml recommend serve --pack pack        (abre http://127.0.0.1:8770)
   ou: python -m osuml recommend suggest --pack pack --player "PXD Vieira" --skills speed
3. O feedback (Serve / Não serve) fica em pack/feedback/recomendacoes_feedback.txt (TSV: abre em qualquer editor ou folha de cálculo).
"""


def _copy_players(store, dest_db: Path, players: list[str]) -> dict[str, Any]:
    from sqlalchemy import select

    from ..storage import models as m
    from ..storage.database import Store, utcnow

    wanted = {p.strip().lower() for p in players if p.strip()}
    out = Store(f"sqlite:///{dest_db}", dest_db.parent / "raw")
    copied: dict[str, int] = {}
    with store.engine.connect() as src, out.engine.begin() as dst:
        users = [u for u in src.execute(select(m.users)).mappings().all() if u["username"].lower() in wanted or str(u["user_id"]) in wanted]
        missing = wanted - {u["username"].lower() for u in users} - {str(u["user_id"]) for u in users}
        if missing:
            raise ValueError("jogadores que não estão na base de dados: " + ", ".join(sorted(missing)))
        now = utcnow()
        for u in users:
            dst.execute(m.users.insert().values(user_id=u["user_id"], username=u["username"], playmode=u["playmode"], raw={"username": u["username"]},
                                                first_seen_at=now, fetched_at=now, request_id=None))
            rows = src.execute(select(m.scores).where(m.scores.c.user_id == u["user_id"])).mappings().all()
            dst.execute(m.scores.insert(), [{
                "score_id": r["score_id"], "user_id": r["user_id"], "beatmap_id": r["beatmap_id"], "ruleset_id": r["ruleset_id"],
                "passed": r["passed"], "accuracy": r["accuracy"], "pp": r["pp"], "mod_acronyms": r["mod_acronyms"], "ended_at": r["ended_at"],
                "first_source": r["first_source"], "raw": {}, "content_sha256": "", "revision": 1, "first_seen_at": now, "last_seen_at": now}
                for r in rows])
            copied[u["username"]] = len(rows)
    out.engine.dispose()
    return copied


def _training_summary(results_json) -> dict[str, Any] | None:
    """Resumo do treino (contagens e métricas, sem dados de jogadores) a partir de um ou mais `results.json` (pass-model, acc-model ou reach-model)."""
    paths = [results_json] if isinstance(results_json, (str, Path)) else list(results_json or [])
    out: dict[str, Any] = {}
    for f in paths:
        if not f or not Path(f).exists():
            continue
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        res, data = d.get("results", {}), d.get("data", {})
        if "model" in res:  # acc-model
            out["acc_model"] = {"mae": res["model"].get("mae"), "r2": res["model"].get("r2"), "passed_pairs": data.get("passed_pairs"), "players": data.get("players"),
                                "test_players": data.get("test_players")}
            out.setdefault("players", data.get("players"))
            out.setdefault("created_at", d.get("created_at"))
        elif "A" in res and "M" in res:  # pass-model
            out["pass_model"] = {"auc": (res["A"].get("all") or {}).get("auc"), "players": data.get("players"), "pairs": data.get("pairs_with_catalog"),
                                 "test_players": data.get("test_players")}
            out.setdefault("players", data.get("players"))
        else:  # reach-model (análise; já não usado pelo recomendador)
            out.setdefault("players", data.get("players"))
            out["reach_auc_by_threshold"] = {k: (v.get("A", {}).get("all", {}) or {}).get("auc") for k, v in res.items() if isinstance(v, dict) and "A" in v}
            out.setdefault("created_at", d.get("created_at"))
    return out or None


def build_pack(store, index_dir: Path, models_dir: Path, out_zip: Path, players: list[str], *, note: str = "",
               training_results=None) -> dict[str, Any]:
    index_dir, models_dir = Path(index_dir), Path(models_dir)
    missing = [f for f in ("index.npz", "meta.json") if not (index_dir / f).exists()]
    models = [models_dir / f for f in ("pass_model_A.txt", "acc_pass_A.txt") if (models_dir / f).exists()]
    if missing or len(models) < 2:
        raise FileNotFoundError(f"índice/modelos incompletos (falta {missing or 'pass_model_A.txt e acc_pass_A.txt'}): corre `osuml recommend build-index` e o treino")
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # no Windows o SQLite pode demorar a largar o ficheiro
        root = Path(tmp) / "pack"
        (root / "index").mkdir(parents=True)
        (root / "models").mkdir()
        for f in INDEX_FILES:
            if (index_dir / f).exists():
                shutil.copy2(index_dir / f, root / "index" / f)
        for f in [*models, *([models_dir / "calibration_pass_acc.json"] if (models_dir / "calibration_pass_acc.json").exists() else [])]:
            shutil.copy2(f, root / "models" / f.name)
        copied = _copy_players(store, root / "players.db", players)
        shutil.rmtree(root / "raw", ignore_errors=True)
        training = _training_summary(training_results)
        if training:
            (root / "models" / "training.json").write_text(json.dumps(training, indent=2, ensure_ascii=False), encoding="utf-8")
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "players": copied, "models": [f.name for f in models], "note": note,
                    "training": training,
                    "license_note": "deriva dos dumps do data.ppy.sh: uso privado, não publicar sem autorização do ppy"}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (root / "LEIA-ME.txt").write_text(README, encoding="utf-8")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, Path("pack") / f.relative_to(root))
    digest = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    return {"zip": str(out_zip), "bytes": out_zip.stat().st_size, "sha256": digest, **manifest}
