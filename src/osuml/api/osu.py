"""Cliente fino da osu!API v2.

Só expõe os endpoints necessários na Fase 0. Cada método devolve o HttpResult
completo (inclui o corpo raw) para que o collector o possa preservar.

Comportamento verificado no código do osu-web (UsersController / Solo\\Score):
- GET /users/{user}/scores/{type}: type ∈ best, firsts, pinned, recent;
  limit ∈ [1, 100] (omissão: 5); offset para paginação.
- recent: só scores com ended_at nas últimas 24h, e offset+limit <= 100.
  include_fails=1 inclui fails.
- best/firsts/pinned: paginação "infinita" até a página vir vazia/incompleta.
- GET /users/@{username}/{mode}: lookup por username (prefixo @).
"""

from __future__ import annotations

import time
from typing import Callable, Literal

import httpx

from .auth import ClientCredentialsAuth
from .http import ApiError, HttpClient, HttpResult
from .rate_limit import MinIntervalLimiter

ScoreType = Literal["best", "firsts", "pinned", "recent"]
Ruleset = Literal["osu", "taiko", "fruits", "mania"]

RECENT_MAX_RESULTS = 100
PAGE_LIMIT = 100


class OsuClient:
    API_PREFIX = "/api/v2"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        user_agent: str,
        min_interval: float = 1.1,
        base_url: str = "https://osu.ppy.sh",
        api_version: str = "20240529",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: MinIntervalLimiter | None = None,
    ) -> None:
        self.auth = ClientCredentialsAuth(client_id, client_secret)
        self.http = HttpClient(
            base_url,
            limiter or MinIntervalLimiter(min_interval, sleep=sleep),
            user_agent,
            name="osu",
            transport=transport,
            sleep=sleep,
            auth_header=self.auth.header,
            extra_headers={"x-api-version": api_version},
        )
        self.auth.bind(self.http)

    def close(self) -> None:
        self.http.close()

    def _get(self, path: str, params: dict | None = None) -> HttpResult:
        full = f"{self.API_PREFIX}{path}"
        try:
            return self.http.request("GET", full, params=params)
        except ApiError as exc:
            if exc.status == 401:
                # Token expirado/revogado: renova uma vez e repete.
                self.auth.invalidate()
                return self.http.request("GET", full, params=params)
            raise

    def get_user(self, user: str | int, mode: Ruleset | None = None) -> HttpResult:
        ident = str(user) if isinstance(user, int) else f"@{user}"
        path = f"/users/{ident}" + (f"/{mode}" if mode else "")
        return self._get(path)

    def get_user_scores(
        self,
        user_id: int,
        score_type: ScoreType,
        *,
        mode: Ruleset,
        limit: int = PAGE_LIMIT,
        offset: int = 0,
        include_fails: bool = False,
        legacy_only: bool = False,
    ) -> HttpResult:
        params: dict[str, int | str] = {
            "mode": mode,
            "limit": max(1, min(limit, PAGE_LIMIT)),
            "offset": max(0, offset),
            "legacy_only": int(legacy_only),
        }
        if score_type == "recent":
            params["include_fails"] = int(include_fails)
        return self._get(f"/users/{user_id}/scores/{score_type}", params)
