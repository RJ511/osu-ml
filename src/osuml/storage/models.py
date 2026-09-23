"""Esquema relacional (camada normalizada + auditoria).

Camadas:
- raw: ficheiros .json.gz content-addressed em data/raw/ (ver raw.py),
  referenciados por `api_requests`. Nunca são apagados.
- normalized: users, scores, beatmaps, beatmapsets.
- proveniência/cobertura: score_observations (que fonte viu que score em que
  pedido), coverage_gaps (janelas onde podem faltar scores), collector_state.

Funciona em SQLite (omissão) e PostgreSQL (mudar OSUML_DATABASE_URL).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

JSONType = JSON().with_variant(JSONB(), "postgresql")

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("command", String(64), nullable=False),
    Column("target", String(255)),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime),
    Column("status", String(16), nullable=False, default="running"),
    Column("summary", JSONType),
)

api_requests = Table(
    "api_requests",
    metadata,
    Column("request_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), ForeignKey("runs.run_id")),
    Column("service", String(16), nullable=False),  # osu
    Column("method", String(8), nullable=False),
    Column("path", String(512), nullable=False),
    Column("params", JSONType),
    Column("status", Integer),
    Column("attempts", Integer),
    Column("duration_ms", Integer),
    Column("requested_at", DateTime, nullable=False),
    Column("raw_sha256", String(64)),
    Column("raw_path", String(512)),
    Column("raw_bytes", Integer),
    Column("error", Text),
)

users = Table(
    "users",
    metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("username", String(64), nullable=False),
    Column("playmode", String(16)),
    Column("raw", JSONType, nullable=False),
    Column("first_seen_at", DateTime, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
    Column("request_id", Integer, ForeignKey("api_requests.request_id")),
)
Index("ix_users_username", users.c.username)

scores = Table(
    "scores",
    metadata,
    # `id` do objeto Score (x-api-version >= 20220705): espaço unificado
    # stable+lazer. É a chave de deduplicação.
    Column("score_id", BigInteger, primary_key=True, autoincrement=False),
    Column("user_id", BigInteger, nullable=False),
    Column("beatmap_id", BigInteger),
    Column("ruleset_id", Integer),
    Column("legacy_score_id", BigInteger),
    Column("passed", Boolean),
    Column("accuracy", Float),
    Column("total_score", BigInteger),
    Column("legacy_total_score", BigInteger),
    Column("classic_total_score", BigInteger),
    Column("max_combo", Integer),
    Column("pp", Float),
    Column("rank", String(4)),
    Column("is_perfect_combo", Boolean),
    Column("mods", JSONType),  # lista completa de objetos {acronym, settings}
    Column("mod_acronyms", String(128)),  # "DT,HD" ordenado, para filtros rápidos
    Column("statistics", JSONType),
    Column("maximum_statistics", JSONType),
    Column("started_at", DateTime),
    Column("ended_at", DateTime),
    Column("has_replay", Boolean),
    Column("build_id", Integer),
    Column("raw", JSONType, nullable=False),  # versão mais recente do objeto
    Column("content_sha256", String(64), nullable=False),
    Column("revision", Integer, nullable=False, default=1),  # sobe se o objeto mudar (ex.: recálculo de pp)
    Column("first_source", String(16), nullable=False),
    Column("first_seen_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
)
Index("ix_scores_user_ended", scores.c.user_id, scores.c.ended_at)
Index("ix_scores_beatmap", scores.c.beatmap_id)
Index("ix_scores_legacy", scores.c.legacy_score_id)

score_observations = Table(
    "score_observations",
    metadata,
    Column("observation_id", Integer, primary_key=True, autoincrement=True),
    Column("score_id", BigInteger, ForeignKey("scores.score_id"), nullable=False),
    Column("source", String(16), nullable=False),  # best | firsts | pinned | recent
    Column("request_id", Integer, ForeignKey("api_requests.request_id"), nullable=False),
    Column("observed_at", DateTime, nullable=False),
    UniqueConstraint("score_id", "request_id", name="uq_obs_score_request"),
)
Index("ix_obs_source", score_observations.c.source)

beatmapsets = Table(
    "beatmapsets",
    metadata,
    Column("beatmapset_id", BigInteger, primary_key=True, autoincrement=False),
    Column("artist", String(512)),
    Column("title", String(512)),
    Column("creator", String(255)),
    Column("status", String(32)),
    Column("raw", JSONType, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
    Column("request_id", Integer, ForeignKey("api_requests.request_id")),
)

beatmaps = Table(
    "beatmaps",
    metadata,
    Column("beatmap_id", BigInteger, primary_key=True, autoincrement=False),
    Column("beatmapset_id", BigInteger),
    Column("mode", String(16)),
    Column("version", String(512)),
    Column("difficulty_rating", Float),  # SR sem mods, tal como vem embutido
    Column("status", String(32)),
    Column("checksum", String(64)),
    Column("raw", JSONType, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
    Column("request_id", Integer, ForeignKey("api_requests.request_id")),
)

collector_state = Table(
    "collector_state",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", JSONType, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

coverage_gaps = Table(
    "coverage_gaps",
    metadata,
    Column("gap_id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("source", String(16), nullable=False),
    Column("gap_start", DateTime),
    Column("gap_end", DateTime),
    Column("reason", Text, nullable=False),
    Column("detected_at", DateTime, nullable=False),
    Column("run_id", String(36), ForeignKey("runs.run_id")),
)
