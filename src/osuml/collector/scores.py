"""Collector de scores de um jogador (Fase 0).

Fontes (apenas a osu!API v2 oficial), por ordem de execução:
1. user     — resolve username -> id (cache com TTL; não repete pedidos).
2. snapshot — best / firsts / pinned, paginados até ao fim. Mudam devagar,
              por isso só são refeitos após OSUML_SNAPSHOT_TTL_HOURS.
3. recent   — /scores/recent com include_fails=1. Única fonte de fails e de
              plays medianos. A API só devolve as últimas 24h e no máximo
              100 scores, por isso o collector deve correr várias vezes por dia.

Deteção de lacunas (tabela coverage_gaps):
- recent: intervalo entre polls > 24h, ou resposta cheia (100) que não chega
  ao poll anterior.

Cada resposta é gravada em raw ANTES de ser normalizada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..api.http import ApiError, Cancelled, HttpResult
from ..api.osu import PAGE_LIMIT, RECENT_MAX_RESULTS, OsuClient
from ..storage.database import IngestStats, Store, utcnow
from ..storage.normalize import parse_dt

log = logging.getLogger(__name__)

SNAPSHOT_TYPES = ("best", "firsts", "pinned")
# Teto EMPÍRICO de `best`: em 3 de 3 jogadores (incl. os rank 1 e 2, com milhares de plays) a API deu
# offset 0 → 100 itens, offset 100 → 100, offset 200 → 0. O osu-web permite paginar `best` sem teto no
# controlador, por isso o limite vem dos dados que a API expõe, não do código lido. Sem isto, cada
# jogador gastava 1 pedido (1 em cada 5) só para receber uma página vazia.
SNAPSHOT_MAX_ITEMS = {"best": 200}
MAX_SNAPSHOT_PAGES = 50  # salvaguarda contra ciclos infinitos
RECENT_WINDOW = timedelta(hours=24)


@dataclass
class SourceResult:
    source: str
    requests: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    stats: IngestStats = field(default_factory=IngestStats)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"requests": self.requests}
        if self.skipped:
            d["skipped"] = self.skip_reason
        if self.error:
            d["error"] = self.error
        d.update(self.stats.as_dict())
        return d


class ScoreCollector:
    def __init__(
        self,
        store: Store,
        osu: OsuClient,
        *,
        snapshot_ttl: timedelta = timedelta(days=7),
        user_ttl: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.osu = osu
        self.snapshot_ttl = snapshot_ttl
        self.user_ttl = user_ttl
        self.now = now
        self.run_id: str | None = None

    # ------------------------------------------------------------ helpers
    def _call(self, service: str, fn: Callable[..., HttpResult], *args, **kwargs) -> tuple[HttpResult, int]:
        """Executa o pedido e regista-o (sucesso -> raw + api_requests; falha -> api_requests com erro)."""
        assert self.run_id
        try:
            result = fn(*args, **kwargs)
        except ApiError as exc:
            self.store.record_failed_request(
                self.run_id, service, "GET", getattr(fn, "__name__", "?"), kwargs or None, exc.status, str(exc)
            )
            raise
        return result, self.store.record_request(self.run_id, service, result)

    @staticmethod
    def _key(user_id: int, mode: str, name: str) -> str:
        return f"user:{user_id}:{mode}:{name}"

    # --------------------------------------------------------------- user
    def resolve_user(self, username: str | int) -> dict[str, Any]:
        cached = (self.store.find_user_by_id(username) if isinstance(username, int)
                  else self.store.find_user(username))
        if cached and self.now() - cached["fetched_at"] < self.user_ttl:
            log.info("Utilizador %s em cache (id=%s); sem pedido à API", username, cached["user_id"])
            return {"user_id": cached["user_id"], "username": cached["username"],
                    "playmode": cached["playmode"], "from_cache": True}
        result, req_id = self._call("osu", self.osu.get_user, username)
        raw = result.json()
        user_id = self.store.upsert_user(raw, req_id)
        return {"user_id": user_id, "username": raw.get("username"),
                "playmode": raw.get("playmode"), "from_cache": False}

    # ----------------------------------------------------------- snapshot
    def collect_snapshot(self, user_id: int, mode: str, score_type: str, force: bool) -> SourceResult:
        res = SourceResult(score_type)
        key = self._key(user_id, mode, f"snapshot:{score_type}")
        state = self.store.get_state(key) or {}
        last = parse_dt(state.get("last_run_at"))
        if not force and last and self.now() - last < self.snapshot_ttl:
            res.skipped, res.skip_reason = True, f"snapshot recente ({last.isoformat()}); TTL não expirou"
            return res
        offset = 0
        for _ in range(MAX_SNAPSHOT_PAGES):
            result, req_id = self._call(
                "osu", self.osu.get_user_scores, user_id, score_type, mode=mode, limit=PAGE_LIMIT, offset=offset
            )
            res.requests += 1
            page = result.json()
            if not isinstance(page, list):
                raise ApiError(f"resposta inesperada em {score_type}: {type(page).__name__}")
            res.stats.merge(self.store.ingest_scores(page, score_type, req_id))
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            if offset >= SNAPSHOT_MAX_ITEMS.get(score_type, float("inf")):
                break
        self.store.set_state(key, {"last_run_at": self.now().isoformat(), "last_count": res.stats.received})
        return res

    # ------------------------------------------------------------- recent
    def collect_recent(self, user_id: int, mode: str) -> SourceResult:
        res = SourceResult("recent")
        key = self._key(user_id, mode, "recent")
        state = self.store.get_state(key) or {}
        now = self.now()
        last_poll = parse_dt(state.get("last_poll_at"))

        result, req_id = self._call(
            "osu", self.osu.get_user_scores, user_id, "recent",
            mode=mode, limit=RECENT_MAX_RESULTS, offset=0, include_fails=True,
        )
        res.requests += 1
        page = result.json()
        res.stats = self.store.ingest_scores(page, "recent", req_id)

        window_start = now - RECENT_WINDOW
        if last_poll and last_poll < window_start:
            self.store.add_gap(self.run_id, user_id, "recent", last_poll, window_start,
                               "intervalo entre polls > 24h: fails/passes nesse período não foram vistos pelo recent")
        if len(page) >= RECENT_MAX_RESULTS:
            lower = max(last_poll, window_start) if last_poll else window_start
            oldest = res.stats.min_ended_at
            if oldest and oldest > lower:
                self.store.add_gap(self.run_id, user_id, "recent", lower, oldest,
                                   "recent devolveu o máximo (100): scores mais antigos da janela foram cortados; "
                                   "aumentar a frequência de polling")

        self.store.set_state(key, {
            "last_poll_at": now.isoformat(),
            "coverage_start": state.get("coverage_start") or window_start.isoformat(),
            "newest_ended_at": (res.stats.max_ended_at.isoformat() if res.stats.max_ended_at
                                else state.get("newest_ended_at")),
        })
        return res

    # ---------------------------------------------------------------- run
    def collect(self, username: str | int, *, mode: str | None = None, force_snapshot: bool = False,
                snapshot_types: tuple[str, ...] = SNAPSHOT_TYPES) -> dict[str, Any]:
        username = username if isinstance(username, int) else str(username)
        self.run_id = self.store.start_run("collect", str(username))
        summary: dict[str, Any] = {"run_id": self.run_id, "username": username, "sources": {}, "errors": []}
        status = "ok"
        try:
            user = self.resolve_user(username)
            user_id = int(user["user_id"])
            mode = mode or user.get("playmode") or "osu"
            summary.update(user_id=user_id, mode=mode, user_from_cache=user["from_cache"])

            steps: list[tuple[str, Callable[[], SourceResult]]] = [
                *[(t, lambda t=t: self.collect_snapshot(user_id, mode, t, force_snapshot)) for t in snapshot_types],
                ("recent", lambda: self.collect_recent(user_id, mode)),
            ]
            for name, step in steps:
                try:
                    r = step()
                except (ApiError, ValueError, KeyError) as exc:
                    # Uma fonte falhar não invalida as outras; fica registado.
                    log.error("Fonte %s falhou: %s", name, exc)
                    summary["errors"].append({"source": name, "error": str(exc)})
                    status = "partial"
                    continue
                summary["sources"][name] = r.as_dict()

            summary["dataset"] = self.store.user_report(user_id)
        except Cancelled:
            status = "cancelled"
            summary["errors"].append({"source": "run", "error": "cancelado pelo utilizador"})
            raise
        except Exception as exc:
            status = "failed"
            summary["errors"].append({"source": "run", "error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            summary["http"] = {"osu": vars(self.osu.http.stats).copy()}
            summary["status"] = status
            self.store.finish_run(self.run_id, status, _jsonable(summary))
        return summary


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj

