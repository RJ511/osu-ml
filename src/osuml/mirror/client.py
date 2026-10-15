"""Cliente do mirror hinai para `.osu` em falta, meta do top 100 PT e strains. Reaproveita o guard/ritmo do coletor de tags (`TagCollector`).

Regras (decisão do utilizador, 2026-10-15): só se pede o que falta; dentro dos limites do mirror (docs de 2026-10-15: JSON sem limite global,
mas moderação): ≥ 1 s entre pedidos, guard consultado antes e a cada 25 pedidos, tetos diários abaixo."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..tags.collect import GUARD_CHECK_EVERY, MAX_CONSEC_ERRORS, MAX_RETRY_AFTER_S, SLOW_RESPONSE_S, Abort, TagCollector, _Retryable

log = logging.getLogger(__name__)

DAILY_CAPS = {"osu": 1000, "goal": 3, "strains": 300}   # pedidos por dia UTC e por finalidade
OSU_SKIP_DAYS = 30                                      # um mapa que o mirror não tem (404) ou com MD5 diferente não se volta a pedir durante 30 dias
OSU_HEADER = b"osu file format v"


class Usage:
    """Contador diário de pedidos por finalidade, em `data/control/mirror_usage.json` (guarda só os últimos dias)."""

    def __init__(self, path: Path, today=None) -> None:
        self.path = Path(path)
        self._today = today or (lambda: datetime.now(timezone.utc).date().isoformat())

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def used(self, what: str) -> int:
        return int(self._load().get(self._today(), {}).get(what, 0))

    def remaining(self, what: str) -> int:
        return max(DAILY_CAPS[what] - self.used(what), 0)

    def add(self, what: str, n: int = 1) -> None:
        d = self._load()
        day = self._today()
        d.setdefault(day, {})[what] = int(d.get(day, {}).get(what, 0)) + n
        for k in sorted(d)[:-14]:
            d.pop(k, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=1), encoding="utf-8")


class MirrorClient(TagCollector):
    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET com ritmo e tratamento de erros (429/503/rede/≠200 → `_Retryable`); 404 devolve a resposta."""
        self._pace()
        started = self._clock()
        try:
            resp = self.http.get(path, params=params)
        except httpx.HTTPError as exc:
            self.requests += 1
            raise _Retryable(f"rede: {type(exc).__name__}", 30.0) from exc
        self.requests += 1
        took = self._clock() - started
        self.latencies_ms.append(took * 1000)
        self.status_counts[str(resp.status_code)] = self.status_counts.get(str(resp.status_code), 0) + 1
        if took > SLOW_RESPONSE_S:
            self._sleep(took)
        if resp.status_code in (429, 503):
            raise _Retryable(f"HTTP {resp.status_code} {resp.headers.get('x-hinai-forensics', '')}".strip(), float(resp.headers.get("Retry-After") or 120))
        if resp.status_code not in (200, 404):
            raise _Retryable(f"HTTP {resp.status_code}", 60.0)
        return resp

    def calm(self, max_wait_guard_s: float = 1800.0) -> None:
        if not self.wait_for_calm(max_wait_guard_s):
            raise Abort("guard vermelho (dor alta): nada foi pedido; tenta mais tarde")


# ------------------------------------------------------------------ .osu em falta
def _skip_file(store) -> Path:
    return Path(store.raw.raw_dir).parent / "control" / "mirror_osu_skip.json"


def missing_osu(store, priority_user_ids: list[int], include_others: bool = False) -> list[tuple[int, str]]:
    """[(beatmap_id, md5)] dos mapas ranked/loved/approved sem ficheiro e com checksum conhecido, dos jogadores prioritários primeiro (por ordem) e,
    só com `include_others`, dos restantes. Sem os que o mirror já não tinha/diferiam há menos de `OSU_SKIP_DAYS` dias."""
    from sqlalchemy import select

    from ..beatmaps.acquire import wanted_beatmaps
    from ..storage import models as m

    with store.engine.connect() as c:
        status = {int(r[0]): r[1] for r in c.execute(select(m.beatmaps.c.beatmap_id, m.beatmaps.c.status))}
    try:
        skip = json.loads(_skip_file(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        skip = {}
    today = datetime.now(timezone.utc).date()
    recent = {int(k) for k, v in skip.items() if (today - date.fromisoformat(v["at"])).days < OSU_SKIP_DAYS}
    out: list[tuple[int, str]] = []
    seen: set[int] = set()

    def take(wanted) -> None:
        for bid, md5 in sorted(wanted.by_id.items()):
            if bid not in seen and md5 and status.get(bid) in ("ranked", "loved", "approved") and bid not in recent:
                seen.add(bid)
                out.append((bid, md5))

    for uid in priority_user_ids:
        take(wanted_beatmaps(store, uid))
    if include_others:
        take(wanted_beatmaps(store, None))
    return out


def fill_osu_files(client: MirrorClient, store, usage: Usage, todo: list[tuple[int, str]], *, max_wait_guard_s: float = 1800.0, progress=None) -> dict[str, Any]:
    """Pede ao mirror (`/v3/osu/beatmaps/osu/{id}`, cache de 1 h do Cloudflare) cada `.osu` em falta e guarda-o só se o MD5 coincidir com o `checksum` da API.
    Para por teto diário, guard vermelho ou 3 falhas seguidas. Devolve contagens."""
    from ..beatmaps.acquire import _save

    out: dict[str, Any] = {"planned": len(todo), "saved": 0, "not_found": 0, "md5_mismatch": 0, "aborted": None}
    skip_path = _skip_file(store)
    try:
        skip = json.loads(skip_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        skip = {}
    consec = 0
    try:
        client.calm(max_wait_guard_s)
        for i, (bid, md5) in enumerate(todo):
            if usage.remaining("osu") <= 0:
                raise Abort(f"teto diário de .osu atingido ({DAILY_CAPS['osu']})")
            if i and i % GUARD_CHECK_EVERY == 0:
                client.calm(max_wait_guard_s)
            try:
                resp = client.get(f"/v3/osu/beatmaps/osu/{bid}")
                consec = 0
            except _Retryable as exc:
                consec += 1
                if consec >= MAX_CONSEC_ERRORS:
                    raise Abort(f"{consec} falhas seguidas (último: {exc}); parei") from exc
                client._sleep(min(exc.retry_after, MAX_RETRY_AFTER_S))
                continue
            usage.add("osu")
            data = resp.content
            if resp.status_code == 404 or not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER):
                out["not_found"] += 1
                skip[str(bid)] = {"at": datetime.now(timezone.utc).date().isoformat(), "why": "404/ficheiro inválido"}
            elif hashlib.md5(data).hexdigest() != md5:
                out["md5_mismatch"] += 1  # o mirror tem outra versão do ficheiro: não serve para o checksum da API
                skip[str(bid)] = {"at": datetime.now(timezone.utc).date().isoformat(), "why": "md5 diferente"}
            elif _save(store, bid, data, "hinai", f"/v3/osu/beatmaps/osu/{bid}", md5):
                out["saved"] += 1
            if progress is not None:
                progress.update(i + 1, label=f".osu do mirror: {out['saved']} guardados")
    except Abort as exc:
        out["aborted"] = str(exc)
    skip_path.parent.mkdir(parents=True, exist_ok=True)
    skip_path.write_text(json.dumps(skip), encoding="utf-8")
    out["requests"] = client.requests
    return out


# ------------------------------------------------------------------ meta do top 100 PT
def refresh_goal(client: MirrorClient, usage: Usage, goal_path: Path, *, min_age_days: int = 6, force: bool = False) -> dict[str, Any]:
    """Atualiza `goal.json` com o pp do jogador no rank-alvo do país (1 pedido: página 2 do ranking, ranks 51-100, proxy da osu!API com cache de 5 min).
    No máximo 1× em `min_age_days` dias. Guarda `target_history` (medições) para a projeção ver o quanto a meta anda."""
    goal = json.loads(goal_path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"checked": False}
    rank, country = int(goal.get("target_country_rank") or 100), goal.get("country") or "PT"
    last = goal.get("measured_at")
    if last and not force and (datetime.now(timezone.utc).date() - date.fromisoformat(last)).days < min_age_days:
        out["reason"] = f"medida há menos de {min_age_days} dias ({last})"
        return out
    if usage.remaining("goal") <= 0:
        out["reason"] = "teto diário atingido"
        return out
    page = (rank - 1) // 50 + 1
    try:
        client.calm(600.0)
        resp = client.get("/v3/osu/rankings/osu/performance", params={"country": country, "cursor": json.dumps({"page": page})})
    except (Abort, _Retryable) as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    usage.add("goal")
    out["checked"] = True
    if resp.status_code != 200:
        out["reason"] = f"HTTP {resp.status_code}"
        return out
    out.update(apply_ranking(goal_path, resp.json(), rank, country, page))
    return out


def apply_ranking(goal_path: Path, data: dict[str, Any], rank: int, country: str, page: int) -> dict[str, Any]:
    """Escreve em `goal.json` o pp do rank-alvo a partir da resposta do mirror (`data.ranking` = 50 linhas planas com `pp` e `global_rank`)."""
    goal = json.loads(goal_path.read_text(encoding="utf-8"))
    rows = ((data.get("data") or data).get("ranking")) or []
    idx = (rank - 1) % 50
    if len(rows) <= idx or rows[idx].get("pp") is None:
        return {"reason": "resposta sem a linha do rank-alvo"}
    new_pp = float(rows[idx]["pp"])
    gr = rows[idx].get("global_rank")
    today = datetime.now(timezone.utc).date().isoformat()
    hist = list(goal.get("target_history") or [])
    if not hist and goal.get("measured_at") and goal.get("target_pp"):
        hist.append({"at": goal["measured_at"], "pp": goal["target_pp"], "global_rank": goal.get("target_global_rank")})
    hist.append({"at": today, "pp": new_pp, "global_rank": gr})
    prev = hist[-2]["pp"] if len(hist) > 1 else None
    goal.update({"target_pp": round(new_pp, 2), "target_global_rank": gr or goal.get("target_global_rank"), "measured_at": today,
                 "source": f"hinai /v3/osu/rankings/osu/performance?country={country}&cursor={{page:{page}}} (1 pedido, {today})", "target_history": hist[-60:]})
    tmp = goal_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(goal, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(goal_path)
    return {"target_pp": round(new_pp, 2), "previous": prev, "rank": rank}


# ------------------------------------------------------------------ strains
def strains_path(raw_dir: Path, beatmap_id: int, mods: str) -> Path:
    return Path(raw_dir) / f"{beatmap_id}_{(mods or 'NM').replace(',', '')}.json.gz"


def load_strains(raw_dir: Path, beatmap_id: int, mods: str) -> dict[str, Any] | None:
    p = strains_path(raw_dir, beatmap_id, mods)
    if not p.exists():
        return None
    try:
        return json.loads(gzip.decompress(p.read_bytes()))
    except (OSError, ValueError):
        return None


def fetch_strains(client: MirrorClient, usage: Usage, raw_dir: Path, items: list[tuple[int, str]], *, points: int = 120, max_wait_guard_s: float = 600.0) -> dict[str, Any]:
    """Pede e guarda (gz) os strains dos (mapa, mods) que ainda não temos. Uma vez guardados nunca se repetem."""
    todo = [(b, m_) for b, m_ in items if not strains_path(raw_dir, b, m_).exists()]
    out: dict[str, Any] = {"planned": len(todo), "saved": 0, "not_found": 0, "aborted": None}
    consec = 0
    try:
        if todo:
            client.calm(max_wait_guard_s)
        for i, (bid, mods) in enumerate(todo):
            if usage.remaining("strains") <= 0:
                raise Abort(f"teto diário de strains atingido ({DAILY_CAPS['strains']})")
            if i and i % GUARD_CHECK_EVERY == 0:
                client.calm(max_wait_guard_s)
            try:
                resp = client.get(f"/v3/osu/pp/{bid}/strains", params={"points": points, **({"mods": mods.replace(",", "")} if mods else {})})
                consec = 0
            except _Retryable as exc:
                consec += 1
                if consec >= MAX_CONSEC_ERRORS:
                    raise Abort(f"{consec} falhas seguidas (último: {exc}); parei") from exc
                client._sleep(min(exc.retry_after, MAX_RETRY_AFTER_S))
                continue
            usage.add("strains")
            p = strains_path(raw_dir, bid, mods)
            p.parent.mkdir(parents=True, exist_ok=True)
            if resp.status_code == 200:
                p.write_bytes(gzip.compress(resp.content))
                out["saved"] += 1
            else:
                out["not_found"] += 1
                p.write_bytes(gzip.compress(b'{"unavailable": true}'))  # não voltar a pedir
    except Abort as exc:
        out["aborted"] = str(exc)
    out["requests"] = client.requests
    return out


def compact_curve(strains: dict[str, Any] | None, n: int = 40) -> dict[str, Any] | None:
    """Dificuldade ao longo do mapa para a página: por troço, o maior entre aim e speed, normalizado pelo máximo do próprio mapa (0-100), reduzido a `n`
    pontos por máximo; `peak_pct` = em que % do mapa está o pico. None se não há dados (mapa sem strains ou pedido marcado como indisponível)."""
    if not strains or strains.get("unavailable") or not strains.get("skills"):
        return None
    by = {sk["name"]: sk["values"] for sk in strains["skills"] if sk.get("values")}
    aim, speed = by.get("aim") or [], by.get("speed") or []
    m = max(len(aim), len(speed))
    if m < 3:
        return None
    raw = [max(aim[i] if i < len(aim) else 0.0, speed[i] if i < len(speed) else 0.0) for i in range(m)]
    top = max(raw)
    if top <= 0:
        return None
    k = min(n, m)
    curve = [round(100 * max(raw[int(j * m / k):max(int((j + 1) * m / k), int(j * m / k) + 1)]) / top) for j in range(k)]
    return {"curve": curve, "peak_pct": round(100 * (raw.index(top) + 0.5) / m), "length_s": round(float(strains.get("length_ms") or 0) / 1000)}


class StrainsPrefetcher:
    """Pede em fundo (1 fio de cada vez) os strains que faltam dos mapas do plano; o que já está em disco nunca se repete."""

    def __init__(self, store: Any, raw_dir: Path, usage_path: Path, max_per_run: int = 40) -> None:
        import threading

        self.store, self.raw_dir, self.usage_path, self.max_per_run = store, Path(raw_dir), Path(usage_path), max_per_run
        self._lock = threading.Lock()
        self._busy = False
        self.last: dict[str, Any] = {}

    def request(self, items: list[tuple[int, str]]) -> bool:
        import threading

        todo = [(b, m_) for b, m_ in items if not strains_path(self.raw_dir, b, m_).exists()][: self.max_per_run]
        if not todo:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def run() -> None:
            client = MirrorClient(self.store)
            try:
                self.last = fetch_strains(client, Usage(self.usage_path), self.raw_dir, todo)
            except Exception as exc:  # noqa: BLE001 - extra: nunca deita o painel abaixo
                self.last = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            finally:
                client.close()
                with self._lock:
                    self._busy = False

        threading.Thread(target=run, name="strains-prefetch", daemon=True).start()
        return True
