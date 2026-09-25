"""CLI.

    python -m osuml collect --user "PXD Vieira"
    python -m osuml status  --user "PXD Vieira"
    python -m osuml export  --user "PXD Vieira" --version v0.1

    python -m osuml maps import --path <dump .tar.bz2 | pasta | .zip>
    python -m osuml maps fetch  --user "PXD Vieira" [--limit N]   (fallback opcional)
    python -m osuml maps status --user "PXD Vieira"
    python -m osuml maps export --user "PXD Vieira" --version v0.2
    python -m osuml maps difficulty --user "PXD Vieira" --version v0.2
    python -m osuml maps reference-pool --path <dump> --n 8000 --seed 42 --version v1
    python -m osuml maps skills --user "PXD Vieira" --version v0.2 --reference-version v1

    python -m osuml dump-scores --tar <dump performance .tar.bz2> [--version v1]   (scores osu! do dump -> Parquet; 0 pedidos)
    python -m osuml sync-s3 [--dry-run]   (espelha data/raw/ + data/processed/ + dump para S3)

    python -m osuml panel [--port 8765]   (painel único: pedidos à API + categorização, em direto)
    python -m osuml poll [--dry-run | --status | --pause | --resume]   (recolha contínua dos jogadores do painel)
    python -m osuml categorize [--port 8766] [--open] [--reference-version v3]   (categoriza mapas e jogadores; 0 pedidos)
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import timedelta

from .config import Settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # O httpx em DEBUG registaria headers (incluindo Authorization). Nunca.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _store(settings: Settings):
    from .storage.database import Store

    return Store(settings.database_url, settings.raw_dir)


def cmd_collect(args: argparse.Namespace, settings: Settings) -> int:
    from .api.http import ApiError
    from .api.osu import OsuClient
    from .collector.scores import ScoreCollector

    if args.max_start_delay > 0:
        # Polling irregular (boa prática da política da API) quando corre em cron.
        delay = random.uniform(0, args.max_start_delay)
        logging.info("A aguardar %.0fs antes de começar (jitter)", delay)
        time.sleep(delay)

    from .api.lock import ApiLock

    api_lock = ApiLock(settings.data_dir / "control" / "api.lock")
    if not api_lock.acquire(timeout=1800, settle=1.1):
        print("Outra recolha à API (ex.: o painel) está em curso há mais de 30 min; a sair sem fazer pedidos.",
              file=sys.stderr)
        return 3
    store = _store(settings)
    osu = OsuClient(
        settings.client_id, settings.client_secret, user_agent=settings.user_agent,
        min_interval=settings.min_interval_osu, base_url=settings.osu_base_url,
        api_version=settings.api_version,
    )
    try:
        collector = ScoreCollector(
            store, osu,
            snapshot_ttl=timedelta(hours=settings.snapshot_ttl_hours),
            user_ttl=timedelta(hours=settings.user_ttl_hours),
        )
        summary = collector.collect(args.user, mode=args.mode, force_snapshot=args.force_snapshot)
    except ApiError as exc:
        hint = ""
        if exc.status in (401, 403) and "/oauth/token" in str(exc):
            hint = " Verifica OSU_CLIENT_ID/OSU_CLIENT_SECRET."
        elif exc.status == 404:
            hint = " Utilizador não encontrado? Confirma o username."
        print(f"Recolha falhou: {exc}.{hint} O pedido falhado ficou registado em api_requests.", file=sys.stderr)
        return 1
    finally:
        osu.close()
        api_lock.release()
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0 if summary["status"] == "ok" else 2


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    user = store.find_user(args.user)
    if not user:
        print(f"Utilizador '{args.user}' ainda não foi recolhido. Corre primeiro: collect --user \"{args.user}\"")
        return 1
    print(json.dumps({"user_id": user["user_id"], "username": user["username"],
                      **store.user_report(user["user_id"])}, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    from .dataset.export import export_user

    store = _store(settings)
    user = store.find_user(args.user)
    if not user:
        print(f"Utilizador '{args.user}' ainda não foi recolhido.")
        return 1
    manifest = export_user(store, user["user_id"], settings.processed_dir, args.version)
    print(json.dumps({k: v for k, v in manifest.items() if k != "report"}, indent=2, ensure_ascii=False))
    return 0


def _user_id(store, username: str | None) -> int | None:
    if username is None:
        return None
    user = store.find_user(username)
    if not user:
        raise SystemExit(f"Utilizador '{username}' ainda não foi recolhido (corre primeiro: collect).")
    return int(user["user_id"])


def cmd_maps(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .beatmaps.acquire import OsuWebFetcher, files_report, import_from_path, wanted_beatmaps

    if args.maps_command == "reference-pool":
        from .beatmaps.reference_pool import export_reference_pool

        out = export_reference_pool(Path(args.path), args.n, args.seed, settings.processed_dir, args.version)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    store = _store(settings)
    uid = _user_id(store, getattr(args, "user", None))
    if args.maps_command == "import":
        path = Path(args.path)
        if not path.exists():
            print(f"Não existe: {path}", file=sys.stderr)
            return 1
        wanted = wanted_beatmaps(store, uid)
        if not wanted:
            print("Todos os mapas referenciados já têm ficheiro.")
            return 0
        source = "folder" if path.is_dir() and not args.dump else "dump"
        logging.info("A procurar %d mapas em %s", len(wanted), path)
        stats = import_from_path(store, path, wanted, source)
        out = {"import": stats.as_dict(), "status": files_report(store, uid)}
    elif args.maps_command == "fetch":
        wanted = wanted_beatmaps(store, uid)
        fetcher = OsuWebFetcher(user_agent=settings.user_agent, min_interval=settings.min_interval_osu)
        try:
            out = {"fetch": fetcher.fetch_missing(store, wanted, limit=args.limit),
                   "status": files_report(store, uid)}
        finally:
            fetcher.close()
    elif args.maps_command == "status":
        out = files_report(store, uid)
    elif args.maps_command == "export":
        from .beatmaps.export import export_beatmaps

        out = export_beatmaps(store, uid, settings.processed_dir, args.version)
    elif args.maps_command == "difficulty":
        from .beatmaps.difficulty import export_difficulty

        out = export_difficulty(store, uid, settings.processed_dir, args.version)
    else:  # skills
        from .beatmaps.skills import export_skill_scales

        diff_path = settings.processed_dir / args.version / f"difficulty_{uid}.parquet"
        maps_path = settings.processed_dir / args.version / f"beatmaps_{uid}.parquet"
        ref_path = settings.processed_dir / args.reference_version / f"reference_pool_{args.reference_version}.parquet"
        for label, p, hint in (
            ("difficulty", diff_path, f"maps difficulty --user ... --version {args.version}"),
            ("beatmaps", maps_path, f"maps export --user ... --version {args.version}"),
            ("reference-pool", ref_path, f"maps reference-pool --version {args.reference_version}"),
        ):
            if not p.exists():
                print(f"Não existe: {p}. Corre primeiro: {hint}", file=sys.stderr)
                return 1
        out = export_skill_scales(diff_path, maps_path, ref_path, settings.processed_dir, args.version, uid)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_dump_scores(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .external.scores_dump import import_scores

    tar = Path(args.tar)
    if not tar.exists():
        print(f"Não existe: {tar}", file=sys.stderr)
        return 1
    from .progress import Progress

    stem = tar.name.split(".")[0].replace("_performance_osu", "")
    prog = Progress(Path(args.progress) if args.progress else settings.processed_dir / "dump_scores" / args.version / f"dump_scores_{stem}.progress.json",
                    f"Importar scores ({stem})", tar.stat().st_size, "bytes")
    out = import_scores(tar, settings.processed_dir / "dump_scores", args.version, max_rows=args.max_rows, progress=prog, workers=args.workers)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_map_catalog(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .beatmaps import catalog as cat

    out_dir = settings.processed_dir / "catalog"
    step = args.catalog_step
    if step == "plan":
        files = [Path(f) for f in args.scores]
        wanted = cat.choose_pairs(files, args.top_n, args.min_plays, [Path(f) for f in args.playcounts] if args.playcounts else None)
        path = out_dir / args.version / "plan.json"
        cat.save_plan(wanted, path, {"top_n": args.top_n, "min_plays": args.min_plays, "score_files": [f.name for f in files]})
        out = {"plan": str(path), "maps": len(wanted), "pairs": sum(len(v) for v in wanted.values())}
    elif step == "bundle":
        out = cat.bundle_osu(Path(args.dump), cat.load_plan(Path(args.plan)), Path(args.out))
    elif step == "run":
        out = cat.run_catalog(Path(args.source), cat.load_plan(Path(args.plan)), Path(args.out_dir) if args.out_dir else out_dir,
                              args.version, shard=cat.parse_shard(args.shard), workers=args.workers, max_maps=args.max_maps)
    elif step == "merge":
        plan = cat.load_plan(Path(args.plan)) if args.plan else None
        out = cat.merge_catalog(Path(args.out_dir) if args.out_dir else out_dir, args.version, plan)
    else:  # all
        out = cat.build_catalog(Path(args.dump), [Path(f) for f in args.scores], out_dir, args.version, top_n=args.top_n,
                                min_plays=args.min_plays, workers=args.workers, max_maps=args.max_maps)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    proc = settings.processed_dir
    scores = [Path(f) for f in args.scores] if args.scores else sorted((proc / "dump_scores" / "v1").glob("dump_scores_*.parquet"))
    if not scores and args.analysis_step not in ("pass-model", "reach-model", "similarity", "export-api-plays", "reach-api-check", "reach-calibrate", "acc-model", "pass-calibrate", "fail-points"):  # esses leem os Parquet de --inputs
        print("Sem scores de dump: corre primeiro `osuml dump-scores`.", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir) if args.out_dir else proc / "analysis" / args.analysis_step.replace("-", "_")
    progress = Path(args.progress) if args.progress else out_dir / args.version / "progress.json"
    catalog_dir = proc / "catalog" / "v1"
    if args.analysis_step == "pp-check":
        from .analysis.ppcheck import run_pp_check

        bundle = Path(args.bundle) if args.bundle else catalog_dir / "osu_subset.tar.gz"
        out = run_pp_check(scores, Path(args.plan) if args.plan else catalog_dir / "plan.json", bundle, out_dir, args.version,
                           n_scores=args.n, seed=args.seed, progress_path=progress)
    elif args.analysis_step == "export-api-plays":
        from .analysis.pass_model import export_api_plays

        out = export_api_plays(_store(settings), Path(args.out) if args.out else out_dir / "inputs" / "api_plays.parquet")
    elif args.analysis_step == "acc-model":
        from .analysis.acc_model import run_acc_model

        inputs = Path(args.inputs) if args.inputs else out_dir / "inputs"
        out = run_acc_model(inputs, out_dir, args.version, rounds=args.rounds, threads=args.threads, cap_rows=args.cap_rows,
                            cap_train=args.cap_train, seed=args.seed, progress_path=progress, sample_pct=args.sample_pct)
    elif args.analysis_step == "fail-points":
        from .analysis.fail_points import analyze

        bundle = Path(args.bundle) if args.bundle else catalog_dir / "osu_subset.tar.gz"
        out = analyze(_store(settings), bundle, out_dir, args.version, progress_path=progress)
    elif args.analysis_step == "pass-calibrate":
        from .analysis.pass_calibration import run_pass_calibration

        rdir = proc / "recommend"
        out = run_pass_calibration(_store(settings), Path(args.index_dir) if args.index_dir else rdir / "index",
                                   Path(args.models_dir) if args.models_dir else rdir / "models", out_dir, args.version, cutoff=args.cutoff,
                                   progress_path=progress)
    elif args.analysis_step == "reach-calibrate":
        from .analysis.reach_calibration import run_reach_calibration

        rdir = proc / "recommend"
        out = run_reach_calibration(_store(settings), Path(args.index_dir) if args.index_dir else rdir / "index",
                                    Path(args.models_dir) if args.models_dir else rdir / "models", out_dir, args.version, cutoff=args.cutoff,
                                    progress_path=progress)
    elif args.analysis_step == "reach-api-check":
        from .analysis.reach_api_check import run_reach_api_check

        rdir = proc / "recommend"
        out = run_reach_api_check(_store(settings), Path(args.index_dir) if args.index_dir else rdir / "index",
                                  Path(args.models_dir) if args.models_dir else rdir / "models", out_dir, args.version, cutoff=args.cutoff,
                                  seed=args.seed, progress_path=progress)
    elif args.analysis_step == "similarity":
        from .analysis.similarity import run_similarity_eval

        inputs = Path(args.inputs) if args.inputs else proc / "analysis" / "pass_model" / "inputs"
        out = run_similarity_eval(inputs, out_dir, args.version, n_test_users=args.test_users, k_neighbors=args.neighbors,
                                  top_items=args.top_items, seed=args.seed, progress_path=progress, sample_pct=args.sample_pct)
    elif args.analysis_step == "reach-model":
        from .analysis.reach_model import run_reach_model

        inputs = Path(args.inputs) if args.inputs else proc / "analysis" / "pass_model" / "inputs"
        thr = {f"acc{int(round(float(x) * 100))}": float(x) for x in args.thresholds.split(",")} if args.thresholds else None
        out = run_reach_model(inputs, out_dir, args.version, rounds=args.rounds, threads=args.threads, cap_rows=args.cap_rows,
                              cap_train=args.cap_train, seed=args.seed, progress_path=progress, sample_pct=args.sample_pct,
                              thresholds=thr, only_a=args.only_a)
    elif args.analysis_step == "pass-model":
        from .analysis.pass_model import run_pass_model

        inputs = Path(args.inputs) if args.inputs else out_dir / "inputs"
        players = {}
        for item in (args.players or []):
            name, _, uid = item.rpartition("=")
            players[name] = int(uid)
        api = Path(args.api_plays) if args.api_plays else inputs / "api_plays.parquet"
        out = run_pass_model(inputs, out_dir, args.version, seeds=tuple(int(x) for x in args.seeds.split(",")), rounds=args.rounds,
                             threads=args.threads, cap_rows=args.cap_rows, cap_train=args.cap_train, progress_path=progress,
                             api_plays=api if api.exists() else None, players=players, sample_pct=args.sample_pct)
    elif args.analysis_step == "playcount-check":
        from .analysis.playcount_check import run_playcount_check

        pcs = [Path(f) for f in args.playcounts] if args.playcounts else sorted((proc / "dump_tables" / "v1").glob("osu_user_beatmap_playcount_*.parquet"))
        if not pcs:
            print("Sem tabelas de playcount: corre primeiro `osuml dump-table`.", file=sys.stderr)
            return 1
        out = run_playcount_check(scores, pcs, out_dir, args.version, progress_path=progress)
    else:
        from .analysis.accuracy_baseline import run_baseline

        catalog = Path(args.catalog) if args.catalog else catalog_dir / "map_attributes.parquet"
        out = run_baseline(scores, catalog, out_dir, args.version, hist_start=args.hist_start, cutoff=args.cutoff, split=args.split,
                           per_player=args.per_player, min_hist=args.min_hist, rounds=args.rounds, seed=args.seed, progress_path=progress)
    print(json.dumps(out, indent=2, ensure_ascii=True, default=str))
    return 0


def cmd_dump_table(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .external.table_dump import import_numeric_table
    from .progress import Progress

    tar = Path(args.tar)
    if not tar.exists():
        print(f"Não existe: {tar}", file=sys.stderr)
        return 1
    stem = tar.name.split(".")[0].replace("_performance_osu", "")
    out = Path(args.out) if args.out else settings.processed_dir / "dump_tables" / "v1" / f"{args.table}_{stem}.parquet"
    prog = Progress(Path(args.progress) if args.progress else out.with_suffix(".progress.json"),
                    f"Importar {args.table} ({stem})", tar.stat().st_size, "bytes")
    res = import_numeric_table(tar, args.table, out, prog, max_rows=args.max_rows)
    print(json.dumps(res, indent=2, ensure_ascii=True, default=str))
    return 0


def _pack_dir(args: argparse.Namespace):
    """Pasta do pacote de dados: --pack, ou ./pack se existir (release), senão None (usa data/ do projeto)."""
    from pathlib import Path

    if getattr(args, "pack", None):
        return Path(args.pack)
    return Path("pack") if (Path("pack") / "players.db").exists() else None


def _make_recommender(args: argparse.Namespace, settings: Settings):
    """(Recommender, pasta do feedback). Com pacote: players.db + index/ + models/ do pacote; sem pacote: a BD e data/processed/recommend."""
    from pathlib import Path

    from .recommend import Recommender
    from .storage.database import Store

    pack = _pack_dir(args)
    if pack is not None:
        store = Store(f"sqlite:///{(pack / 'players.db').as_posix()}", pack / "raw")
        fb = Path(args.feedback_file) if getattr(args, "feedback_file", None) else pack / "feedback" / "recomendacoes_feedback.txt"
        return Recommender(store, pack / "index", pack / "models", feedback_file=fb, log_predictions=True), fb
    proc = settings.processed_dir
    fb = Path(args.feedback_file) if getattr(args, "feedback_file", None) else settings.data_dir / "feedback" / "recomendacoes_feedback.txt"
    return Recommender(_store(settings), proc / "recommend" / "index", proc / "recommend" / "models", feedback_file=fb, log_predictions=True), fb


def _recommend_tools(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    try:  # consolas Windows (cp1252) não têm "≥" nem "–" dos títulos
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    step = args.recommend_step
    if step == "pack":
        from .recommend.pack import build_pack

        proc = settings.processed_dir
        out = build_pack(_store(settings), Path(args.index_dir) if args.index_dir else proc / "recommend" / "index",
                         Path(args.models_dir) if args.models_dir else proc / "recommend" / "models", Path(args.out), args.players, note=args.note,
                         training_results=[Path(f) for f in args.training_results] if args.training_results else None)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    if step == "recalibrate":
        from .recommend.adjust import recalibrate, refresh_adjustments

        models = Path(args.models_dir) if args.models_dir else settings.processed_dir / "recommend" / "models"
        store = _store(settings)
        out = recalibrate(store, models, apply=args.apply)
        if args.apply:
            out["player_adjust"] = refresh_adjustments(store, models)  # depois da calibração global nova, os ajustes por jogador recalculam-se sobre ela
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        if not args.apply:
            print("\n(simulação: nada foi alterado; junta --apply para gravar, o que só acontece se a validação cruzada melhorar)")
        return 0
    if step == "upload":
        from .storage.s3 import upload_pack

        try:
            out = upload_pack(Path(args.zip), settings.s3_bucket, settings.s3_region, args.key, share_hours=args.share_hours, dry_run=args.dry_run)
        except ImportError:
            print("boto3 não está instalado. Corre: pip install .[s3]", file=sys.stderr)
            return 1
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"Upload não feito: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    rec, fb = _make_recommender(args, settings)
    ok, why = rec.ready()
    if step == "check":
        pack = _pack_dir(args)
        print(f"Pasta de dados: {pack if pack is not None else 'dados do projeto (data/processed/recommend)'}")
        print(f"Modelo: {json.dumps(rec.model_info(), ensure_ascii=False, default=str)}")
        try:
            print("Jogadores: " + ", ".join(f"{p['username']} ({p['n_scores']} scores)" for p in rec.players()))
        except Exception as exc:  # BD em falta ou sem esquema
            print(f"Jogadores: erro ({exc})")
        print("Tudo pronto." if ok else f"PROBLEMA: {why}")
        return 0 if ok else 1
    if not ok:
        print(why, file=sys.stderr)
        return 1
    if step == "serve":
        import webbrowser

        from .recommend.app import build_app

        server, _ = build_app(rec, args.port)
        url = f"http://127.0.0.1:{args.port}"
        print(f"Recomendador em {url}  (Ctrl+C para sair)\nFeedback guardado em: {fb}")
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    found = rec.find_player(args.player)
    if found is None:
        names = ", ".join(p["username"] for p in rec.players()) or "nenhum"
        print(f"Jogador não encontrado na base de dados. Disponíveis: {names}", file=sys.stderr)
        return 1
    if step == "suggest":
        res = rec.recommend(found[0], [x.strip() for x in args.skills.split(",") if x.strip()], n=args.n)
        if "error" in res:
            print(res["error"], file=sys.stderr)
            return 1
        print(f"Sugestões para {found[1]} ({', '.join(res['skills'])}):")
        for i, it in enumerate(res["items"], 1):
            acc = "" if it["acc_cur"] is None else f" (atual {it['acc_cur'] * 100:.1f}%)"
            print(f"{i:2d}. [{it['kind']}] {it['label']}  {it['stars']:.2f}*  P(passar): {it['p_pass'] * 100:.0f}%  acc se passar {it['acc_pass'] * 100:.1f}%{acc}\n"
                  f"      ID do mapa: {it['beatmap_id']}  {it['url']}\n      {it['why']}")
        return 0
    out = rec.feedback(found[0], args.beatmap, args.verdict, [x.strip() for x in args.skills.split(",") if x.strip()], note=args.note)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_recommend(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    if args.recommend_step != "build-index":
        return _recommend_tools(args, settings)
    from .progress import Progress  # noqa: F401
    from .recommend.index import build_index

    proc = settings.processed_dir
    inputs = Path(args.inputs) if args.inputs else proc / "analysis" / "pass_model" / "inputs"
    pool = Path(args.reference_pool) if args.reference_pool else proc / "v3" / "reference_pool_v3.parquet"
    bundle = Path(args.bundle) if args.bundle else proc / "catalog" / "v1" / "osu_subset.tar.gz"
    out_dir = Path(args.out_dir) if args.out_dir else proc / "recommend" / "index"
    meta = build_index(inputs, pool, bundle if bundle.exists() else None, out_dir, sample_pct=args.sample_pct,
                       progress_path=out_dir / "progress.json")
    print(json.dumps(meta, indent=2, ensure_ascii=True, default=str))
    return 0


def cmd_sync_s3(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .storage.s3 import sync_all

    dump_path = Path(args.dump_path) if args.dump_path else next(iter(sorted(Path(".").glob("*.tar.bz2"))), None)
    try:
        out = sync_all(settings.raw_dir, settings.processed_dir, dump_path,
                       settings.s3_bucket, settings.s3_region, dry_run=args.dry_run)
    except ImportError:
        print("boto3 não está instalado. Corre: pip install .[s3]", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_panel(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from .beatmaps.skills import ReferenceScale, SkillScorer
    from .categorize.core import CategorizeController, MapCategorizer
    from .panel.core import PanelController, default_runner_factory
    from .panel.dashboard import make_dashboard

    panel_file = Path(args.panel_file) if args.panel_file else settings.processed_dir / "players" / "panel_v1.json"
    if not panel_file.exists():
        print(f"Não existe: {panel_file}", file=sys.stderr)
        return 1
    store = _store(settings)
    ctrl = PanelController(store, panel_file, default_runner_factory(settings, store),
                           lock_path=settings.data_dir / "control" / "api.lock",
                           min_interval=settings.min_interval_osu)
    ref = settings.processed_dir / args.reference_version / f"reference_pool_{args.reference_version}.parquet"
    cat_ctrl = None
    if ref.exists():
        cat_ctrl = CategorizeController(store, MapCategorizer(store, SkillScorer(ReferenceScale.from_parquet(ref))))
    else:
        print(f"Aviso: sem {ref}; o separador Categorização fica de fora.", file=sys.stderr)
    def checker_factory(explorer):
        from .api.lock import ApiLock
        from .explore.check import PlayerChecker
        from .scheduler.tracker import Tracker, make_collect_fn

        control = settings.data_dir / "control"
        tracker = Tracker(store, pause_file=control / "PAUSE", lock=ApiLock(control / "api.lock"))
        return PlayerChecker(explorer, tracker, lambda: make_collect_fn(settings, store), cat_ctrl)

    from .jobs import JobManager

    jobs = JobManager(Path.cwd(), settings.processed_dir, settings.data_dir / "logs")
    from .recommend import Recommender

    recommender = Recommender(store, settings.processed_dir / "recommend" / "index", settings.processed_dir / "recommend" / "models",
                              feedback_file=settings.data_dir / "feedback" / "recomendacoes_feedback.txt", log_predictions=True)
    server, _ = make_dashboard(ctrl, cat_ctrl, args.port, checker_factory, jobs, recommender)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Painel em {url}  (Ctrl+C para sair; nenhum pedido à API é feito até carregares em 'Iniciar' na Recolha)")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        ctrl.cancel_all()
        if cat_ctrl is not None:
            cat_ctrl.cancel()
        ctrl.wait(30)
    finally:
        server.server_close()
    return 0


def cmd_poll(args: argparse.Namespace, settings: Settings) -> int:
    from datetime import datetime

    from .api.lock import ApiLock
    from .scheduler.tracker import FileCandidates, Tracker, make_collect_fn

    control = settings.data_dir / "control"
    pause = control / "PAUSE"
    if args.pause:
        control.mkdir(parents=True, exist_ok=True)
        pause.write_text(f"pausado em {datetime.now().isoformat()}", encoding="utf-8")
        print(f"Pausado: nenhuma execução de `poll` fará pedidos enquanto {pause} existir (usa --resume).")
        return 0
    if args.resume:
        pause.unlink(missing_ok=True)
        print("Retomado.")
        return 0

    store = _store(settings)
    players = settings.processed_dir / "players"
    tracker = Tracker(store, panel_file=players / "panel_v1.json", pause_file=pause,
                      candidates=FileCandidates(players, settings.data_dir / "external" / "performance" / "extracted"),
                      lock=ApiLock(control / "api.lock"))
    if args.status or args.dry_run:
        tracker.seed_from_panel()  # só escreve na BD local, 0 pedidos
        out = tracker.status() if args.status else tracker.poll_once(lambda *_: {}, dry_run=True)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.max_start_delay > 0:
        delay = random.uniform(0, args.max_start_delay)
        logging.info("A aguardar %.0fs antes de começar (jitter)", delay)
        time.sleep(delay)
    log_path = settings.data_dir / "logs" / "poll.jsonl"  # a tarefa agendada corre sem consola (pythonw)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_line(payload: dict) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **payload},
                                ensure_ascii=False, default=str) + "\n")

    collect_fn, close = make_collect_fn(settings, store)
    try:
        out = tracker.poll_once(collect_fn, max_players=args.max_players)
    except Exception as exc:
        log_line({"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        close()
    log_line({k: out[k] for k in ("polled", "errors", "inactive", "skipped", "due_total", "requests_last_24h") if k in out})
    if out.get("polled") and not getattr(args, "no_eval", False):
        try:  # comparação automática previsão/realidade com as jogadas novas; nunca pode estragar uma recolha
            from .recommend.log import evaluate_pending

            rec = _eval_recommender(settings, store)
            if rec.ready()[0]:
                log_line({"eval": evaluate_pending(store, rec)})
                from .recommend.adjust import refresh_adjustments

                log_line({"adjust": refresh_adjustments(store, rec.models_dir)})  # correção por jogador com as jogadas novas (barato, 0 pedidos)
        except Exception as exc:  # noqa: BLE001
            log_line({"eval_error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def _eval_recommender(settings: Settings, store):
    from .recommend import Recommender

    proc = settings.processed_dir
    return Recommender(store, proc / "recommend" / "index", proc / "recommend" / "models")


def cmd_eval_log(args: argparse.Namespace, settings: Settings) -> int:
    """Compara previsão e realidade (0 pedidos): liga as recomendações jogadas, faz a avaliação-sombra das jogadas novas e mostra o relatório."""
    from datetime import datetime

    from .recommend.log import evaluate_pending, report

    store = _store(settings)
    since = datetime.fromisoformat(args.since) if args.since else None
    if not args.report_only:
        rec = _eval_recommender(settings, store)
        ok, why = rec.ready()
        if not ok:
            print(why, file=sys.stderr)
            return 1
        print(json.dumps(evaluate_pending(store, rec, since=since, batch=args.batch), ensure_ascii=False))
        from .recommend.adjust import refresh_adjustments

        print(json.dumps({"player_adjust": refresh_adjustments(store, rec.models_dir)}, ensure_ascii=False))
    print(json.dumps(report(store, since=since), indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_categorize(args: argparse.Namespace, settings: Settings) -> int:
    from .beatmaps.skills import ReferenceScale, SkillScorer
    from .categorize.core import CategorizeController, MapCategorizer
    from .categorize.server import make_server

    ref = settings.processed_dir / args.reference_version / f"reference_pool_{args.reference_version}.parquet"
    if not ref.exists():
        print(f"Não existe: {ref}. Corre primeiro: maps reference-pool --version {args.reference_version}", file=sys.stderr)
        return 1
    store = _store(settings)
    ctrl = CategorizeController(store, MapCategorizer(store, SkillScorer(ReferenceScale.from_parquet(ref))))
    server, _ = make_server(ctrl, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Painel de categorização em {url}  (0 pedidos à API; só lê a BD e os .osu locais; Ctrl+C para sair)")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        ctrl.cancel()
        ctrl.wait(30)
    finally:
        server.server_close()
    return 0


def _handlers() -> dict:
    return {"collect": cmd_collect, "status": cmd_status, "export": cmd_export, "maps": cmd_maps,
            "sync-s3": cmd_sync_s3, "dump-scores": cmd_dump_scores, "map-catalog": cmd_map_catalog, "analyze": cmd_analyze, "recommend": cmd_recommend, "dump-table": cmd_dump_table, "panel": cmd_panel, "poll": cmd_poll, "categorize": cmd_categorize, "eval-log": cmd_eval_log}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="osuml", description="osu! ML — collector de scores (Fase 0)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--env-file", default=".env")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="recolhe scores de um jogador (incremental)")
    c.add_argument("--user", required=True, help='username (ex.: "PXD Vieira")')
    c.add_argument("--mode", choices=["osu", "taiko", "fruits", "mania"], help="omissão: modo principal do jogador")
    c.add_argument("--force-snapshot", action="store_true", help="refaz best/firsts/pinned mesmo dentro do TTL")
    c.add_argument("--max-start-delay", type=int, default=0, help="jitter aleatório (s) antes de começar; útil em cron")

    s = sub.add_parser("status", help="relatório de cobertura do dataset local (sem pedidos à API)")
    s.add_argument("--user", required=True)

    e = sub.add_parser("export", help="exporta o dataset versionado (Parquet/JSONL)")
    e.add_argument("--user", required=True)
    e.add_argument("--version", required=True, help="ex.: v0.1")

    mp = sub.add_parser("maps", help="ficheiros .osu: importar, obter, estado, exportar")
    msub = mp.add_subparsers(dest="maps_command", required=True)
    mi = msub.add_parser("import", help="importa .osu de um dump do data.ppy.sh, pasta ou .zip/.osz (0 pedidos)")
    mi.add_argument("--path", required=True)
    mi.add_argument("--user", help="só os mapas deste jogador (omissão: todos os referenciados)")
    mi.add_argument("--dump", action="store_true", help="marca a pasta como dump oficial extraído")
    mf = msub.add_parser("fetch", help="fallback: descarrega os .osu em falta de osu.ppy.sh/osu/{id} (1 por pedido)")
    mf.add_argument("--user", required=True)
    mf.add_argument("--limit", type=int, help="máximo de ficheiros nesta execução")
    ms = msub.add_parser("status", help="quantos mapas já têm ficheiro (0 pedidos)")
    ms.add_argument("--user")
    me = msub.add_parser("export", help="exporta mapas e hit objects parseados para Parquet")
    me.add_argument("--user", required=True)
    me.add_argument("--version", required=True)
    md = msub.add_parser("difficulty", help="atributos de dificuldade por mapa e combinação de mods (rosu-pp, 0 pedidos)")
    md.add_argument("--user", required=True)
    md.add_argument("--version", required=True)
    mr = msub.add_parser("reference-pool", help="amostra de referência do dump por reservoir sampling, "
                                                 "osu!standard nomod, não ligada a um jogador (0 pedidos)")
    mr.add_argument("--path", required=True, help="dump .tar.bz2 (ou pasta/zip)")
    mr.add_argument("--n", type=int, default=8000, help="tamanho da amostra (omissão: 8000)")
    mr.add_argument("--seed", type=int, default=42, help="seed do reservoir sampling (reprodutibilidade)")
    mr.add_argument("--version", required=True)
    mk = msub.add_parser("skills", help="escalas 0-100 (e nota) por skill, calibradas contra a pool de referência")
    mk.add_argument("--user", required=True)
    mk.add_argument("--version", required=True, help="versão do difficulty_<id>.parquet a usar (ex.: v0.2)")
    mk.add_argument("--reference-version", required=True, help="versão da reference_pool a usar (ex.: v1)")

    pn = sub.add_parser("panel", help="painel local: fila multi-jogador, cancelar pedidos, intervalo real medido")
    pn.add_argument("--port", type=int, default=8765)
    pn.add_argument("--panel-file", help="omissão: data/processed/players/panel_v1.json")
    pn.add_argument("--open", action="store_true", help="abre o navegador")
    pn.add_argument("--reference-version", default="v3", help="pool de referência da Categorização (omissão: v3)")

    ct = sub.add_parser("categorize", help="painel local: categoriza mapas e jogadores em direto (0 pedidos à API)")
    ct.add_argument("--port", type=int, default=8766)
    ct.add_argument("--reference-version", default="v3", help="pool de referência a usar (omissão: v3)")
    ct.add_argument("--open", action="store_true", help="abre o navegador")

    ev = sub.add_parser("eval-log", help="compara previsão e realidade com as jogadas novas (0 pedidos): recomendações jogadas + avaliação-sombra + relatório")
    ev.add_argument("--since", default=None, help="AAAA-MM-DD: recupera o histórico desde esta data (use com --batch day)")
    ev.add_argument("--batch", choices=["new", "day"], default="new", help="new = só o que entrou desde a última avaliação; day = uma passagem por dia (histórico)")
    ev.add_argument("--report-only", action="store_true", help="só mostra o relatório, sem avaliar")
    pl = sub.add_parser("poll", help="recolha contínua e irregular dos jogadores do painel (<24 h por jogador)")
    pl.add_argument("--no-eval", action="store_true", help="não fazer a comparação previsão/realidade no fim (0 pedidos)")
    pl.add_argument("--max-players", type=int, default=6, help="máx. de jogadores por execução (omissão: 6)")
    pl.add_argument("--max-start-delay", type=int, default=0, help="jitter aleatório (s) antes de começar")
    pl.add_argument("--dry-run", action="store_true", help="mostra o que faria; 0 pedidos")
    pl.add_argument("--status", action="store_true", help="estado do acompanhamento; 0 pedidos")
    pl.add_argument("--pause", action="store_true", help="suspende todas as execuções (cria data/control/PAUSE)")
    pl.add_argument("--resume", action="store_true", help="retoma (apaga data/control/PAUSE)")

    ds = sub.add_parser("dump-scores", help="importa scores.sql (osu!) de um dump de performance para Parquet (0 pedidos)")
    ds.add_argument("--tar", required=True, help="ex.: data/external/performance/2026_09_01_performance_osu_random_10000.tar.bz2")
    ds.add_argument("--version", default="v1")
    ds.add_argument("--max-rows", type=int, default=None, help="parar após N linhas (teste)")
    ds.add_argument("--progress", default=None, help="progress.json (omissão: ao lado do Parquet)")
    ds.add_argument("--workers", type=int, default=1, help="processos para o parsing (o bzip2 lê-se num só); >1 para dumps grandes")
    mc = sub.add_parser("map-catalog", help="atributos (stars/aim/speed/reading...) dos mapas mais jogados nos scores do dump (0 pedidos)")
    cs = mc.add_subparsers(dest="catalog_step", required=True)
    for name, hlp in (("plan", "escolhe os pares (mapa, mods) -> plan.json"), ("bundle", "extrai do dump só os .osu do plano -> .tar.gz"),
                      ("run", "calcula (opcional: só o shard I/K), com partes e retoma"), ("merge", "junta as partes -> map_attributes.parquet"),
                      ("all", "plano + cálculo + merge numa só máquina")):
        c = cs.add_parser(name, help=hlp)
        c.add_argument("--version", default="v1")
        if name in ("plan", "all"):
            c.add_argument("--scores", nargs="+", required=True, help="Parquet(s) de dump-scores")
            c.add_argument("--playcounts", nargs="+", default=None, help="Parquet(s) de playcount: inclui também os mapas tentados sem passes")
            c.add_argument("--top-n", type=int, default=50000, help="nº de mapas mais jogados (omissão: 50000, cerca de 87 por cento dos scores)")
            c.add_argument("--min-plays", type=int, default=3, help="scores mínimos para calcular uma combinação de mods")
        if name in ("bundle", "all"):
            c.add_argument("--dump", required=(name == "all"), help="dump de .osu (ex.: 2026_09_01_osu_files.tar.bz2)")
        if name == "bundle":
            c.add_argument("--plan", required=True)
            c.add_argument("--out", required=True, help="ex.: data/processed/catalog/v1/osu_subset.tar.gz")
        if name == "run":
            c.add_argument("--source", required=True, help="dump .tar.bz2, bundle .tar.gz ou pasta de .osu")
            c.add_argument("--plan", required=True)
            c.add_argument("--shard", default=None, help="I/K: só os mapas com beatmap_id %% K == I")
            c.add_argument("--out-dir", default=None)
        if name == "merge":
            c.add_argument("--plan", default=None)
            c.add_argument("--out-dir", default=None)
        if name in ("run", "all"):
            c.add_argument("--workers", type=int, default=3)
            c.add_argument("--max-maps", type=int, default=None, help="parar após N mapas (teste)")
    an = sub.add_parser("analyze", help="análises sobre os scores dos dumps (0 pedidos): pp-check, acc-baseline")
    asub = an.add_subparsers(dest="analysis_step", required=True)
    for name, hlp in (("pp-check", "valida o nosso pp (rosu-pp) contra o pp oficial do dump, numa amostra"),
                      ("acc-baseline", "baseline de accuracy esperada (split temporal, LightGBM)"),
                      ("playcount-check", "testa se playcount - passes estima fails (sem API)"),
                      ("export-api-plays", "exporta os scores da BD (API) para Parquet, para avaliar o modelo pass/fail"),
                      ("reach-api-check", "valida os modelos >=88/93 %% com os jogadores da API (BD local, 0 pedidos)"),
                      ("acc-model", "modelo da accuracy esperada SE PASSAR (mediana do melhor passe do par) para jogador+mapa (treinar no RunPod)"),
                      ("fail-points", "onde e porquê o jogador falha: progresso, morte (HP) vs reinício e o trecho do mapa (BD local, 0 pedidos)"),
                      ("pass-calibrate", "valida e calibra P(passar) e a accuracy se passar com jogadores da API (guarda calibration_pass_acc.json; 0 pedidos)"),
                      ("reach-calibrate", "calibra os modelos >=88/93 %% para jogadores da API (guarda calibration.json ao lado dos modelos; 0 pedidos)"),
                      ("pass-model", "modelo P(passar alguma vez | jogador, mapa) com avaliação (treinar no RunPod)"),
                      ("reach-model", "modelo P(chegar a >=88 %% / >=93 %% de accuracy | jogador, mapa) e a sua fiabilidade"),
                      ("similarity", "compara semelhança de estilo: jogadores parecidos vs mapa a mapa (recall de mapas escondidos)")):
        a = asub.add_parser(name, help=hlp)
        a.add_argument("--version", default="v1")
        a.add_argument("--scores", nargs="+", default=None, help="Parquet(s) de dump-scores (omissão: todos em data/processed/dump_scores/v1)")
        a.add_argument("--out-dir", default=None)
        a.add_argument("--progress", default=None, help="progress.json para a janela de progresso")
        a.add_argument("--seed", type=int, default=42)
        if name == "export-api-plays":
            a.add_argument("--out", default=None)
        if name in ("reach-api-check", "reach-calibrate", "pass-calibrate"):
            a.add_argument("--index-dir", default=None)
            a.add_argument("--models-dir", default=None)
            a.add_argument("--cutoff", default="2026-09-01")
        if name == "similarity":
            a.add_argument("--inputs", default=None)
            a.add_argument("--test-users", type=int, default=1500)
            a.add_argument("--neighbors", type=int, default=50)
            a.add_argument("--top-items", type=int, default=6000)
            a.add_argument("--sample-pct", type=int, default=100)
        if name == "reach-model":
            a.add_argument("--thresholds", default=None, help="ex.: 0.85,0.90,0.95,0.97 (omissão: 0.88,0.93)")
            a.add_argument("--only-a", action="store_true", help="treinar só o modelo A (modelos do recomendador)")
            a.add_argument("--inputs", default=None)
            a.add_argument("--rounds", type=int, default=500)
            a.add_argument("--threads", type=int, default=4)
            a.add_argument("--cap-rows", type=int, default=800)
            a.add_argument("--cap-train", type=int, default=4_000_000)
            a.add_argument("--sample-pct", type=int, default=100)
        if name == "fail-points":
            a.add_argument("--bundle", default=None, help=".osu dos mapas (omissão: catalog/v1/osu_subset.tar.gz)")
        if name == "acc-model":
            a.add_argument("--inputs", default=None, help="pasta com playcount, dump_scores e map_attributes (.parquet)")
            a.add_argument("--rounds", type=int, default=500)
            a.add_argument("--threads", type=int, default=4)
            a.add_argument("--cap-rows", type=int, default=800)
            a.add_argument("--cap-train", type=int, default=8_000_000)
            a.add_argument("--sample-pct", type=int, default=100)
        if name == "pass-model":
            a.add_argument("--inputs", default=None, help="pasta com playcount, dump_scores, map_attributes e api_plays (.parquet)")
            a.add_argument("--api-plays", default=None)
            a.add_argument("--players", nargs="*", default=None, help='ex.: "PXD Vieira=13745526" gaaGOD=23994179')
            a.add_argument("--seeds", default="42,43,44")
            a.add_argument("--rounds", type=int, default=600)
            a.add_argument("--threads", type=int, default=4)
            a.add_argument("--cap-rows", type=int, default=800)
            a.add_argument("--cap-train", type=int, default=4_000_000)
            a.add_argument("--sample-pct", type=int, default=100, help="usar só X %% dos jogadores (ensaio local)")
        if name == "playcount-check":
            a.add_argument("--playcounts", nargs="+", default=None, help="Parquet(s) de dump-table (omissão: todos)")
        if name == "pp-check":
            a.add_argument("--n", type=int, default=5000, help="nº de scores da amostra")
            a.add_argument("--plan", default=None)
            a.add_argument("--bundle", default=None)
        elif name == "acc-baseline":
            a.add_argument("--catalog", default=None)
            a.add_argument("--hist-start", default="2023-01-01")
            a.add_argument("--cutoff", default="2025-01-01")
            a.add_argument("--split", default="2025-07-01")
            a.add_argument("--per-player", type=int, default=100)
            a.add_argument("--min-hist", type=int, default=20)
            a.add_argument("--rounds", type=int, default=300)
    dt = sub.add_parser("dump-table", help="importa uma tabela numérica de um dump de performance para Parquet (0 pedidos)")
    dt.add_argument("--tar", required=True)
    dt.add_argument("--table", default="osu_user_beatmap_playcount")
    dt.add_argument("--out", default=None)
    dt.add_argument("--progress", default=None)
    dt.add_argument("--max-rows", type=int, default=None)
    rc = sub.add_parser("recommend", help="recomendador de mapas: construir o índice (0 pedidos)")
    rsub = rc.add_subparsers(dest="recommend_step", required=True)
    bi = rsub.add_parser("build-index", help="notas por eixo, jogadores parecidos e nomes dos mapas")
    bi.add_argument("--inputs", default=None)
    bi.add_argument("--reference-pool", default=None)
    bi.add_argument("--bundle", default=None)
    bi.add_argument("--out-dir", default=None)
    bi.add_argument("--sample-pct", type=int, default=70, help="%% dos jogadores dos dumps na matriz de filtragem colaborativa")
    for nm, hlp in (("check", "verifica a pasta de dados (modelos, índice, jogadores) e diz que modelo está carregado"),
                    ("serve", "abre a aplicação local (escolher jogador + skills, ver mapas, dar feedback)"),
                    ("suggest", "sugestões no terminal para um jogador da base de dados"),
                    ("feedback", "regista feedback (serve / nao_serve) de um mapa, no ficheiro de texto e na BD")):
        a = rsub.add_parser(nm, help=hlp)
        a.add_argument("--pack", default=None, help="pasta do pacote de dados (omissão: ./pack se existir, senão os dados do projeto)")
        a.add_argument("--feedback-file", default=None, help="ficheiro de texto do feedback (omissão: <pack>/feedback/recomendacoes_feedback.txt)")
        if nm == "serve":
            a.add_argument("--port", type=int, default=8770)
            a.add_argument("--open", action="store_true", help="abre o browser")
        elif nm == "check":
            pass
        else:
            a.add_argument("--player", required=True, help='nome (ex.: "PXD Vieira") ou id')
            a.add_argument("--skills", default="speed" if nm == "suggest" else "", help="aim,speed,stamina,reading (uma ou várias)")
        if nm == "suggest":
            a.add_argument("-n", type=int, default=20)
        if nm == "feedback":
            a.add_argument("--beatmap", type=int, required=True)
            a.add_argument("--verdict", choices=["serve", "nao_serve"], required=True)
            a.add_argument("--note", default="")
    rcal = rsub.add_parser("recalibrate", help="recalibra a probabilidade/accuracy com o registo de previsões (0 pedidos) e atualiza a correção por jogador; simulação sem --apply")
    rcal.add_argument("--apply", action="store_true", help="grava a nova calibração (só se melhorar em validação cruzada; guarda cópia da anterior)")
    rcal.add_argument("--models-dir", default=None)
    up = rsub.add_parser("upload", help="envia o pacote (.zip) para o bucket S3 privado (só quando pedido; credenciais do boto3)")
    up.add_argument("--zip", default="dist/osuml-pack.zip")
    up.add_argument("--key", default=None, help="chave no bucket (omissão: recommend/<nome do zip>)")
    up.add_argument("--share-hours", type=float, default=0, help="devolve também um link temporário de descarga válido X horas")
    up.add_argument("--dry-run", action="store_true", help="não contacta o S3")
    pk = rsub.add_parser("pack", help="cria o pacote de dados (.zip) com índice, modelos e só os jogadores indicados (uso privado)")
    pk.add_argument("--out", default="dist/osuml-pack.zip")
    pk.add_argument("--players", nargs="+", required=True, help='ex.: "PXD Vieira" gaaGOD')
    pk.add_argument("--index-dir", default=None)
    pk.add_argument("--models-dir", default=None)
    pk.add_argument("--note", default="")
    pk.add_argument("--training-results", nargs="*", default=None, help="results.json do pass-model e do acc-model (guarda no pacote o resumo do treino, sem dados de jogadores)")
    sy = sub.add_parser("sync-s3", help="espelha data/raw/ + data/processed/ + dump para o bucket S3 (0 pedidos à osu!API)")
    sy.add_argument("--dry-run", action="store_true", help="não contacta o S3, só lista o que seria enviado")
    sy.add_argument("--dump-path", help="dump a incluir (omissão: primeiro *.tar.bz2 encontrado na raiz do projeto)")

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        settings = Settings.from_env(args.env_file, require_credentials=(args.command in ("collect", "panel") or (args.command == "poll" and not (
            args.status or args.dry_run or args.pause or args.resume))))
    except RuntimeError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 1
    return _handlers()[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
