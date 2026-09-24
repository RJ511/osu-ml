"""Configuração lida de variáveis de ambiente (.env suportado).

Os segredos (client secret, tokens) nunca são registados em logs nem escritos
em disco por este módulo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str = field(repr=False)  # nunca aparece em repr/logs
    user_agent: str
    database_url: str
    data_dir: Path
    min_interval_osu: float
    snapshot_ttl_hours: int
    user_ttl_hours: int

    osu_base_url: str = "https://osu.ppy.sh"
    # Versão de resposta da API (header x-api-version). >= 20220705 devolve o
    # objeto Score "novo" (ids unificados stable/lazer, statistics lazer).
    api_version: str = "20240529"

    # Espelho opcional em S3 (osuml sync-s3). Credenciais nunca aqui: o boto3
    # resolve-as sozinho do ambiente (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_PROFILE).
    s3_bucket: str = "osu-ml-skill"
    s3_region: str = "eu-west-1"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @classmethod
    def from_env(cls, env_file: str | None = ".env", require_credentials: bool = True) -> "Settings":
        if env_file:
            load_dotenv(env_file)
        client_id = os.getenv("OSU_CLIENT_ID", "").strip()
        client_secret = os.getenv("OSU_CLIENT_SECRET", "").strip()
        if require_credentials and (not client_id or not client_secret):
            raise RuntimeError(
                "OSU_CLIENT_ID e OSU_CLIENT_SECRET são obrigatórios (ver .env.example)."
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=os.getenv("OSUML_USER_AGENT", "osuml/0.1"),
            database_url=os.getenv("OSUML_DATABASE_URL", "sqlite:///data/osuml.db"),
            data_dir=Path(os.getenv("OSUML_DATA_DIR", "data")),
            min_interval_osu=_float("OSUML_MIN_INTERVAL_OSU", 1.1),
            snapshot_ttl_hours=_int("OSUML_SNAPSHOT_TTL_HOURS", 168),
            user_ttl_hours=_int("OSUML_USER_TTL_HOURS", 24),
            s3_bucket=os.getenv("OSUML_S3_BUCKET", "osu-ml-skill"),
            s3_region=os.getenv("OSUML_S3_REGION", "eu-west-1"),
        )
