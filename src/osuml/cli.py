"""CLI.

    python -m osuml collect --user "PXD Vieira"
    python -m osuml status  --user "PXD Vieira"
    python -m osuml export  --user "PXD Vieira" --version v0.1
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

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        settings = Settings.from_env(args.env_file, require_credentials=args.command == "collect")
    except RuntimeError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 1
    return {"collect": cmd_collect, "status": cmd_status, "export": cmd_export}[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
