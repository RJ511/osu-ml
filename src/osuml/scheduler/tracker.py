"""Recolha contínua e irregular dos jogadores do painel (`osuml poll`).

Regras (decididas com o utilizador):
- cada jogador é consultado (`recent`) a intervalos ALEATÓRIOS de 14–22 h — sempre < 24 h, porque o
  `recent` da API só cobre as últimas 24 h; a irregularidade evita rajadas e horas fixas;
- `best` só na 1.ª recolha de cada jogador (nunca para quem já tem o snapshot); PXD Vieira fica de
  fora (tem a sua própria tarefa `osuml-collect`, com TTL de 7 dias para o `best`);
- sem atividade há ≥ 9 dias (contados a partir do último score OU do início do acompanhamento, o que
  for mais recente) → deixa de haver pedidos para esse jogador e é substituído por outro da mesma
  banda (jogadores escolhidos pelo utilizador, banda "nomeado", não são substituídos sozinhos);
- proteções: lotes pequenos por execução, pausa aleatória entre jogadores, teto diário de pedidos,
  bloqueio entre processos (`ApiLock`) e ficheiro `data/control/PAUSE` para suspender tudo.

A atividade mede-se pelo máximo de `ended_at` dos scores guardados: o `best` sozinho não chega (um
top player pode jogar sem bater os seus melhores), mas os `recent` recolhidos entram na mesma tabela.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from sqlalchemy import func, select, update

from ..api.http import ApiError
from ..api.lock import ApiLock
from ..storage import models as m
from ..storage.database import Store, utcnow

log = logging.getLogger(__name__)

POLL_INTERVAL_H = (14.0, 22.0)
RETRY_AFTER_ERROR_H = (2.0, 4.0)
INACTIVITY_DAYS = 9
MAX_PLAYERS_PER_RUN = 6
PAUSE_BETWEEN_PLAYERS_S = (20.0, 90.0)
DAILY_REQUEST_CAP = 400
EXCLUDED_USER_IDS = {13745526}
NO_AUTO_REPLACE_BANDS = {"nomeado"}
LOCK_SETTLE_S = 1.1

BANDS: dict[str, tuple[int, int]] = {
    "top20": (0, 20), "1000-10000": (1000, 10000), "10000-25000": (10000, 25000),
    "25000-50000": (25000, 50000), "50000-100000": (50000, 100000),
    "100000-200000": (100000, 200000), "200000-500000": (200000, 500000), "500000+": (500000, 10**9),
}


class Candidates(Protocol):
    def pick(self, band: str, exclude: set[int]) -> dict[str, Any] | None: ...


class FileCandidates:
    """Substitutos a partir do que já temos dos dumps (0 pedidos): 1.º os candidatos do painel v1
    com mais dias ativos; depois `osu_user_stats` (activos ≤ 7 dias no snapshot, playcount ≥ 1000)."""

    def __init__(self, players_dir: Path, stats_dir: Path, seed: int = 42) -> None:
        self.players_dir, self.stats_dir, self.seed = Path(players_dir), Path(stats_dir), seed
        self._stats: dict[str, list[dict]] | None = None

    def _load_stats(self) -> dict[str, list[dict]]:
        if self._stats is None:
            from ..external.sqldump import iter_table

            self._stats = {}
            for key, prefix in (("rnd", "2026_09_01_performance_osu_random_10000"),
                                ("top", "2026_09_01_performance_osu_top_1000")):
                p = self.stats_dir / f"{prefix}__osu_user_stats.sql"
                self._stats[key] = list(iter_table(p)) if p.exists() else []
        return self._stats

    def pick(self, band: str, exclude: set[int]) -> dict[str, Any] | None:
        cand_f, act_f = self.players_dir / "panel_candidates.json", self.players_dir / "panel_activity.json"
        if cand_f.exists() and act_f.exists():
            act = {int(k): v for k, v in json.loads(act_f.read_text(encoding="utf-8")).items()}
            pool = [c for c in json.loads(cand_f.read_text(encoding="utf-8"))
                    if c["band"] == band and c["user_id"] not in exclude
                    and act.get(c["user_id"], {}).get("active_days_28d", 0) >= 12]
            pool.sort(key=lambda c: (-act[c["user_id"]]["active_days_28d"], c["user_id"]))
            if pool:
                return {"user_id": int(pool[0]["user_id"]), "band": band, "source": "candidatos do painel v1"}
        lo, hi = BANDS.get(band, (None, None))
        if lo is None:
            return None
        stats = self._load_stats()
        rows = stats["top"] if band == "top20" else stats["rnd"]
        newest = max((r["last_played"] for r in rows if r.get("last_played")), default=None)
        if newest is None:
            return None
        cutoff = datetime.fromisoformat(newest) - timedelta(days=7)
        lo_rank, hi_rank = (20, 1000) if band == "top20" else (lo, hi)
        pool = [r for r in rows if lo_rank < r["rank_score_index"] <= hi_rank and r["user_id"] not in exclude
                and r["playcount"] >= 1000 and r.get("last_played")
                and datetime.fromisoformat(r["last_played"]) >= cutoff]
        if not pool:
            return None
        if band == "top20":
            r = min(pool, key=lambda r: r["rank_score_index"])
        else:
            r = random.Random(f"{self.seed}:{band}:{len(exclude)}").choice(pool)
        return {"user_id": int(r["user_id"]), "band": band, "source": "osu_user_stats (dump)",
                "rank": r["rank_score_index"]}


class Tracker:
    def __init__(self, store: Store, *, panel_file: Path | None = None, candidates: Candidates | None = None,
                 now: Callable[[], datetime] = utcnow, rng: random.Random | None = None,
                 sleep: Callable[[float], None] = time.sleep, pause_file: Path | None = None,
                 lock: ApiLock | None = None, lock_settle: float = LOCK_SETTLE_S) -> None:
        self.store, self.panel_file, self.candidates = store, panel_file, candidates
        self._now, self._rng, self._sleep = now, rng or random.Random(), sleep
        self.pause_file, self.lock, self.lock_settle = pause_file, lock, lock_settle

    # ------------------------------------------------------------ helpers
    def _jitter(self, hours: tuple[float, float]) -> timedelta:
        return timedelta(hours=self._rng.uniform(*hours))

    def _rows(self, **where: Any) -> list[dict[str, Any]]:
        q = select(m.tracked_players)
        for k, v in where.items():
            q = q.where(m.tracked_players.c[k] == v)
        with self.store.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(m.tracked_players.c.next_poll_at)).mappings()]

    def _set(self, uid: int, **values: Any) -> None:
        with self.store.engine.begin() as c:
            c.execute(update(m.tracked_players).where(m.tracked_players.c.user_id == uid).values(**values))

    def _last_activity(self, uid: int) -> datetime | None:
        with self.store.engine.connect() as c:
            return c.execute(select(func.max(m.scores.c.ended_at)).where(m.scores.c.user_id == uid)).scalar()

    def requests_last_24h(self) -> int:
        since = self._now() - timedelta(hours=24)
        with self.store.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(m.api_requests)
                                 .where(m.api_requests.c.requested_at >= since)).scalar())

    # --------------------------------------------------------------- seed
    def seed_from_panel(self) -> int:
        """Acrescenta ao acompanhamento os jogadores do painel que ainda lá não estão (idempotente)."""
        if self.panel_file is None or not Path(self.panel_file).exists():
            return 0
        added, now = 0, self._now()
        entries = json.loads(Path(self.panel_file).read_text(encoding="utf-8"))
        with self.store.engine.connect() as c:
            known = {r[0] for r in c.execute(select(m.tracked_players.c.user_id))}
            jobs = {r["user_id"]: r for r in c.execute(select(m.panel_jobs)).mappings()}
        for e in entries:
            uid = int(e["user_id"])
            if uid in EXCLUDED_USER_IDS or uid in known:
                continue
            state = self.store.get_state(f"user:{uid}:osu:recent") or {}
            last_poll = state.get("last_poll_at")
            last_poll_dt = datetime.fromisoformat(last_poll).replace(tzinfo=None) if last_poll else None
            label = e.get("name") or f"{e.get('band')} · rank {e.get('rank_at_snapshot')}"
            with self.store.engine.begin() as c:
                c.execute(m.tracked_players.insert().values(
                    user_id=uid, band=e.get("band"), label=label, status="active",
                    tracked_since=(jobs.get(uid) or {}).get("started_at") or now, last_poll_at=last_poll_dt,
                    next_poll_at=(last_poll_dt + self._jitter(POLL_INTERVAL_H)) if last_poll_dt else now))
            added += 1
        return added

    # ---------------------------------------------------------- inatividade
    def inactive_reason(self, row: dict[str, Any], now: datetime) -> str | None:
        last = self._last_activity(row["user_id"])
        ref = max(t for t in (last, row["tracked_since"]) if t is not None)
        if now - ref >= timedelta(days=INACTIVITY_DAYS):
            return (f"sem atividade há {(now - ref).days} dias (último score: "
                    f"{last.date().isoformat() if last else 'nenhum'})")
        return None

    def mark_inactive(self, row: dict[str, Any], reason: str) -> dict[str, Any]:
        """Para os pedidos a este jogador e tenta pôr outro da mesma banda no lugar."""
        result: dict[str, Any] = {"user_id": row["user_id"], "band": row["band"], "reason": reason,
                                  "replacement": None}
        self._set(row["user_id"], status="inactive", next_poll_at=None, note=reason[:250])
        if row["band"] in NO_AUTO_REPLACE_BANDS or self.candidates is None:
            self._set(row["user_id"], note=(reason + " — sem substituição automática")[:250])
            return result
        with self.store.engine.connect() as c:
            exclude = {r[0] for r in c.execute(select(m.tracked_players.c.user_id))} | EXCLUDED_USER_IDS
        pick = self.candidates.pick(row["band"], exclude)
        if pick is None:
            self._set(row["user_id"], note=(reason + " — sem candidato de reserva nesta banda")[:250])
            return result
        now = self._now()
        with self.store.engine.begin() as c:
            c.execute(m.tracked_players.insert().values(
                user_id=pick["user_id"], band=row["band"],
                label=f"{row['band']} · reserva ({pick.get('source')})", status="active",
                tracked_since=now, next_poll_at=now, replaces=row["user_id"]))
        self._set(row["user_id"], replaced_by=pick["user_id"])
        result["replacement"] = pick["user_id"]
        return result

    # ----------------------------------------------------------------- poll
    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or self._now()
        return [r for r in self._rows(status="active") if r["next_poll_at"] is not None and r["next_poll_at"] <= now]

    def _needs_best(self, uid: int) -> bool:
        return self.store.get_state(f"user:{uid}:osu:snapshot:best") is None

    def poll_once(self, collect_fn: Callable[[int, bool], dict[str, Any]], *,
                  max_players: int = MAX_PLAYERS_PER_RUN, dry_run: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"dry_run": dry_run, "polled": [], "errors": [], "inactive": [], "skipped": None}
        if self.pause_file is not None and Path(self.pause_file).exists():
            out["skipped"] = f"pausado ({self.pause_file} existe; apaga-o ou usa --resume)"
            return out
        if not dry_run:
            self.seed_from_panel()
        used = self.requests_last_24h()
        out["requests_last_24h"] = used
        if used >= DAILY_REQUEST_CAP:
            out["skipped"] = f"teto diário atingido ({used}/{DAILY_REQUEST_CAP} pedidos nas últimas 24 h)"
            return out
        now = self._now()
        due = self.due(now)
        out["due_total"] = len(due)
        batch = due[:max_players]
        if dry_run:
            out["would_poll"] = [
                {"user_id": r["user_id"], "band": r["band"], "needs_best": self._needs_best(r["user_id"]),
                 "overdue_h": round((now - r["next_poll_at"]).total_seconds() / 3600, 1)} for r in batch]
            out["would_be_inactive"] = [
                {"user_id": r["user_id"], "band": r["band"], "reason": why}
                for r in self._rows(status="active") if (why := self.inactive_reason(r, now))]
            return out
        for i, row in enumerate(batch):
            if self.requests_last_24h() >= DAILY_REQUEST_CAP:
                out["skipped"] = "teto diário atingido a meio do lote"
                break
            if self.lock is not None and not self.lock.acquire(timeout=0, settle=self.lock_settle):
                out["skipped"] = "outra recolha à API em curso (bloqueio ocupado); tento na próxima execução"
                break
            uid = row["user_id"]
            need_best = self._needs_best(uid)
            try:
                summary = collect_fn(uid, need_best)
                done_at = self._now()
                self._set(uid, last_poll_at=done_at, next_poll_at=done_at + self._jitter(POLL_INTERVAL_H),
                          last_activity_at=self._last_activity(uid), note=None)
                out["polled"].append({"user_id": uid, "needs_best": need_best, "requests": sum(
                    s.get("requests", 0) for s in summary.get("sources", {}).values())})
                why = self.inactive_reason(self._rows(user_id=uid)[0], done_at)
                if why:
                    out["inactive"].append(self.mark_inactive(self._rows(user_id=uid)[0], why))
            except ApiError as exc:
                if exc.status == 404:  # conta apagada/restrita: deixa de haver pedidos e substitui
                    out["inactive"].append(self.mark_inactive(row, "404 da API (conta indisponível)"))
                else:
                    self._set(uid, next_poll_at=self._now() + self._jitter(RETRY_AFTER_ERROR_H),
                              note=f"erro: {exc}"[:250])
                    out["errors"].append({"user_id": uid, "error": str(exc)[:200]})
            finally:
                if self.lock is not None:
                    self.lock.release()
            if i < len(batch) - 1:
                self._sleep(self._rng.uniform(*PAUSE_BETWEEN_PLAYERS_S))
        return out

    def check_now(self, user_id: int, collect_fn: Callable[[int, bool], dict[str, Any]]) -> dict[str, Any]:
        """Verificação manual de UM jogador (botão do painel Explorar). Mesmas proteções do `poll_once`:
        pausa, teto diário, bloqueio entre processos; jogadores dados como inativos não recebem pedidos."""
        if self.pause_file is not None and Path(self.pause_file).exists():
            return {"error": "recolha pausada (ficheiro PAUSE existe; `osuml poll --resume`)"}
        rows = self._rows(user_id=user_id)
        if rows and rows[0]["status"] == "inactive":
            return {"error": "jogador inativo (≥ 9 dias sem jogar): deixou de receber pedidos"}
        used = self.requests_last_24h()
        if used >= DAILY_REQUEST_CAP:
            return {"error": f"teto diário atingido ({used}/{DAILY_REQUEST_CAP} pedidos nas últimas 24 h)"}
        if self.lock is not None and not self.lock.acquire(timeout=0, settle=self.lock_settle):
            return {"error": "outra recolha à API em curso (bloqueio ocupado); tenta daqui a pouco"}
        try:
            need_best = self._needs_best(user_id)
            summary = collect_fn(user_id, need_best)
            done_at = self._now()
            if rows:
                self._set(user_id, last_poll_at=done_at, next_poll_at=done_at + self._jitter(POLL_INTERVAL_H),
                          last_activity_at=self._last_activity(user_id), note=None)
            return {"ok": True, "needs_best": need_best,
                    "requests": sum(s.get("requests", 0) for s in summary.get("sources", {}).values())}
        except ApiError as exc:
            if exc.status == 404 and rows:
                self.mark_inactive(rows[0], "404 da API (conta indisponível)")
            return {"error": f"erro da API: {exc}"[:250]}
        finally:
            if self.lock is not None:
                self.lock.release()

    # --------------------------------------------------------------- estado
    def status(self) -> dict[str, Any]:
        now = self._now()
        rows = self._rows()
        active = [r for r in rows if r["status"] == "active"]
        nxt = [r["next_poll_at"] for r in active if r["next_poll_at"]]
        return {
            "now": now.isoformat(), "paused": bool(self.pause_file and Path(self.pause_file).exists()),
            "tracked": len(rows), "active": len(active), "inactive": len(rows) - len(active),
            "due_now": len(self.due(now)), "next_due_at": min(nxt).isoformat() if nxt else None,
            "max_hours_since_poll": max((round((now - r["last_poll_at"]).total_seconds() / 3600, 1)
                                         for r in active if r["last_poll_at"]), default=None),
            "requests_last_24h": self.requests_last_24h(), "daily_cap": DAILY_REQUEST_CAP,
            "by_band": {b: sum(1 for r in active if r["band"] == b) for b in dict.fromkeys(r["band"] for r in rows)},
            "inactive_players": [{"user_id": r["user_id"], "band": r["band"], "note": r["note"],
                                  "replaced_by": r["replaced_by"]} for r in rows if r["status"] == "inactive"],
        }


def make_collect_fn(settings: Any, store: Store) -> tuple[Callable[[int, bool], dict[str, Any]], Callable[[], None]]:
    """Um só cliente por execução. `best` só quando o jogador ainda não tem snapshot."""
    from ..api.osu import OsuClient
    from ..collector.scores import ScoreCollector

    osu = OsuClient(settings.client_id, settings.client_secret, user_agent=settings.user_agent,
                    min_interval=max(settings.min_interval_osu, 1.1), base_url=settings.osu_base_url,
                    api_version=settings.api_version)
    collector = ScoreCollector(store, osu, snapshot_ttl=timedelta(days=365), user_ttl=timedelta(days=365))

    def collect(user_id: int, need_best: bool) -> dict[str, Any]:
        return collector.collect(user_id, mode="osu", snapshot_types=("best",) if need_best else ())

    return collect, osu.close
