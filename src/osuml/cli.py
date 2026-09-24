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

    server, _ = make_dashboard(ctrl, cat_ctrl, args.port, checker_factory)
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
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
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
            "sync-s3": cmd_sync_s3, "panel": cmd_panel, "poll": cmd_poll, "categorize": cmd_categorize}


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

    pl = sub.add_parser("poll", help="recolha contínua e irregular dos jogadores do painel (<24 h por jogador)")
    pl.add_argument("--max-players", type=int, default=6, help="máx. de jogadores por execução (omissão: 6)")
    pl.add_argument("--max-start-delay", type=int, default=0, help="jitter aleatório (s) antes de começar")
    pl.add_argument("--dry-run", action="store_true", help="mostra o que faria; 0 pedidos")
    pl.add_argument("--status", action="store_true", help="estado do acompanhamento; 0 pedidos")
    pl.add_argument("--pause", action="store_true", help="suspende todas as execuções (cria data/control/PAUSE)")
    pl.add_argument("--resume", action="store_true", help="retoma (apaga data/control/PAUSE)")

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
