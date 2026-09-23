"""OAuth2 Client Credentials para a osu!API v2.

Para ler dados públicos de um jogador (perfil, scores) chega um token de
Client Credentials com scope `public`. O token fica só em memória: não é
escrito em disco nem em logs. Um token novo por execução é um custo de 1
pedido, aceitável face ao risco de guardar segredos em disco.
"""

from __future__ import annotations

import time
from typing import Callable

from .http import HttpClient


class ClientCredentialsAuth:
    TOKEN_PATH = "/oauth/token"
    # Renova o token com margem antes de expirar.
    EXPIRY_MARGIN_S = 300

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._http: HttpClient | None = None

    def __repr__(self) -> str:  # nunca expor segredos
        return f"ClientCredentialsAuth(client_id={self._client_id!r}, token={'set' if self._token else 'unset'})"

    def bind(self, http: HttpClient) -> None:
        self._http = http

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def _fetch(self) -> None:
        if self._http is None:
            raise RuntimeError("ClientCredentialsAuth não está ligado a um HttpClient")
        result = self._http.request(
            "POST",
            self.TOKEN_PATH,
            form={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            authenticated=False,
        )
        data = result.json()
        if data.get("token_type", "").lower() != "bearer" or "access_token" not in data:
            raise RuntimeError("Resposta inesperada do endpoint de token")
        self._token = data["access_token"]
        self._expires_at = self._clock() + int(data.get("expires_in", 3600))

    def header(self) -> dict[str, str]:
        if self._token is None or self._clock() >= self._expires_at - self.EXPIRY_MARGIN_S:
            self._fetch()
        return {"Authorization": f"Bearer {self._token}"}
