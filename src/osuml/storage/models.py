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

beatmap_files = Table(
    "beatmap_files",
    metadata,
    # Ficheiro .osu guardado em data/raw/osu_files/{md5}.osu (content-addressed).
    Column("beatmap_id", BigInteger, primary_key=True, autoincrement=False),
    Column("md5", String(32), nullable=False),
    Column("rel_path", String(512), nullable=False),
    Column("bytes", Integer, nullable=False),
    Column("source", String(32), nullable=False),  # dump | folder | osu_web
    Column("origin", String(1024)),  # ficheiro/arquivo de onde veio
    # True se o MD5 coincide com beatmaps.checksum (a versão do mapa que a API conhece).
    Column("checksum_match", Boolean),
    Column("imported_at", DateTime, nullable=False),
)
Index("ix_beatmap_files_md5", beatmap_files.c.md5)

panel_jobs = Table(
    "panel_jobs",
    metadata,
    # Fila de recolhas multi-jogador controlada pelo painel (`osuml panel`).
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("label", String(128), nullable=False),
    Column("band", String(32)),
    Column("status", String(16), nullable=False),  # queued | running | done | cancelled | failed
    Column("requests", Integer, nullable=False, default=0),
    Column("scores_total", Integer),  # scores únicos do jogador na BD após o job
    Column("error", String(512)),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
)

tracked_players = Table(
    "tracked_players",
    metadata,
    # Jogadores acompanhados continuamente por `osuml poll` (recolha irregular, < 24 h por jogador).
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("band", String(32)),
    Column("label", String(128)),
    Column("status", String(16), nullable=False),  # active | inactive
    Column("tracked_since", DateTime, nullable=False),
    Column("last_poll_at", DateTime),
    Column("next_poll_at", DateTime),  # NULL quando inativo
    Column("last_activity_at", DateTime),  # máx. ended_at dos scores guardados
    Column("replaces", BigInteger),
    Column("replaced_by", BigInteger),
    Column("note", String(256)),
)

map_categories = Table(
    "map_categories",
    metadata,
    # Categorização de cada (mapa, combinação de mods jogada) — feita UMA vez e reutilizada por todos
    # os jogadores (`osuml categorize`). `scheme` = versão da pool de referência + do esquema.
    Column("beatmap_id", BigInteger, primary_key=True, autoincrement=False),
    Column("mods", String(64), primary_key=True, default=""),  # mods_effective (sem CL); "" = nomod
    Column("status", String(16), nullable=False),  # ok | no_file | error
    Column("scheme", String(32), nullable=False),
    Column("raw", JSONType),  # stars, aim, speed, density, reading_visual, tech_entropy, ar, cs, od, hp, n_objects
    Column("scores", JSONType),  # <eixo>_score / <eixo>_grade
    Column("error", String(256)),
    Column("computed_at", DateTime, nullable=False),
)

player_profiles = Table(
    "player_profiles",
    metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("username", String(64)),
    Column("pp", Float),
    Column("global_rank", Integer),
    Column("scheme", String(32), nullable=False),
    Column("n_scores", Integer, nullable=False),
    Column("n_evidence", Integer, nullable=False),  # plays passadas com accuracy >= limiar
    Column("n_missing", Integer, nullable=False),  # plays em mapas sem categoria (sem .osu/erro)
    Column("ratings", JSONType),  # <eixo>_rating (P90), <eixo>_typical (mediana), <eixo>_grade
    Column("computed_at", DateTime, nullable=False),
)


recommendation_feedback = Table(
    "recommendation_feedback",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("beatmap_id", BigInteger, nullable=False),
    Column("verdict", String(16), nullable=False),  # serve | nao_serve
    Column("kind", String(24)),  # novo | rejogar | tentar_de_novo
    Column("skills", String(64)),  # "speed,aim"
    Column("score", Float),
    Column("created_at", DateTime, nullable=False),
)

# Mapas que o jogador não quer que lhe sejam recomendados (preferência; ver Recommender.block_map)
recommendation_blocks = Table(
    "recommendation_blocks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("scope", String(8), nullable=False),  # set (o mapa inteiro, todas as dificuldades) | diff (só esta dificuldade)
    Column("beatmap_id", BigInteger),  # obrigatório se scope=diff; a dificuldade de onde se bloqueou (só informativo se scope=set)
    Column("beatmapset_id", BigInteger),  # obrigatório se scope=set
    Column("label", String(300)),
    Column("note", String(300)),
    Column("created_at", DateTime, nullable=False),
)
Index("ix_recommendation_blocks_user", recommendation_blocks.c.user_id)


# Registo de previsões do recomendador e resultado real (ver recommend/log.py)
prediction_log = Table(
    "prediction_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("beatmap_id", BigInteger, nullable=False),
    Column("kind", String(12), nullable=False),  # recomendacao | sombra
    Column("model_fp", String(16)),
    Column("created_at", DateTime, nullable=False),
    Column("asof", DateTime),  # o perfil só usa passes anteriores a este instante
    Column("skills", String(64)),
    Column("tier", String(12)),
    Column("p_pass", Float),
    Column("acc_pass", Float),
    Column("p_pass_raw", Float),
    Column("acc_pass_raw", Float),
    Column("challenge", Float),  # exigência máxima acima do nível do jogador (4 eixos)
    Column("evaluated_at", DateTime),  # preenchido quando há resultado
    Column("n_attempts", Integer),
    Column("n_lazer_attempts", Integer),
    Column("passed", Boolean),
    Column("first_try_passed", Boolean),
    Column("best_acc", Float),
    Column("n_deaths_possible", Integer),
    Column("n_restarts", Integer),
)
Index("ix_prediction_log_user_map", prediction_log.c.user_id, prediction_log.c.beatmap_id)

shadow_state = Table(
    "shadow_state",
    metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("last_seen_at", DateTime, nullable=False),  # jogadas com first_seen_at <= isto já foram avaliadas
)
