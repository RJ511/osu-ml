"""Recolha das tags votadas dos mapas (lazer) a partir do mirror hinai: 1 pedido por SET (todas as dificuldades).

`GET /v3/osu/beatmaps/s/{set_id}` (omissão, local) ou `.../details` → `beatmaps[].top_tag_ids = [{tag_id, count}]`; o `/details` coincide com a osu!API, o local é uma gravação com ~80 % dos votos.
0 pedidos à osu!API. Boa conduta (docs do mirror, lidas em 2026-10-14; ver CLAUDE.md):
- só sets ranked/loved/approved (respondidos localmente pelo mirror; os outros gastam a quota partilhada deles na osu!API);
- ritmo ≥ `MIN_INTERVAL_S` entre pedidos (e mais devagar se o mirror demorar), teto por execução;
- consulta `GET /api/v1/hinai/guard` antes de começar e a cada `GUARD_CHECK_EVERY` pedidos; se houver pressão, espera
  (`GUARD_RETRY_S`, o `retry_after_s` publicado) e, passado o tempo máximo, termina sem pedir mais;
- 429/503: respeita `Retry-After`; `MAX_CONSEC_ERRORS` falhas seguidas ⇒ pára tudo;
- User-Agent só com nome+versão do projeto (sem dados pessoais); sem parâmetros para contornar a cache; sem downloads `.osz`.
Retomável: sets já recolhidos não se repetem.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import delete, func, select

from ..progress import Progress
from ..storage import models as m
from ..storage.database import Store, utcnow

log = logging.getLogger(__name__)

HINAI_BASE = "https://mirror.hinamizawa.ai"
# nome+versão do projeto e o Discord do utilizador como contacto (pedido explícito dele em 2026-10-14; nunca e-mail)
USER_AGENT = "osuml/1.0 (+discord:.bieira)"
SOURCES = {"raw": "hinai_raw", "details": "hinai"}   # fonte guardada com cada set
DEFAULT_ENDPOINT = "raw"  # `/v3/osu/beatmaps/s/{id}`: servido localmente pelo mirror para ranked/loved (gravação: votos ~80 % dos atuais);
#                           "details" = `/v3/osu/beatmaps/s/{id}/details` (votos iguais aos da osu!API, mas origem local não confirmada)
MIN_INTERVAL_S = 1.0
DEFAULT_DAILY_CAP = 3000          # teto diário de sets recolhidos ao mirror (aprovado pelo utilizador a 2026-10-14; antes 1000)
GUARD_CHECK_EVERY = 25
GUARD_RETRY_S = 120          # `thresholds.retry_after_s` do guard
AMBER_PAIN = 2               # semáforo: dor >= 2 ⇒ âmbar (2 s por pedido)
HIGH_PAIN = 4                # dor "alta" (limiar publicado: 5): só a partir daqui se pára (vermelho)
PRESSURE_INTERVAL_S = 3.0    # com "pressão" ativa mas dor baixa (decisão do utilizador, 2026-10-14): continua a 1 pedido a cada 3 s
AMBER_INTERVAL_S = 2.0
MAX_CONSEC_ERRORS = 3
MAX_RETRY_AFTER_S = 600
SLOW_RESPONSE_S = 3.0        # resposta lenta ⇒ pausa extra igual (não pressionar um serviço já sob carga)
GOOD_STATUSES = ("ranked", "loved", "approved")


class Abort(Exception):
    """Pára a execução com um motivo (pressão do guard, erros seguidos, ...)."""


class TagCollector:
    def __init__(self, store: Store, *, base_url: str = HINAI_BASE, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 min_interval: float = MIN_INTERVAL_S, raw_dir: Path | None = None, progress: Progress | None = None,
                 endpoint: str = DEFAULT_ENDPOINT) -> None:
        if endpoint not in SOURCES:
            raise ValueError(f"endpoint desconhecido: {endpoint!r} (raw | details)")
        self.endpoint, self.source = endpoint, SOURCES[endpoint]
        self.store = store
        self.http = httpx.Client(base_url=base_url, headers={"User-Agent": USER_AGENT}, timeout=30.0,
                                 transport=transport, follow_redirects=False)
        self._sleep, self._clock = sleep, clock
        self.min_interval = max(float(min_interval), MIN_INTERVAL_S)  # nunca abaixo de 1 pedido/s
        self.raw_dir = raw_dir
        self.progress = progress
        self._last_start: float | None = None
        self.requests = 0
        self.guard_checks = 0
        self._interval = 0.0               # intervalo mínimo atual (≥ min_interval): sobe com o semáforo (âmbar 2 s, pressão com dor baixa 3 s)
        self.last_light = "unknown"
        self.failures: dict[int, int] = {}
        self.status_counts: dict[str, int] = {}
        self.latencies_ms: list[float] = []

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ guard
    def guard(self) -> dict[str, Any]:
        """Estado de pressão do mirror (`pressure_active` global ou da credencial partilhada)."""
        self.guard_checks += 1
        self._pace()
        resp = self.http.get("/api/v1/hinai/guard")
        self.requests += 1
        resp.raise_for_status()
        d = resp.json()
        pressure = bool(d.get("pressure_active")) or bool((d.get("credential") or {}).get("pressure_active"))
        th = d.get("thresholds") or {}
        pain = d.get("pain_last_60s")
        limit = th.get("pain_60s") or 5
        if pain is not None and pain >= min(HIGH_PAIN, limit):  # dor alta: parar
            self.last_light, self._interval = "red", 0.0
        elif pressure:  # pressão ativa mas dor baixa: continuar devagar (1 pedido / 3 s)
            self.last_light, self._interval = "amber", PRESSURE_INTERVAL_S
        elif pain is not None and pain >= AMBER_PAIN:
            self.last_light, self._interval = "amber", AMBER_INTERVAL_S
        else:
            self.last_light, self._interval = "green", 0.0
        with self.store.engine.begin() as c:
            c.execute(m.tag_guard_log.insert().values(at=utcnow(), pressure=pressure, pain_60s=pain, pain_limit=th.get("pain_60s"),
                                                      retry_after_s=th.get("retry_after_s")))
        return {"pressure": pressure, "pain_60s": pain, "thresholds": th, "light": self.last_light}

    def wait_for_calm(self, max_wait_s: float) -> bool:
        """Espera até o guard deixar de indicar pressão. False se passou o tempo máximo (não se pede mais nada)."""
        waited = 0.0
        while True:
            state = self.guard()
            if state["light"] != "red":  # só a dor alta pára a recolha; pressão com dor baixa abranda (3 s/pedido)
                return True
            retry = float(state["thresholds"].get("retry_after_s") or GUARD_RETRY_S)
            if waited + retry > max_wait_s:
                return False
            self._status(f"guard vermelho (dor {state['pain_60s']}) — à espera {int(retry)} s")
            self._sleep(retry)
            waited += retry

    # ------------------------------------------------------------------ plano
    def load_dictionary(self, path: Path) -> int:
        """Dicionário das tags (`GET /api/v2/tags` já guardado em `data/raw/probe_tags/tags.json`). 0 pedidos."""
        if not path.exists():
            return 0
        tags = json.loads(path.read_text(encoding="utf-8")).get("tags", [])
        with self.store.engine.begin() as c:
            c.execute(delete(m.tag_dictionary))
            c.execute(m.tag_dictionary.insert(), [{"tag_id": t["id"], "name": t["name"], "ruleset_id": t.get("ruleset_id"),
                                                    "description": (t.get("description") or "")[:1024]} for t in tags])
        return len(tags)

    def plan(self, user_ids: list[int]) -> list[int]:
        """Sets ranked/loved/approved dos mapas jogados pelos jogadores (1.º jogador primeiro), sem os já recolhidos."""
        out: list[int] = []
        seen: set[int] = set()
        with self.store.engine.connect() as c:
            done = {r[0] for r in c.execute(select(m.tag_fetches.c.beatmapset_id))}
            for uid in user_ids:
                rows = c.execute(
                    select(m.beatmaps.c.beatmapset_id).distinct()
                    .select_from(m.scores.join(m.beatmaps, m.beatmaps.c.beatmap_id == m.scores.c.beatmap_id))
                    .where(m.scores.c.user_id == uid, m.beatmaps.c.status.in_(GOOD_STATUSES),
                           m.beatmaps.c.beatmapset_id.isnot(None))
                    .order_by(m.beatmaps.c.beatmapset_id)).all()
                for (sid,) in rows:
                    sid = int(sid)
                    if sid not in seen and sid not in done:
                        seen.add(sid)
                        out.append(sid)
        return out

    # ------------------------------------------------------------------ execução
    def run(self, set_ids: list[int], *, max_sets: int = 1000, max_wait_guard_s: float = 1800.0) -> dict[str, Any]:
        todo = set_ids[:max_sets]
        out: dict[str, Any] = {"planned": len(todo), "ok": 0, "not_found": 0, "tags_rows": 0, "aborted": None}
        if self.progress is not None:
            self.progress.total = len(todo)
        consec = 0
        try:
            if not self.wait_for_calm(max_wait_guard_s):
                raise Abort("guard vermelho (dor alta): nada foi pedido; tenta mais tarde")
            for i, sid in enumerate(todo):
                if i and i % GUARD_CHECK_EVERY == 0 and not self.wait_for_calm(max_wait_guard_s):
                    raise Abort(f"guard vermelho (dor alta) após {i} sets; parei")
                try:
                    n = self._fetch_set(sid)
                    consec = 0
                except _Retryable as exc:
                    consec += 1
                    self.failures[sid] = self.failures.get(sid, 0) + 1
                    log.warning("set %s: %s", sid, exc)
                    if consec >= MAX_CONSEC_ERRORS:
                        raise Abort(f"{consec} falhas seguidas (último: {exc}); parei") from exc
                    self._sleep(min(exc.retry_after, MAX_RETRY_AFTER_S))
                    continue
                if n is None:
                    out["not_found"] += 1
                else:
                    out["ok"] += 1
                    out["tags_rows"] += n
                self._tick(i + 1, f"tags: {out['ok']} sets com dados · {out['not_found']} sem set")
        except Abort as exc:
            out["aborted"] = str(exc)
        out["requests"] = self.requests
        out["guard_checks"] = self.guard_checks
        if self.progress is not None:
            self.progress.finish("error" if out["aborted"] else "done",
                                 f"tags: {out['ok']} sets · {out['not_found']} sem set" + (f" — {out['aborted']}" if out["aborted"] else ""))
        return out

    def run_session(self, *, daily_cap: int = DEFAULT_DAILY_CAP, max_minutes: float = 110.0, batch: int = GUARD_CHECK_EVERY, max_wait_guard_s: float = 1800.0,
                    paused_fn: Callable[[], bool] | None = None, trigger: str = "scheduled") -> dict[str, Any]:
        """Sessão contínua guiada pelo semáforo do guard: enquanto estiver verde/âmbar continua (âmbar = metade do ritmo),
        em vermelho espera e volta a consultar; termina por fila vazia, teto diário, pausa, tempo máximo ou paragem por erros/pressão.
        O guard é consultado antes de cada lote (`batch` sets, por omissão 25). Um set que falha 2 vezes na sessão é saltado."""
        from .queue import next_batch, queue_counts, tags_today

        started, t0 = utcnow(), self._clock()
        prog, self.progress = self.progress, None  # o progresso da sessão é escrito aqui, não por lote
        tot = {"planned": 0, "ok": 0, "not_found": 0, "tags_rows": 0}
        done = 0
        aborted = skipped = None
        todo = sum(q["todo"] for q in queue_counts(self.store).values())
        if prog is not None:
            prog.total = max(min(todo, max(daily_cap - tags_today(self.store), 0)), 1)
            prog.update(0, label="tags: sessão iniciada", force=True)
        try:
            while True:
                if paused_fn is not None and paused_fn():
                    skipped = "recolha pausada (TAGS_PAUSE)"
                    break
                left = daily_cap - tags_today(self.store)
                if left <= 0:
                    skipped = f"teto diário atingido ({daily_cap})"
                    break
                if (self._clock() - t0) / 60.0 >= max_minutes:
                    skipped = "tempo máximo da sessão"
                    break
                ids = [s for s in next_batch(self.store, min(batch, left) + len(self.failures)) if self.failures.get(s, 0) < 2][:min(batch, left)]
                if not ids:
                    skipped = "fila vazia"
                    break
                out = self.run(ids, max_sets=len(ids), max_wait_guard_s=max_wait_guard_s)
                for k in tot:
                    tot[k] += out.get(k, 0)
                done += out["ok"] + out["not_found"]
                if prog is not None:
                    prog.update(done, label=f"tags: {done} sets nesta sessão · semáforo {self.last_light}")
                if out["aborted"]:
                    aborted = out["aborted"]
                    break
        finally:
            self.progress = prog
        lat = self.latencies_ms
        out = {**tot, "aborted": aborted, "skipped": skipped, "requests": self.requests, "guard_checks": self.guard_checks, "light": self.last_light}
        with self.store.engine.begin() as c:
            c.execute(m.tag_runs.insert().values(
                started_at=started, finished_at=utcnow(), trigger=trigger, planned=tot["planned"], ok=tot["ok"], not_found=tot["not_found"],
                requests=self.requests, guard_checks=self.guard_checks, status_counts=self.status_counts,
                avg_ms=(sum(lat) / len(lat) if lat else None), max_ms=(max(lat) if lat else None),
                aborted=aborted, note=(skipped or None)))
        if prog is not None:
            prog.finish("error" if aborted else "done", f"tags: {done} sets nesta sessão" + (f" — {aborted}" if aborted else (f" — {skipped}" if skipped else "")))
        return out

    def run_queue(self, *, max_sets: int = 100, daily_cap: int = DEFAULT_DAILY_CAP, max_wait_guard_s: float = 1800.0, paused: bool = False,
                  trigger: str = "manual") -> dict[str, Any]:
        """Uma execução sobre a fila (fases B→C→A): respeita pausa, teto diário e guard; regista-se em `tag_runs`."""
        from .queue import next_batch, tags_today

        started = utcnow()
        used = tags_today(self.store)
        limit = min(max_sets, max(daily_cap - used, 0))
        out: dict[str, Any] = {"planned": 0, "ok": 0, "not_found": 0, "tags_rows": 0, "aborted": None, "skipped": None}
        if paused:
            out["skipped"] = "recolha pausada (TAGS_PAUSE)"
        elif limit <= 0:
            out["skipped"] = f"teto diário atingido ({used}/{daily_cap})"
        else:
            batch = next_batch(self.store, limit)
            if not batch:
                out["skipped"] = "fila vazia"
            else:
                out = {**out, **self.run(batch, max_sets=limit, max_wait_guard_s=max_wait_guard_s)}
        lat = self.latencies_ms
        with self.store.engine.begin() as c:
            c.execute(m.tag_runs.insert().values(
                started_at=started, finished_at=utcnow(), trigger=trigger, planned=out.get("planned"), ok=out.get("ok"),
                not_found=out.get("not_found"), requests=self.requests, guard_checks=self.guard_checks, status_counts=self.status_counts,
                avg_ms=(sum(lat) / len(lat) if lat else None), max_ms=(max(lat) if lat else None),
                aborted=(out.get("aborted") or None), note=(out.get("skipped") or None)))
        return out

    def _fetch_set(self, sid: int) -> int | None:
        """Pede e guarda um set. Devolve nº de linhas de tags (None = o mirror não tem o set)."""
        self._pace()
        started = self._clock()
        try:
            resp = self.http.get(f"/v3/osu/beatmaps/s/{sid}" + ("/details" if self.endpoint == "details" else ""))
        except httpx.HTTPError as exc:
            self.requests += 1
            self.status_counts["rede"] = self.status_counts.get("rede", 0) + 1
            raise _Retryable(f"rede: {type(exc).__name__}", 30.0) from exc
        self.requests += 1
        took = self._clock() - started
        self.latencies_ms.append(took * 1000)
        self.status_counts[str(resp.status_code)] = self.status_counts.get(str(resp.status_code), 0) + 1
        if took > SLOW_RESPONSE_S:
            self._sleep(took)  # serviço lento: abrandar na mesma medida
        if resp.status_code == 404:
            self._save(sid, None, 404, resp.content)
            return None
        if resp.status_code in (429, 503):
            retry = float(resp.headers.get("Retry-After") or GUARD_RETRY_S)
            raise _Retryable(f"HTTP {resp.status_code} {resp.headers.get('x-hinai-forensics', '')}".strip(), retry)
        if resp.status_code != 200:
            raise _Retryable(f"HTTP {resp.status_code}", 60.0)
        return self._save(sid, resp.json(), 200, resp.content)

    def _save(self, sid: int, data: dict[str, Any] | None, http_status: int, raw: bytes) -> int | None:
        now = utcnow()
        rows = []
        n_maps = 0
        if data is not None:
            for b in data.get("beatmaps") or []:
                n_maps += 1
                for t in b.get("top_tag_ids") or []:
                    rows.append({"beatmap_id": int(b["id"]), "tag_id": int(t["tag_id"]), "votes": int(t["count"]),
                                 "beatmapset_id": sid, "fetched_at": now, "source": self.source})
            if self.raw_dir is not None:
                self.raw_dir.mkdir(parents=True, exist_ok=True)
                suffix = ".json.gz" if self.endpoint == "details" else ".local.json.gz"
                (self.raw_dir / f"{sid}{suffix}").write_bytes(gzip.compress(raw))
        with self.store.engine.begin() as c:
            c.execute(delete(m.beatmap_tags).where(m.beatmap_tags.c.beatmapset_id == sid))
            if rows:
                c.execute(m.beatmap_tags.insert(), rows)
            c.execute(delete(m.tag_fetches).where(m.tag_fetches.c.beatmapset_id == sid))
            c.execute(m.tag_fetches.insert().values(
                beatmapset_id=sid, status="ok" if data is not None else "not_found", n_beatmaps=n_maps or None,
                n_tags=len(rows), http_status=http_status, source=self.source, fetched_at=now))
        return len(rows) if data is not None else None

    # ------------------------------------------------------------------ utilitários
    def _pace(self) -> None:
        if self._last_start is not None:
            wait = max(self.min_interval, self._interval) - (self._clock() - self._last_start)
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._clock()

    def _tick(self, done: int, label: str) -> None:
        if self.progress is not None:
            self.progress.update(done, label=None)
            self.progress.label = label

    def _status(self, label: str) -> None:
        log.info(label)
        if self.progress is not None:
            self.progress.update(label=label, force=True)


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def summary(store: Store) -> dict[str, Any]:
    with store.engine.connect() as c:
        return {
            "sets_ok": c.execute(select(func.count()).select_from(m.tag_fetches).where(m.tag_fetches.c.status == "ok")).scalar(),
            "sets_not_found": c.execute(select(func.count()).select_from(m.tag_fetches).where(m.tag_fetches.c.status == "not_found")).scalar(),
            "beatmaps_with_tags": c.execute(select(func.count(func.distinct(m.beatmap_tags.c.beatmap_id)))).scalar(),
            "tag_rows": c.execute(select(func.count()).select_from(m.beatmap_tags)).scalar(),
            "dictionary": c.execute(select(func.count()).select_from(m.tag_dictionary)).scalar(),
        }
