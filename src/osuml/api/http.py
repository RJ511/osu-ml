"""Cliente HTTP base da osu!API.

Responsabilidades:
- passar cada tentativa pelo rate limiter;
- retry com exponential backoff + jitter em 429, 5xx e erros de rede;
- respeitar o header Retry-After quando existe;
- registar pedidos em log SEM headers (o token nunca é registado);
- devolver o corpo raw (bytes) para ser preservado pela camada de storage.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from .rate_limit import MinIntervalLimiter

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, body: bytes | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Cancelled(Exception):
    """Cancelamento pedido pelo utilizador (painel): nenhum pedido novo é enviado."""


@dataclass
class HttpResult:
    method: str
    url: str
    path: str
    params: dict[str, Any]
    status: int
    body: bytes
    duration_ms: int
    attempts: int
    requested_at: float = field(default_factory=time.time)

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class HttpStats:
    requests: int = 0
    retries: int = 0
    errors: int = 0


class HttpClient:
    def __init__(
        self,
        base_url: str,
        limiter: MinIntervalLimiter,
        user_agent: str,
        *,
        name: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 6,
        backoff_base: float = 2.0,
        backoff_max: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        auth_header: Callable[[], Mapping[str, str]] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self.limiter = limiter
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._auth_header = auth_header
        self.stats = HttpStats()
        # Ligados pelo painel (`osuml panel`): `cancel_check()` é consultado antes de CADA pedido
        # (e de novo depois da espera do limitador); `observer(evento)` recebe todos os pedidos
        # enviados, incluindo o de token OAuth, com o instante real de início.
        self.cancel_check: Callable[[], bool] | None = None
        self.observer: Callable[[dict[str, Any]], None] | None = None
        headers = {"Accept": "application/json", "User-Agent": user_agent}
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _raise_if_cancelled(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise Cancelled("cancelado antes de enviar o pedido")

    def _emit(self, method: str, path: str, start_wall: float, duration_ms: int, status: int | None) -> None:
        if self.observer is not None:
            self.observer({"client": self.name, "method": method, "path": path,
                           "start": start_wall, "duration_ms": duration_ms, "status": status})

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_max)
            except ValueError:
                pass
        # Exponential backoff com "full jitter" entre 50% e 100% do valor.
        delay = min(self.backoff_max, self.backoff_base * (2**attempt))
        return delay * random.uniform(0.5, 1.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> HttpResult:
        params = dict(params or {})
        attempt = 0
        while True:
            self._raise_if_cancelled()
            headers: dict[str, str] = {}
            if authenticated and self._auth_header is not None:
                # Pode disparar o pedido de token (que passa pelo limitador): tem de acontecer
                # ANTES de esperar pelo intervalo deste pedido, senão a chamada à API sai
                # colada ao pedido de token e viola o intervalo mínimo.
                headers.update(self._auth_header())
            self.limiter.wait()
            self._raise_if_cancelled()  # o cancelamento pode ter chegado durante a espera do limitador
            started = time.monotonic()
            started_wall = time.time()
            self.stats.requests += 1
            try:
                resp = self._client.request(
                    method, path, params=params, json=json_body, data=form, headers=headers
                )
            except httpx.TransportError as exc:
                duration = int((time.monotonic() - started) * 1000)
                self._emit(method, path, started_wall, duration, None)
                if attempt >= self.max_retries:
                    self.stats.errors += 1
                    raise ApiError(f"{self.name}: erro de rede persistente em {path}: {exc}") from exc
                delay = self._backoff(attempt, None)
                log.warning(
                    "%s %s %s -> erro de rede (%s) em %dms; retry %d em %.1fs",
                    self.name, method, path, type(exc).__name__, duration, attempt + 1, delay,
                )
                self.stats.retries += 1
                attempt += 1
                self._sleep(delay)
                continue

            duration = int((time.monotonic() - started) * 1000)
            self._emit(method, path, started_wall, duration, resp.status_code)
            # Log sem headers nem corpo: nunca expõe o token.
            log.info("%s %s %s %s -> %d (%dms)", self.name, method, path, params or "", resp.status_code, duration)

            if resp.status_code in RETRYABLE_STATUS:
                if attempt >= self.max_retries:
                    self.stats.errors += 1
                    raise ApiError(
                        f"{self.name}: {resp.status_code} persistente em {path}",
                        status=resp.status_code,
                        body=resp.content,
                    )
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.warning("%s: %d em %s; retry %d em %.1fs", self.name, resp.status_code, path, attempt + 1, delay)
                self.stats.retries += 1
                attempt += 1
                self._sleep(delay)
                continue

            if resp.status_code >= 400:
                self.stats.errors += 1
                raise ApiError(
                    f"{self.name}: {resp.status_code} em {path}",
                    status=resp.status_code,
                    body=resp.content,
                )

            return HttpResult(
                method=method,
                url=str(resp.request.url.copy_with(query=None)),
                path=path,
                params=params,
                status=resp.status_code,
                body=resp.content,
                duration_ms=duration,
                attempts=attempt + 1,
            )
