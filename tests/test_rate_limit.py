"""Intervalo mínimo entre TODOS os pedidos, incluindo o pedido de token OAuth (sem rede)."""

from __future__ import annotations

import httpx

from osuml.api.osu import OsuClient
from osuml.api.rate_limit import MinIntervalLimiter


def test_token_request_and_first_api_call_respect_min_interval():
    now = [0.0]
    stamps: list[tuple[float, str]] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def handler(req: httpx.Request) -> httpx.Response:
        stamps.append((now[0], req.url.path))
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400, "token_type": "Bearer"})
        return httpx.Response(200, json={"id": 1, "username": "x"})

    limiter = MinIntervalLimiter(1.1, clock=clock, sleep=sleep)
    osu = OsuClient("1", "secret", user_agent="t", transport=httpx.MockTransport(handler), sleep=sleep, limiter=limiter)
    for name in ("a", "b", "c"):
        osu.get_user(name)
    osu.close()

    times = [t for t, _ in stamps]
    assert [p for _, p in stamps][0] == "/oauth/token"
    gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
    assert len(times) == 4 and all(g >= 1.1 for g in gaps), gaps
