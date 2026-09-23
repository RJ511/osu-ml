"""Acesso à base de dados.

O collector é sequencial (um processo), por isso usamos select-then-insert em
vez de upserts específicos de dialeto: é portável entre SQLite e PostgreSQL.
A PRIMARY KEY em scores.score_id garante, em último caso, que nunca existem
duplicados mesmo que dois processos corram em simultâneo por engano.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Integer, create_engine, func, select, update
from sqlalchemy.engine import Connection, Engine

from ..api.http import HttpResult
from . import models as m
from .normalize import content_hash, normalize_score, parse_dt, strip_volatile
from .raw import RawStore


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class IngestStats:
    received: int = 0
    new: int = 0
    updated: int = 0  # já existia, conteúdo mudou (revision++)
    unchanged: int = 0  # duplicado exato: ignorado
    invalid: int = 0  # sem id/user_id: só fica no raw
    new_beatmaps: int = 0
    new_beatmapsets: int = 0
    min_ended_at: datetime | None = None
    max_ended_at: datetime | None = None
    beatmap_ids: set[int] = field(default_factory=set)

    def merge(self, other: "IngestStats") -> None:
        for k in ("received", "new", "updated", "unchanged", "invalid", "new_beatmaps", "new_beatmapsets"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.beatmap_ids |= other.beatmap_ids
        for attr, fn in (("min_ended_at", min), ("max_ended_at", max)):
            a, b = getattr(self, attr), getattr(other, attr)
            setattr(self, attr, b if a is None else (a if b is None else fn(a, b)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "new": self.new,
            "updated": self.updated,
            "duplicates_ignored": self.unchanged,
            "invalid": self.invalid,
            "new_beatmaps": self.new_beatmaps,
            "new_beatmapsets": self.new_beatmapsets,
            "unique_beatmaps_in_batch": len(self.beatmap_ids),
            "min_ended_at": self.min_ended_at.isoformat() if self.min_ended_at else None,
            "max_ended_at": self.max_ended_at.isoformat() if self.max_ended_at else None,
        }


class Store:
    def __init__(self, database_url: str, raw_dir: Path) -> None:
        if database_url.startswith("sqlite:///"):
            Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(database_url, future=True)
        self.raw = RawStore(raw_dir)
        m.metadata.create_all(self.engine)

    # ------------------------------------------------------------------ runs
    def start_run(self, command: str, target: str | None) -> str:
        run_id = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(m.runs.insert().values(
                run_id=run_id, command=command, target=target, started_at=utcnow(), status="running"
            ))
        return run_id

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(update(m.runs).where(m.runs.c.run_id == run_id).values(
                finished_at=utcnow(), status=status, summary=summary
            ))

    # -------------------------------------------------------------- requests
    def record_request(self, run_id: str, service: str, result: HttpResult) -> int:
        """Guarda o corpo raw e regista o pedido. Chamado ANTES de normalizar:
        se a normalização falhar, a resposta original já está preservada."""
        sha, rel = self.raw.save(service, result.body)
        with self.engine.begin() as c:
            res = c.execute(m.api_requests.insert().values(
                run_id=run_id, service=service, method=result.method, path=result.path,
                params=result.params, status=result.status, attempts=result.attempts,
                duration_ms=result.duration_ms, requested_at=utcnow(),
                raw_sha256=sha, raw_path=rel, raw_bytes=len(result.body),
            ))
            return int(res.inserted_primary_key[0])

    def record_failed_request(self, run_id: str, service: str, method: str, path: str,
                              params: dict | None, status: int | None, error: str) -> int:
        with self.engine.begin() as c:
            res = c.execute(m.api_requests.insert().values(
                run_id=run_id, service=service, method=method, path=path, params=params,
                status=status, requested_at=utcnow(), error=error[:2000],
            ))
            return int(res.inserted_primary_key[0])

    # ----------------------------------------------------------------- users
    def upsert_user(self, raw: dict[str, Any], request_id: int) -> int:
        user_id = int(raw["id"])
        now = utcnow()
        with self.engine.begin() as c:
            exists = c.execute(select(m.users.c.user_id).where(m.users.c.user_id == user_id)).first()
            values = dict(username=raw.get("username", ""), playmode=raw.get("playmode"),
                          raw=raw, fetched_at=now, request_id=request_id)
            if exists:
                c.execute(update(m.users).where(m.users.c.user_id == user_id).values(**values))
            else:
                c.execute(m.users.insert().values(user_id=user_id, first_seen_at=now, **values))
        return user_id

    def find_user(self, username: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(
                select(m.users).where(func.lower(m.users.c.username) == username.lower())
            ).mappings().first()
            return dict(row) if row else None

    # ---------------------------------------------------------------- scores
    def ingest_scores(self, objs: Iterable[dict[str, Any]], source: str, request_id: int) -> IngestStats:
        stats = IngestStats()
        now = utcnow()
        seen_in_batch: set[int] = set()
        with self.engine.begin() as c:
            for obj in objs:
                stats.received += 1
                row = normalize_score(obj)
                if row is None:
                    stats.invalid += 1
                    continue
                if row["score_id"] in seen_in_batch:  # repetido na mesma resposta
                    stats.unchanged += 1
                    continue
                seen_in_batch.add(row["score_id"])
                self._store_embedded_beatmap(c, obj, request_id, now, stats)
                score_only = strip_volatile(obj)
                h = content_hash(score_only)
                existing = c.execute(
                    select(m.scores.c.content_sha256, m.scores.c.revision)
                    .where(m.scores.c.score_id == row["score_id"])
                ).first()
                if existing is None:
                    c.execute(m.scores.insert().values(
                        **row, raw=score_only, content_sha256=h, revision=1,
                        first_source=source, first_seen_at=now, last_seen_at=now,
                    ))
                    stats.new += 1
                elif existing.content_sha256 != h:
                    c.execute(update(m.scores).where(m.scores.c.score_id == row["score_id"]).values(
                        **row, raw=score_only, content_sha256=h,
                        revision=existing.revision + 1, last_seen_at=now,
                    ))
                    stats.updated += 1
                else:
                    c.execute(update(m.scores).where(m.scores.c.score_id == row["score_id"]).values(last_seen_at=now))
                    stats.unchanged += 1
                c.execute(m.score_observations.insert().values(
                    score_id=row["score_id"], source=source, request_id=request_id, observed_at=now
                ))
                if row["beatmap_id"] is not None:
                    stats.beatmap_ids.add(row["beatmap_id"])
                ended = row["ended_at"]
                if ended is not None:
                    stats.min_ended_at = ended if stats.min_ended_at is None else min(stats.min_ended_at, ended)
                    stats.max_ended_at = ended if stats.max_ended_at is None else max(stats.max_ended_at, ended)
        return stats

    def _store_embedded_beatmap(self, c: Connection, obj: dict[str, Any], request_id: int,
                                now: datetime, stats: IngestStats) -> None:
        """Metadata de beatmap que já vem embutida no score: guardar se ainda
        não existir. Não faz pedidos extra à API."""
        bm = obj.get("beatmap")
        if isinstance(bm, dict) and bm.get("id") is not None:
            if c.execute(select(m.beatmaps.c.beatmap_id).where(m.beatmaps.c.beatmap_id == bm["id"])).first() is None:
                c.execute(m.beatmaps.insert().values(
                    beatmap_id=bm["id"], beatmapset_id=bm.get("beatmapset_id"), mode=bm.get("mode"),
                    version=bm.get("version"), difficulty_rating=bm.get("difficulty_rating"),
                    status=bm.get("status"), checksum=bm.get("checksum"), raw=bm,
                    fetched_at=now, request_id=request_id,
                ))
                stats.new_beatmaps += 1
        bs = obj.get("beatmapset")
        if isinstance(bs, dict) and bs.get("id") is not None:
            if c.execute(select(m.beatmapsets.c.beatmapset_id).where(m.beatmapsets.c.beatmapset_id == bs["id"])).first() is None:
                c.execute(m.beatmapsets.insert().values(
                    beatmapset_id=bs["id"], artist=bs.get("artist"), title=bs.get("title"),
                    creator=bs.get("creator"), status=bs.get("status"), raw=bs,
                    fetched_at=now, request_id=request_id,
                ))
                stats.new_beatmapsets += 1

    # ----------------------------------------------------------------- state
    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(select(m.collector_state.c.value).where(m.collector_state.c.key == key)).first()
            return row[0] if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        now = utcnow()
        with self.engine.begin() as c:
            if c.execute(select(m.collector_state.c.key).where(m.collector_state.c.key == key)).first():
                c.execute(update(m.collector_state).where(m.collector_state.c.key == key).values(value=value, updated_at=now))
            else:
                c.execute(m.collector_state.insert().values(key=key, value=value, updated_at=now))

    def add_gap(self, run_id: str, user_id: int, source: str, start: datetime | None,
                end: datetime | None, reason: str) -> None:
        with self.engine.begin() as c:
            c.execute(m.coverage_gaps.insert().values(
                user_id=user_id, source=source, gap_start=start, gap_end=end,
                reason=reason, detected_at=utcnow(), run_id=run_id,
            ))

    # ---------------------------------------------------------------- report
    def user_report(self, user_id: int) -> dict[str, Any]:
        with self.engine.connect() as c:
            s = m.scores
            total, beatmaps, first, last, fails = c.execute(
                select(
                    func.count(),
                    func.count(func.distinct(s.c.beatmap_id)),
                    func.min(s.c.ended_at),
                    func.max(s.c.ended_at),
                    func.sum(func.cast(s.c.passed == False, Integer)),  # noqa: E712
                ).where(s.c.user_id == user_id)
            ).one()
            by_source = dict(c.execute(
                select(m.score_observations.c.source, func.count(func.distinct(m.score_observations.c.score_id)))
                .join(s, s.c.score_id == m.score_observations.c.score_id)
                .where(s.c.user_id == user_id)
                .group_by(m.score_observations.c.source)
            ).all())
            by_first_source = dict(c.execute(
                select(s.c.first_source, func.count()).where(s.c.user_id == user_id).group_by(s.c.first_source)
            ).all())
            beatmaps_with_meta = c.execute(
                select(func.count(func.distinct(s.c.beatmap_id)))
                .join(m.beatmaps, m.beatmaps.c.beatmap_id == s.c.beatmap_id)
                .where(s.c.user_id == user_id)
            ).scalar()
            gaps = [dict(r) for r in c.execute(
                select(m.coverage_gaps.c.source, m.coverage_gaps.c.gap_start, m.coverage_gaps.c.gap_end,
                       m.coverage_gaps.c.reason, m.coverage_gaps.c.detected_at)
                .where(m.coverage_gaps.c.user_id == user_id)
                .order_by(m.coverage_gaps.c.detected_at)
            ).mappings().all()]
        return {
            "unique_scores": total,
            "unique_beatmaps": beatmaps,
            "beatmaps_with_metadata": beatmaps_with_meta,
            "fails": int(fails or 0),
            "fail_ratio": round((fails or 0) / total, 4) if total else None,
            "chronological_range": [first.isoformat() if first else None, last.isoformat() if last else None],
            "scores_seen_by_source": by_source,
            "scores_first_found_by_source": by_first_source,
            "coverage_gaps": [
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in g.items()} for g in gaps
            ],
        }


__all__ = ["Store", "IngestStats", "utcnow", "parse_dt"]
