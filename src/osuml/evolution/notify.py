"""Notificações da página de Evolução: lembretes de sessão, sequência em risco, rival a subir, dados desatualizados.

Cada aviso tem uma chave (`key`) e só é criado uma vez. Ficam sempre guardados em `data/control/notifications.json` (vistos
na página, com o sino). Entrega ao telemóvel é **opcional e desligada por omissão**: define `OSUML_NTFY_URL` no `.env`
(ex.: `https://ntfy.sh/o-teu-topico-secreto`) para enviar cada aviso também por ntfy. O texto enviado nunca leva dados pessoais
nem tokens: só o título e uma frase sobre o treino. De noite (22h-8h) não há envio ao telemóvel; o aviso fica na página.
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

MAX_KEPT = 60
QUIET_FROM, QUIET_TO = 22, 8


def _ascii(t: str) -> str:
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


class Notifier:
    def __init__(self, path: Path, ntfy_url: str | None = None, now=None, sender=None) -> None:
        self.path = Path(path)
        self.ntfy_url = (ntfy_url if ntfy_url is not None else os.environ.get("OSUML_NTFY_URL", "")).strip()
        self._now = now or datetime.now
        self._send = sender or self._ntfy
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, key: str, title: str, body: str, *, push: bool = True) -> bool:
        """Cria o aviso se a chave for nova. Devolve True se foi criado."""
        with self._lock:
            d = self._load()
            items = d.setdefault("items", [])
            if any(i["key"] == key for i in items):
                return False
            now = self._now()
            items.insert(0, {"key": key, "title": title, "body": body, "at": now.isoformat(timespec="seconds"), "read": False, "pushed": False})
            del items[MAX_KEPT:]
            quiet = now.hour >= QUIET_FROM or now.hour < QUIET_TO
            if push and self.ntfy_url and not quiet:
                try:
                    self._send(title, body)
                    items[0]["pushed"] = True
                except Exception:  # noqa: BLE001 - a entrega nunca pode estragar o painel
                    pass
            self._save(d)
            return True

    def _ntfy(self, title: str, body: str) -> None:
        import httpx

        httpx.post(self.ntfy_url, content=body.encode("utf-8"), timeout=10, headers={"Title": _ascii(title), "User-Agent": "osuml/1.0"})

    def list(self) -> dict[str, Any]:
        items = self._load().get("items", [])
        return {"items": items[:20], "unread": sum(1 for i in items if not i["read"]), "push": bool(self.ntfy_url)}

    def mark_read(self, _body: dict | None = None) -> dict[str, Any]:
        with self._lock:
            d = self._load()
            for i in d.get("items", []):
                i["read"] = True
            self._save(d)
        return self.list()

    def memo(self, name: str, value: Any = None) -> Any:
        """Pequena memória (ex.: último pp do rival) para comparar entre execuções. Sem `value` lê."""
        with self._lock:
            d = self._load()
            m = d.setdefault("memo", {})
            if value is None:
                return m.get(name)
            m[name] = value
            self._save(d)
            return value


def check_events(notifier: Notifier, state: dict[str, Any], today: date | None = None, now: datetime | None = None) -> list[str]:
    """Gera os avisos devidos a partir do estado da página. Devolve as chaves criadas."""
    created: list[str] = []
    now = now or notifier._now()
    today = today or now.date()
    iso = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"

    def add(key: str, title: str, body: str) -> None:
        if notifier.add(key, title, body):
            created.append(key)

    cons, week = state.get("consistency") or {}, state.get("week") or {}
    played_today = today.isoformat() in (cons.get("this_week") or [])
    days, goal = cons.get("this_week_days", 0), cons.get("weekly_days_goal", 3)
    wd = today.weekday()
    # 1) lembrete de sessão (ao fim do dia, se hoje ainda não jogaste e a meta da semana não está cumprida)
    if week.get("status") == "ready" and not played_today and days < goal and 17 <= now.hour < 22:
        nxt = next((s for s in week["sessions"] if s["progress"]["done"] < s["progress"]["total"]), None)
        if nxt:
            add(f"session:{today.isoformat()}", f"Sessão {nxt['key']} · {nxt['title']}",
                f"~{nxt['minutes']} min, {nxt['progress']['total']} mapas. Esta semana: {days}/{goal} dias.")
    # 2) sequência em risco: só quando já não há dias suficientes (hoje incluído) para chegar à meta
    remaining = 6 - wd + (0 if played_today else 1)  # dias que ainda podes jogar esta semana
    if cons.get("streak_weeks", 0) >= 1 and days < goal and (goal - days) >= remaining and wd >= 3:
        add(f"streak:{iso}", "Sequência em risco", f"{days}/{goal} dias; sequência de {cons['streak_weeks']} semanas. Faltam {goal - days} dia(s) e restam {remaining}.")
    # 3) teste semanal por fazer (domingo)
    if wd == 6 and week.get("status") == "ready" and week["test"]["summary"]["done"] < week["test"]["summary"]["total"]:
        s = week["test"]["summary"]
        add(f"test:{iso}", "Teste semanal por fazer", f"{s['done']}/{s['total']} feitos. Só conta a 1.ª jogada de cada mapa.")
    # 4) o rival subiu
    rv = state.get("rival")
    if rv and rv.get("pp"):
        last = notifier.memo(f"rival_pp:{rv['user_id']}")
        notifier.memo(f"rival_pp:{rv['user_id']}", float(rv["pp"]))
        if last and rv["pp"] - last >= 25:
            add(f"rival:{rv['user_id']}:{today.isoformat()}", f"{rv['username']} subiu", f"+{rv['pp'] - last:.0f} pp desde a última vez ({rv['pp']:.0f} pp).")
    # 5) dados desatualizados (no máximo de 3 em 3 dias)
    if (state.get("data_quality") or {}).get("stale") and cons.get("days_since_last") is not None:
        add(f"stale:{today.toordinal() // 3}", "Há quanto tempo não jogas?", f"Última jogada há {cons['days_since_last']} dias. Uma sessão curta (20 min) chega para retomar.")
    return created
