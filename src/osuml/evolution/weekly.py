"""Semana de treino: **teste semanal**, **sessões** (cada uma com a sua parte de treino) e **recomendações por sessão**.

- **Teste semanal**: 5 mapas **nunca jogados** (sem mods) numa **faixa de estrelas fixa** (guardada à 1.ª vez para as semanas serem comparáveis), escolhidos ao acaso de forma reprodutível por semana
  (mesma semana ⇒ mesmos mapas). O que conta é a **1.ª jogada** de cada mapa: accuracy e se passou — mede o que generalizas, não o que decoraste. Em paralelo há uma série retrospetiva
  (qualquer mapa novo que tenhas jogado na faixa, semana a semana), que já tem histórico.
- **Sessões** (~20 min cada): A consolidar (mapas onde já tens 92-97 %), B esticar (1 mapa acima do teu tecto), C variedade/leitura (mapas novos, estilos diferentes), D DT (1×/semana, AR a subir).
  Cada uma tem aquecimento, bloco principal (os mapas recomendados) e fecho, com regras de tentativas.
- O plano de cada semana é gerado **uma vez** (num fio de fundo, o recomendador demora alguns segundos) e guardado em `data/control/weekly/`; o estado de cada mapa (jogado, accuracy) é recalculado
  sempre a partir das jogadas na base de dados. Nada aqui faz pedidos à API.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

TEST_MAPS = 5
MIN_EVIDENCE_ACC = 0.90
COMFORT_ACC = 0.93
MIN_PASSES_FOR_BAND = 5
HISTORY_WEEKS = 12
BLOCKS = {
    "A": {"title": "Consolidar", "minutes": 20,
          "goal": "Subir 1 a 2 pontos de accuracy em mapas onde já tens 92-97 %.",
          "rules": ["2 a 3 tentativas por mapa; pausa de 1 min entre mapas.", "Se à 3.ª não subiste, passa ao seguinte (não insistas).",
                    "Pára se tiveres 3 tentativas seguidas abaixo da tua média: é cansaço."]},
    "B": {"title": "Esticar", "minutes": 20,
          "goal": "Passar um mapa acima do teu tecto, primeiro com ≥ 90 % e depois com ≥ 93 %.",
          "rules": ["No máximo 5 tentativas no mesmo mapa; depois muda.", "A 1.ª tentativa é para ler o mapa: não procures accuracy.",
                    "Sucesso por degraus: passar → ≥ 90 % → ≥ 93 %."]},
    "C": {"title": "Variedade e leitura", "minutes": 20,
          "goal": "Jogar mapas novos de estilos diferentes, sem repetir (com tags: 3 de estilos que jogas menos, 2 de conforto).",
          "rules": ["1 a 2 tentativas por mapa; a 1.ª é a que conta.", "Opcional: HD nos mapas de ≤ 5,5★ para forçar a leitura."]},
    "D": {"title": "DT (1× por semana)", "minutes": 20,
          "goal": "Tolerar AR mais alto e subir a velocidade de tap, dentro do teu tecto em DT.",
          "rules": ["Só mapas de DT dentro do teu tecto de estrelas e AR.", "A accuracy em DT é outra skill: não a compares com a sem mods."]},
}
WARMUP = {"name": "Aquecimento", "minutes": 4, "what": "2 mapas fáceis (≥ 1★ abaixo do teu nível) a ≥ 97 %, sem procurar score: acerta o timing e a mão."}
CLOSING = {"name": "Fecho", "minutes": 3, "what": "Escreve 1 coisa que falhou (leitura, timing ou aim) e em que mapa; é o ponto de partida da próxima sessão."}


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _day(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).date()
    except ValueError:
        return None


def _mods(p: dict[str, Any]) -> str:
    """Chave dos mods da jogada (NC=DT; NF/SO/CL ignorados): "" = sem mods."""
    from ..recommend.modsview import stable_key

    return stable_key(p.get("mods"))


def _is_nomod(p: dict[str, Any]) -> bool:
    return _mods(p) == ""


def _stars(p: dict[str, Any]) -> float | None:
    s = (p.get("raw") or {}).get("stars")
    return float(s) if s is not None else None


def _median(v: list[float]) -> float | None:
    if not v:
        return None
    s = sorted(v)
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def nomod_stats(plays: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Estrelas de referência a partir das tuas jogadas sem mods: `s90` (P90 dos passes ≥ 93 % nas últimas 8 semanas; senão de sempre) e `s_max` (máx. dos passes ≥ 90 %)."""
    rows = [(p, _stars(p), _day(p["ended_at"])) for p in plays if _is_nomod(p) and p["passed"] and p["accuracy"] and _stars(p)]
    recent = [s for p, s, d in rows if p["accuracy"] >= COMFORT_ACC and d and (today - d).days <= 56]
    allc = [s for p, s, d in rows if p["accuracy"] >= COMFORT_ACC]
    ev = [s for p, s, d in rows if p["accuracy"] >= MIN_EVIDENCE_ACC]
    pool = sorted(recent if len(recent) >= 5 else allc)
    if len(pool) < MIN_PASSES_FOR_BAND or not ev:  # sem evidência suficiente não se inventa uma faixa
        return {"s90": None, "s_max": None, "n": len(rows)}
    return {"s90": round(pool[int(0.9 * (len(pool) - 1))], 2), "s_max": round(max(ev), 2), "n": len(rows)}


def default_band(stats: dict[str, Any]) -> list[float] | None:
    return [round(stats["s90"] - 0.2, 1), round(stats["s90"] + 0.5, 1)] if stats.get("s90") else None


def first_plays(plays: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """1.ª jogada de sempre de cada mapa (com quaisquer mods), por ordem cronológica."""
    first: dict[int, dict[str, Any]] = {}
    for p in sorted(plays, key=lambda q: str(q["ended_at"] or "")):
        first.setdefault(p["beatmap_id"], p)
    return first


def first_time_series(plays: list[dict[str, Any]], band: list[float], today: date, weeks: int = HISTORY_WEEKS) -> list[dict[str, Any]]:
    """Por semana: mapas que jogaste pela 1.ª vez (sem mods) com estrelas na faixa; accuracy mediana dos passes e taxa de passe. Retrospetivo, sem plano."""
    first = first_plays(plays)
    this_week = week_monday(today)
    out = []
    for i in range(weeks - 1, -1, -1):
        ws = this_week - timedelta(weeks=i)
        sel = [p for p in first.values() if _is_nomod(p) and (d := _day(p["ended_at"])) and ws <= d < ws + timedelta(days=7)
               and _stars(p) is not None and band[0] <= _stars(p) <= band[1]]
        ok = [p["accuracy"] for p in sel if p["passed"] and p["accuracy"]]
        out.append({"week": ws.isoformat(), "n": len(sel), "n_pass": len(ok), "pass_rate": round(len(ok) / len(sel), 3) if sel else None,
                    "acc_median": round(_median(ok), 4) if ok else None})
    return out


def _map_status(plays: list[dict[str, Any]], bid: int, mods: str, start: date, end: date) -> dict[str, Any]:
    mine = sorted((p for p in plays if p["beatmap_id"] == bid and _mods(p) == mods and (d := _day(p["ended_at"])) and start <= d < end),
                  key=lambda q: str(q["ended_at"] or ""))
    if not mine:
        return {"played": False}
    passes = [p["accuracy"] for p in mine if p["passed"] and p["accuracy"]]
    return {"played": True, "attempts": len(mine), "first_acc": mine[0]["accuracy"], "first_passed": bool(mine[0]["passed"]),
            "best_acc": max(passes) if passes else None, "passed": bool(passes)}


def evaluate(plan: dict[str, Any], plays: list[dict[str, Any]]) -> dict[str, Any]:
    """Acrescenta ao plano o que já aconteceu nesta semana (estado de cada mapa, progresso das sessões e resumo do teste)."""
    start = date.fromisoformat(plan["week_start"])
    end = start + timedelta(days=7)
    for m in plan["test"]["maps"]:
        m["status"] = _map_status(plays, m["beatmap_id"], "", start, end)
    done = [m["status"] for m in plan["test"]["maps"] if m["status"]["played"]]
    accs = [s["first_acc"] for s in done if s["first_passed"] and s["first_acc"]]
    plan["test"]["summary"] = {"done": len(done), "total": len(plan["test"]["maps"]), "first_pass": sum(s["first_passed"] for s in done),
                               "first_acc_median": round(_median(accs), 4) if accs else None}
    for s in plan["sessions"]:
        for m in s["maps"]:
            m["status"] = _map_status(plays, m["beatmap_id"], m.get("mods") or "", start, end)
        s["progress"] = {"done": sum(m["status"]["played"] for m in s["maps"]), "total": len(s["maps"])}
    return plan


class WeeklyPlanner:
    """Gera (num fio de fundo) e guarda o plano da semana; devolve-o já avaliado contra as jogadas."""

    def __init__(self, recommender: Any | None, control_dir: Path) -> None:
        self.rec = recommender
        self.dir = Path(control_dir) / "weekly"
        self._building: set[str] = set()
        self._lock = threading.Lock()
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------ ficheiros
    def _plan_file(self, uid: int, wid: str) -> Path:
        return self.dir / f"{uid}_{wid}.json"

    def _band_file(self, uid: int) -> Path:
        return self.dir / f"{uid}_test_band.json"

    def test_band(self, uid: int, goal: dict[str, Any], stats: dict[str, Any]) -> list[float] | None:
        """Faixa do teste: `goal.test_band` se existir; senão a guardada; senão calcula-se e guarda-se (fica fixa para as semanas serem comparáveis)."""
        if goal.get("test_band"):
            return [float(goal["test_band"][0]), float(goal["test_band"][1])]
        f = self._band_file(uid)
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))["band"]
        band = default_band(stats)
        if band:
            self.dir.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"band": band, "created_at": datetime.now().isoformat(), "stats": stats}), encoding="utf-8")
        return band

    # ------------------------------------------------------------------ estado
    def state(self, uid: int, plays: list[dict[str, Any]], goal: dict[str, Any], today: date, rejected: set[int] | None = None,
              style_ctx: dict[str, Any] | None = None) -> dict[str, Any] | None:
        stats = nomod_stats(plays, today)
        band = self.test_band(uid, goal, stats)
        out: dict[str, Any] = {"week": week_id(today), "week_start": week_monday(today).isoformat(), "band": band, "stats": stats, "status": "unavailable",
                               "series": first_time_series(plays, band, today) if band else [], "history": self._history(uid, plays, today)}
        if band is None:
            out["message"] = "ainda sem passes sem mods suficientes para definir a tua faixa"
            return out
        wid = week_id(today)
        f = self._plan_file(uid, wid)
        if f.exists():
            plan = json.loads(f.read_text(encoding="utf-8"))
            out.update(status="ready", **evaluate(plan, plays))
            if self.rec is not None and rounds_done(out):  # terminou tudo: nunca fica parado, nasce uma nova ronda com mapas novos
                self._extend_async(uid, wid, plays, goal, rejected, style_ctx)
                out["extending"] = True
            return out
        if self.rec is None:
            out["message"] = "recomendador indisponível: sem plano"
            return out
        with self._lock:
            if wid in self._errors:
                out.update(status="error", message=self._errors[wid])
                return out
            if wid not in self._building:
                self._building.add(wid)
                threading.Thread(target=self._build_thread, args=(uid, plays, goal, today, band, stats, rejected, style_ctx), name="weekly-plan", daemon=True).start()
        out["status"] = "building"
        return out

    def _extend_async(self, uid: int, wid: str, plays, goal, rejected, style_ctx) -> None:
        key = f"{wid}#ext"
        with self._lock:
            if key in self._building:
                return
            self._building.add(key)

        def run() -> None:
            try:
                f = self._plan_file(uid, wid)
                with self._lock:
                    plan = json.loads(f.read_text(encoding="utf-8"))
                if extend_round(self.rec, uid, plan, plays, set(rejected or ()), style_ctx, goal):
                    with self._lock:
                        cur = json.loads(f.read_text(encoding="utf-8"))
                        if max(x.get("round", 1) for x in cur["sessions"]) < max(x.get("round", 1) for x in plan["sessions"]):  # ninguém acrescentou entretanto
                            tmp = f.with_suffix(".tmp")
                            tmp.write_text(json.dumps(plan, ensure_ascii=False, default=str), encoding="utf-8")
                            tmp.replace(f)
            except Exception as exc:  # noqa: BLE001 - extra: nunca deita a página abaixo
                self._errors[f"{wid}#ext"] = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                with self._lock:
                    self._building.discard(key)

        threading.Thread(target=run, name="weekly-round", daemon=True).start()

    def reject(self, uid: int, beatmap_id: int, plays: list[dict[str, Any]], rejected: set[int], today: date) -> dict[str, Any]:
        """"Não serve": troca o mapa no plano guardado desta semana (se ainda por jogar) e grava o plano."""
        if self.rec is None:
            return {"replaced": False, "reason": "recomendador indisponível"}
        f = self._plan_file(uid, week_id(today))
        with self._lock:
            if not f.exists():
                return {"replaced": False, "reason": "ainda sem plano desta semana"}
            plan = json.loads(f.read_text(encoding="utf-8"))
            res = replace_in_plan(self.rec, uid, plan, beatmap_id, plays, rejected)
            if res["replaced"]:
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(plan, ensure_ascii=False, default=str), encoding="utf-8")
                tmp.replace(f)
        return res

    def _history(self, uid: int, plays: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
        """Testes das semanas anteriores (planos guardados), avaliados contra as jogadas dessa semana."""
        hist = []
        for f in sorted(self.dir.glob(f"{uid}_????-W??.json")):
            try:
                plan = evaluate(json.loads(f.read_text(encoding="utf-8")), plays)
            except (OSError, ValueError, KeyError):
                continue
            if plan["week"] == week_id(today):
                continue
            hist.append({"week": plan["week"], "week_start": plan["week_start"], **plan["test"]["summary"], "band": plan["test"]["band"]})
        return hist[-HISTORY_WEEKS:]

    def _build_thread(self, uid, plays, goal, today, band, stats, rejected=None, style_ctx=None) -> None:
        wid = week_id(today)
        try:
            plan = build_plan(self.rec, uid, plays, today, band, stats, rejected, style_ctx, goal)
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._plan_file(uid, wid).with_suffix(".tmp")
            tmp.write_text(json.dumps(plan, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self._plan_file(uid, wid))
        except Exception as exc:  # o plano é um extra: nunca pode deitar a página abaixo
            with self._lock:
                self._errors[wid] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            with self._lock:
                self._building.discard(wid)


def _pick_diverse(cands: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Round-robin pelos eixos dominantes (aim/speed/stamina/reading) para a sessão de variedade não ser tudo do mesmo estilo."""
    by: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        by.setdefault(c["dominant"], []).append(c)
    out: list[dict[str, Any]] = []
    while len(out) < n and any(by.values()):
        for k in list(by):
            if by[k] and len(out) < n:
                out.append(by[k].pop(0))
    return out


def _clean(c: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"beatmap_id": c["beatmap_id"], "label": c["label"], "url": c["url"], "stars": c["stars"], "ar": c.get("ar"), "mods": c.get("mods") or "",
            "p_pass": c.get("p_pass"), "acc_pass": c.get("acc_pass"), **extra}


def _best_nomod(plays: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Melhor passe sem mods (por accuracy) de cada mapa."""
    best: dict[int, dict[str, Any]] = {}
    for p in plays:
        if _is_nomod(p) and p["passed"] and p["accuracy"] and _stars(p):
            if p["beatmap_id"] not in best or p["accuracy"] > best[p["beatmap_id"]]["accuracy"]:
                best[p["beatmap_id"]] = p
    return best


def _mine(p: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"beatmap_id": p["beatmap_id"], "label": p["map"], "url": f"https://osu.ppy.sh/beatmaps/{p['beatmap_id']}", "stars": round(_stars(p), 2), "mods": "", **extra}


def _consolidate(best: dict[int, dict[str, Any]], s90: float, skip: set[int]) -> list[dict[str, Any]]:
    return sorted((p for p in best.values() if 0.92 <= p["accuracy"] < 0.97 and s90 - 0.6 <= _stars(p) <= s90 + 0.3 and p["beatmap_id"] not in skip),
                  key=lambda p: p["accuracy"])


def _dt_candidates(rec: Any, uid: int, ar_cap: float, skip: set[int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lim = rec._limits(uid, "DT") or {}
    if not lim.get("stars_ceiling"):
        return [], lim
    top = lim["stars_ceiling"]
    cands = rec.band_candidates(uid, max(3.5, top - 1.0), top, mods="DT", min_p=0.5, max_ar=ar_cap, n=40 + len(skip), order="p")
    return [c for c in cands if c["ar"] >= 9.4 and c["beatmap_id"] not in skip], lim


def _pick_styled(cands: list[dict[str, Any]], style_ctx: dict[str, Any], n: int = 5, growth: int = 3, rot: int = 0) -> list[dict[str, Any]]:
    """Sessão C com estilos: até `growth` mapas de estilos DISTINTOS (o resto em estilos de conforto, diversificado por eixo). Os estilos de
    crescimento vêm, se houver rival, primeiro do que **o rival joga mais do que tu** (maior diferença de quota, a começar por uma posição que roda
    com `rot` — semana/ronda — para variar), e o último lugar fica para o estilo que **tu** jogas menos (menor z). Sem rival, só o teu z mais baixo.
    Só usa mapas com estilo dominante claro (peso ≥ 0,5) nos estilos de classe."""
    from .styles import CLASS_STYLES, MIN_SHARE_CLASS

    maps, prof, rival = style_ctx["maps"], style_ctx["profile"], style_ctx.get("rival")

    def dom(c: dict[str, Any]) -> str | None:
        w = {k: v for k, v in (maps.get(c["beatmap_id"]) or {}).items() if k in CLASS_STYLES}
        if not w:
            return None
        k = max(w, key=w.get)
        return k if w[k] >= 0.5 else None

    by: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        d = dom(c)
        if d:
            by.setdefault(d, []).append(c)
    primary = prof.get("primary")
    own = [k for k in sorted(by, key=lambda k: prof["z"].get(k, 0.0)) if k != primary]  # os que menos jogas primeiro
    styles_pick: list[str] = []
    if rival:
        gaps = sorted(((rival["share"][k] - prof["share"][k], k) for k in CLASS_STYLES if k in by and k != primary and rival["share"][k] >= MIN_SHARE_CLASS), reverse=True)
        ahead = [k for g, k in gaps if g > 0]
        if ahead:
            r = rot % len(ahead)
            ahead = ahead[r:] + ahead[:r]
        styles_pick = ahead[:max(growth - 1, 0)]
    for k in own:  # completa com o que tu jogas menos
        if len(styles_pick) >= growth:
            break
        if k not in styles_pick:
            styles_pick.append(k)
    picked = [by[k][0] for k in styles_pick[:growth]]
    rest = [c for c in cands if c not in picked and dom(c) in (None, primary)]
    picked += _pick_diverse(rest, n - len(picked))
    return picked[:n]


def _scale(goal: dict[str, Any] | None) -> float:
    """Fator de tamanho da sessão: `goal.json: session_minutes` (20 = padrão; máx. 40)."""
    try:
        return min(max(float((goal or {}).get("session_minutes") or 20) / 20.0, 1.0), 2.0)
    except (TypeError, ValueError):
        return 1.0


def _build_sessions(rec: Any, uid: int, plays: list[dict[str, Any]], band: list[float], stats: dict[str, Any], ar_cap: float, rej: set[int],
                    style_ctx: dict[str, Any] | None, used: set[int], scale: float = 1.0, rot: int = 0, round_no: int = 1) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """As 4 sessões (A consolidar, B esticar, C variedade, D DT) sem repetir mapas de `used` nem rejeitados. Cada ronda nova usa mapas diferentes."""
    skip = set(rej) | set(used)
    s90, s_max = stats["s90"], stats["s_max"]
    ask = lambda lo, hi, n, **kw: [c for c in rec.band_candidates(uid, lo, hi, max_ar=ar_cap, n=n + len(skip), **kw) if c["beatmap_id"] not in skip]  # noqa: E731
    n_a, n_c, n_d = min(round(3 * scale), 5), min(round(5 * scale), 8), min(round(3 * scale), 4)
    minutes = int(round(20 * scale / 5) * 5)
    best = _best_nomod(plays)
    consolidate = _consolidate(best, s90, skip)[:n_a]
    warm = sorted((p for p in best.values() if p["accuracy"] >= 0.97 and _stars(p) <= s90 - 0.8 and p["beatmap_id"] not in rej), key=lambda p: -p["accuracy"])[:2]
    n_b = 1 if scale < 1.5 else 2
    stretch = ask(s_max + 0.1, s_max + 0.8, n_b + 1, mods="", min_p=0.2, order="p")[:n_b + 1]
    if style_ctx:  # com tags: pede-se uma lista larga (os mapas de outros estilos são raros entre os mais prováveis)
        wide = ask(s90 - 0.3, s90 + 0.3, 5000, mods="", min_p=0.5, order="p")
        variety = _pick_styled(wide, style_ctx, n=n_c, growth=min(3 + (1 if scale >= 1.5 else 0), n_c), rot=rot)
    else:
        variety = _pick_diverse(ask(s90 - 0.3, s90 + 0.3, 60, mods="", min_p=0.6, order="p"), n_c)
    dt_all, dt_lim = _dt_candidates(rec, uid, ar_cap, skip)
    dt = dt_all[:n_d]
    sessions = [
        {"key": "A", **BLOCKS["A"], "minutes": minutes, "maps": [_mine(p, best_acc=p["accuracy"], target=round(min(p["accuracy"] + 0.015, 0.99), 3)) for p in consolidate]},
        {"key": "B", **BLOCKS["B"], "minutes": minutes, "maps": [_clean(c) for c in stretch[:n_b]], "alternative": [_clean(c) for c in stretch[n_b:n_b + 1]]},
        {"key": "C", **BLOCKS["C"], "minutes": minutes, "maps": [_clean(c, dominant=c["dominant"]) for c in variety]},
        {"key": "D", **BLOCKS["D"], "minutes": minutes, "maps": [_clean(c) for c in dt],
         "note": f"tecto DT: {dt_lim.get('stars_ceiling')}★, AR até {ar_cap}" if dt else "sem mapas DT dentro dos teus limites esta semana"},
    ]
    if round_no > 1:  # o DT é 1× por semana: as rondas extra só repetem A, B e C
        sessions = [x for x in sessions if x["key"] != "D"]
    for sess in sessions:
        sess["round"] = round_no
        sess["blocks"] = [{**WARMUP, "maps": [_mine(p, best_acc=p["accuracy"]) for p in warm]} if sess["key"] != "D"
                          else {**WARMUP, "what": "2 mapas de ~4★ em DT, sem procurar score: habitua a mão à velocidade.", "maps": []},
                          {"name": "Bloco principal", "minutes": sess["minutes"] - WARMUP["minutes"] - CLOSING["minutes"], "what": sess["goal"], "maps": []}, CLOSING]
    return sessions, dt_lim


def build_plan(rec: Any, uid: int, plays: list[dict[str, Any]], today: date, band: list[float], stats: dict[str, Any],
               rejected: set[int] | None = None, style_ctx: dict[str, Any] | None = None, goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Monta o teste e as sessões desta semana com o recomendador (só leitura local). `rejected` = mapas a que disseste "Não serve" (nunca entram)."""
    rej = set(rejected or ())
    ws = week_monday(today)
    limits = rec._limits(uid, "") or {}
    ar_cap = limits.get("ar_cap") or 10.1
    seed = int(ws.strftime("%Y%m%d")) + int(uid)
    test = [c for c in rec.band_candidates(uid, band[0], band[1], mods="", min_p=0.35, max_ar=ar_cap, n=TEST_MAPS + len(rej), order="random", seed=seed)
            if c["beatmap_id"] not in rej][:TEST_MAPS]
    taken = {m["beatmap_id"] for m in test}
    sessions, dt_lim = _build_sessions(rec, uid, plays, band, stats, ar_cap, rej, style_ctx, taken, _scale(goal), rot=ws.isocalendar()[1], round_no=1)
    return {"week": week_id(today), "week_start": ws.isoformat(), "created_at": datetime.now().isoformat(), "stats": stats,
            "limits": {"ar_cap": ar_cap, "stars_ceiling_nomod": limits.get("stars_ceiling"), "stars_ceiling_dt": dt_lim.get("stars_ceiling")},
            "test": {"band": band, "maps": [_clean(c) for c in test],
                     "how": "Joga cada mapa pela 1.ª vez, sem mods. Só a 1.ª jogada conta (accuracy e se passaste); depois podes repetir à vontade."},
            "sessions": sessions}


MAX_ROUNDS = 4


def rounds_done(plan: dict[str, Any]) -> bool:
    """A, B e C da última ronda completas (o D, DT 1×/semana, é opcional e não trava a ronda seguinte)."""
    last = max((s.get("round", 1) for s in plan["sessions"]), default=1)
    cur = [s for s in plan["sessions"] if s.get("round", 1) == last and s["key"] != "D"]
    return bool(cur) and all(s["progress"]["total"] == 0 or s["progress"]["done"] >= s["progress"]["total"] for s in cur)


def extend_round(rec: Any, uid: int, plan: dict[str, Any], plays: list[dict[str, Any]], rejected: set[int], style_ctx: dict[str, Any] | None,
                 goal: dict[str, Any] | None = None) -> bool:
    """Acrescenta ao plano uma nova ronda (A, B, C, D outra vez) com mapas ainda não usados nesta semana. Devolve False se já há `MAX_ROUNDS`
    ou se a nova ronda não tiver nenhum mapa."""
    last = max((s.get("round", 1) for s in plan["sessions"]), default=1)
    if last >= MAX_ROUNDS:
        return False
    used = _plan_ids(plan)
    ar_cap = (plan.get("limits") or {}).get("ar_cap") or 10.1
    sessions, _ = _build_sessions(rec, uid, plays, plan["test"]["band"], plan["stats"], ar_cap, set(rejected), style_ctx, used, _scale(goal),
                                  rot=date.fromisoformat(plan["week_start"]).isocalendar()[1] + last, round_no=last + 1)
    sessions = [x for x in sessions if x["maps"]]
    if not sessions:
        return False
    plan["sessions"].extend(sessions)
    return True


def _plan_ids(plan: dict[str, Any]) -> set[int]:
    ids = {m["beatmap_id"] for m in plan["test"]["maps"]}
    for sess in plan["sessions"]:
        ids |= {m["beatmap_id"] for m in sess["maps"] + sess.get("alternative", [])}
        for b in sess["blocks"]:
            ids |= {m["beatmap_id"] for m in b.get("maps", [])}
    return ids


def replace_in_plan(rec: Any, uid: int, plan: dict[str, Any], beatmap_id: int, plays: list[dict[str, Any]], rejected: set[int]) -> dict[str, Any]:
    """Troca um mapa a que disseste "Não serve" por outro com o mesmo papel no plano (mesma faixa/mods/sessão), sem repetir nenhum mapa do plano
    nem dos rejeitados. Só troca mapas ainda por jogar. Devolve {"replaced": bool, "new": mapa|None, "reason"?} e altera `plan`."""
    start = date.fromisoformat(plan["week_start"])
    ws = week_monday(start)
    rej = set(rejected) | {beatmap_id}
    used = _plan_ids(plan) | rej
    limits = plan.get("limits") or {}
    ar_cap = limits.get("ar_cap") or 10.1
    stats = plan["stats"]
    s90, s_max = stats["s90"], stats["s_max"]
    n_prev = len(plan.get("rejected", []))
    seed = int(ws.strftime("%Y%m%d")) + int(uid) + 101 * (n_prev + 1)

    def first(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next((c for c in cands if c["beatmap_id"] not in used), None)

    def ask(lo: float, hi: float, **kw: Any) -> list[dict[str, Any]]:
        return rec.band_candidates(uid, lo, hi, max_ar=ar_cap, n=60 + len(used), **kw)

    target_list, role, old = None, None, None
    for m in plan["test"]["maps"]:
        if m["beatmap_id"] == beatmap_id:
            target_list, role, old = plan["test"]["maps"], "test", m
    for sess in plan["sessions"]:
        for key in ("maps", "alternative"):
            for m in sess.get(key, []):
                if m["beatmap_id"] == beatmap_id and target_list is None:
                    target_list, role, old = sess[key], sess["key"], m
    if target_list is None:
        return {"replaced": False, "reason": "o mapa não está no plano desta semana"}
    st = _map_status(plays, beatmap_id, old.get("mods") or "", start, start + timedelta(days=7))
    if st["played"]:
        return {"replaced": False, "reason": "já jogaste este mapa esta semana"}
    new: dict[str, Any] | None = None
    if role == "test":
        new = first(ask(plan["test"]["band"][0], plan["test"]["band"][1], mods="", min_p=0.35, order="random", seed=seed))
        new = _clean(new) if new else None
    elif role == "A":
        p = next(iter(_consolidate(_best_nomod(plays), s90, used)), None)
        new = _mine(p, best_acc=p["accuracy"], target=round(min(p["accuracy"] + 0.015, 0.99), 3)) if p else None
    elif role == "B":
        new = first(ask(s_max + 0.1, s_max + 0.8, mods="", min_p=0.2, order="p"))
        new = _clean(new) if new else None
    elif role == "C":
        cands = [c for c in ask(s90 - 0.3, s90 + 0.3, mods="", min_p=0.6, order="p") if c["beatmap_id"] not in used]
        same = [c for c in cands if c["dominant"] == old.get("dominant")]
        new = (same or cands or [None])[0]
        new = _clean(new, dominant=new["dominant"]) if new else None
    elif role == "D":
        cands, _lim = _dt_candidates(rec, uid, ar_cap, used)
        new = _clean(cands[0]) if cands else None
    i = target_list.index(old)
    if new is None:
        target_list.pop(i)  # sem substituto dentro dos teus limites: remove-se (melhor que insistir num mapa que não serve)
    else:
        target_list[i] = new
    plan.setdefault("rejected", []).append({"beatmap_id": beatmap_id, "role": role, "replaced_by": new["beatmap_id"] if new else None})
    return {"replaced": True, "new": new, "role": role}
