"""Evolução do jogador (separador "Evolução"): nível por eixo ao longo do tempo, consistência, objetivo e rival.

Só lê a BD local (0 pedidos à API). Reaproveita as jogadas já categorizadas do `Explorer`.
Nível por eixo numa semana = P90 das notas dos passes com accuracy ≥ 90 % nas últimas `WINDOW_WEEKS` semanas
(a mesma regra do perfil do jogador, numa janela deslizante). Com poucas jogadas devolve `None` em vez de um valor
enganador, e as tendências só são chamadas "sobe/desce" acima de um limiar (abaixo é ruído).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..categorize.core import AXES, CONFIDENT_EVIDENCE, EVIDENCE_MIN_ACCURACY, RATING_QUANTILE

WINDOW_WEEKS = 6
SERIES_WEEKS = 26
MIN_EVIDENCE = 5            # jogadas mínimas na janela para mostrar um nível
TREND_WEEKS = 4             # compara com o nível de há 4 semanas
TREND_THRESHOLD = 1.5       # pontos da escala 0-100+ abaixo dos quais é "estável" (ruído)
STALE_DAYS = 3
RIVAL_INACTIVE_DAYS = 14   # um rival que não joga há mais tempo deixa de ser alvo
RIVAL_REACHED = 0.98       # já tens ≥ 98 % dos pp dele: alcançado, escolhe-se outro
RIVAL_HOLD_DAYS = 21       # mínimo entre trocas "por melhor candidato" (evita oscilar)
RIVAL_BETTER = 0.7         # o candidato tem de estar a ≤ 70 % da distância de perfil do rival atual


def _quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _day(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).date()
    except ValueError:
        return None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


class Evolution:
    def __init__(self, explorer: Any, goal_path: Path, today: Any = None, weekly: Any | None = None, notifier: Any | None = None,
                 recommender: Any | None = None, checker: Any | None = None, live: Any | None = None) -> None:
        self.explorer = explorer
        self.notifier, self.recommender, self.checker, self.live = notifier, recommender, checker, live
        self.weekly = weekly  # evolution.weekly.WeeklyPlanner (teste semanal + sessões); opcional
        self.goal_path = Path(goal_path)
        self.rival_path = self.goal_path.parent / "rival.json"
        self._today = today or date.today

    def goal(self) -> dict[str, Any]:
        try:
            return json.loads(self.goal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------ pontos
    @staticmethod
    def levels_by_week(plays: list[dict[str, Any]], today: date) -> dict[str, list[dict[str, Any]]]:
        """Série semanal (últimas `SERIES_WEEKS`) do nível por eixo, janela deslizante de `WINDOW_WEEKS`."""
        evidence = []
        for p in plays:
            d = _day(p["ended_at"])
            if d and p["passed"] and (p["accuracy"] or 0) >= EVIDENCE_MIN_ACCURACY and p["axes"]:
                evidence.append((d, p["axes"]))
        this_week = _week_start(today)
        series: dict[str, list[dict[str, Any]]] = {a: [] for a in (*AXES, "stars")}
        for i in range(SERIES_WEEKS - 1, -1, -1):
            end = this_week - timedelta(weeks=i) + timedelta(days=6)
            start = end - timedelta(weeks=WINDOW_WEEKS) + timedelta(days=1)
            window = [ax for d, ax in evidence if start <= d <= end]
            for a in series:
                vals = [ax[a]["score"] for ax in window if a in ax and ax[a]["score"] is not None]
                series[a].append({
                    "week": (end - timedelta(days=6)).isoformat(), "n": len(vals),
                    "level": round(_quantile(vals, RATING_QUANTILE), 1) if len(vals) >= MIN_EVIDENCE else None})
        return series

    @staticmethod
    def current_levels(series: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, float | None], dict[str, int | None]]:
        """Nível atual por eixo; numa pausa (janela sem jogadas) mantém o último nível conhecido e diz há quantas semanas."""
        levels: dict[str, float | None] = {}
        age: dict[str, int | None] = {}
        for a, pts in series.items():
            levels[a], age[a] = None, None
            for back, pt in enumerate(reversed(pts)):
                if pt["level"] is not None:
                    levels[a], age[a] = pt["level"], back
                    break
        return levels, age

    @staticmethod
    def trend(points: list[dict[str, Any]]) -> dict[str, Any]:
        now = points[-1] if points else None
        before = points[-1 - TREND_WEEKS] if len(points) > TREND_WEEKS else None
        if not now or now["level"] is None or not before or before["level"] is None:
            return {"direction": "sem_dados", "delta": None}
        delta = round(now["level"] - before["level"], 1)
        direction = "estavel" if abs(delta) < TREND_THRESHOLD else ("sobe" if delta > 0 else "desce")
        return {"direction": direction, "delta": delta, "low_confidence": now["n"] < CONFIDENT_EVIDENCE}

    @staticmethod
    def consistency(plays: list[dict[str, Any]], today: date, weekly_days: int) -> dict[str, Any]:
        days = {d for d in (_day(p["ended_at"]) for p in plays) if d}
        this_week = _week_start(today)
        weeks = []
        for i in range(11, -1, -1):
            ws = this_week - timedelta(weeks=i)
            n = sum(1 for k in range(7) if ws + timedelta(days=k) in days)
            weeks.append({"week": ws.isoformat(), "days": n, "met": n >= weekly_days})
        streak = 0
        for w in reversed(weeks[:-1]):  # semanas completas; a atual ainda está a decorrer
            if not w["met"]:
                break
            streak += 1
        if weeks[-1]["met"]:
            streak += 1
        last = max(days) if days else None
        return {
            "weekly_days_goal": weekly_days, "weeks": weeks, "streak_weeks": streak,
            "this_week_days": weeks[-1]["days"],
            "this_week": [(this_week + timedelta(days=k)).isoformat() for k in range(7) if (this_week + timedelta(days=k)) in days],
            "last_played": last.isoformat() if last else None,
            "days_since_last": (today - last).days if last else None,
        }

    def rival(self, me: dict[str, Any], my_levels: dict[str, float | None], exclude: set[int] | None = None,
              active: dict[int, date] | None = None, today: date | None = None) -> dict[str, Any] | None:
        """Jogador acompanhado, com mais pp, de perfil mais parecido (distância nos 4 eixos). `exclude` = ex-rivais já alcançados;
        `active` (user_id → última jogada) tira os que não jogam há mais de RIVAL_INACTIVE_DAYS."""
        if not me.get("pp") or any(my_levels.get(a) is None for a in AXES):
            return None
        best, best_d = None, None
        for p in self.explorer._profiles():
            r = p.get("ratings") or {}
            if p["user_id"] == me["user_id"] or not p.get("pp") or p["pp"] <= me["pp"] or not p.get("confident"):
                continue
            if exclude and p["user_id"] in exclude:
                continue
            if active is not None and today is not None:
                last = active.get(p["user_id"])
                if last is None or (today - last).days > RIVAL_INACTIVE_DAYS:
                    continue
            if any(r.get(f"{a}_rating") is None for a in AXES):
                continue
            dist = math.sqrt(sum((r[f"{a}_rating"] - my_levels[a]) ** 2 for a in AXES))
            if p["pp"] > me["pp"] * 2.0:  # um rival a anos-luz não serve de alvo
                continue
            if best_d is None or dist < best_d:
                best, best_d = p, dist
        if best is None:
            return None
        r = best["ratings"]
        return {"user_id": best["user_id"], "username": best["username"], "pp": best["pp"], "global_rank": best["global_rank"],
                "distance": round(best_d, 1),
                "axes": {a: {"them": round(r[f"{a}_rating"], 1), "me": my_levels[a],
                             "gap": round(r[f"{a}_rating"] - my_levels[a], 1)} for a in AXES}}

    def rival_detail(self, rival_id: int, my_levels: dict[str, float | None], my_plays: list[dict[str, Any]],
                     today: date) -> dict[str, Any]:
        """Tudo sobre o rival: mapas dele ao alcance (nomod, acc >= 90 %, que ainda não joguei), últimas jogadas,
        nível por mês e ritmo (rank e jogadas por mês). Só leitura da BD."""
        data = self.explorer.player(rival_id)
        if data is None:
            return {}
        plays = data["plays"]
        mine = {p["beatmap_id"] for p in my_plays}
        best: dict[int, dict[str, Any]] = {}
        for p in plays:
            if (p["passed"] and p["axes"] and p["beatmap_id"] not in mine and (p["accuracy"] or 0) >= EVIDENCE_MIN_ACCURACY
                    and p["mods_key"] in ("", None, "NM")):
                if p["beatmap_id"] not in best or (p["pp"] or 0) > (best[p["beatmap_id"]]["pp"] or 0):
                    best[p["beatmap_id"]] = p
        maps = []
        for p in best.values():
            gaps = {a: round(p["axes"][a]["score"] - my_levels[a], 1) for a in AXES
                    if a in p["axes"] and my_levels.get(a) is not None}
            maps.append({"beatmap_id": p["beatmap_id"], "map": p["map"], "pp": p["pp"], "accuracy": p["accuracy"],
                         "stars": (p["raw"] or {}).get("stars"), "gaps": gaps,
                         "reach": max((abs(v) for v in gaps.values()), default=None)})
        in_reach = sorted((m for m in maps if m["reach"] is not None and m["reach"] <= 8), key=lambda m: -(m["pp"] or 0))
        recent = [{"when": p["ended_at"], "map": p["map"], "beatmap_id": p["beatmap_id"], "passed": p["passed"],
                   "accuracy": p["accuracy"], "pp": p["pp"], "mods": p["mods_key"] or "",
                   "stars": (p["raw"] or {}).get("stars")} for p in plays[:12]]
        monthly: dict[str, list[dict[str, Any]]] = {}
        for p in plays:
            d = _day(p["ended_at"])
            if d and p["passed"] and (p["accuracy"] or 0) >= EVIDENCE_MIN_ACCURACY and p["axes"]:
                monthly.setdefault(d.strftime("%Y-%m"), []).append(p["axes"])
        levels_m = []
        for mth in sorted(monthly)[-5:]:
            row: dict[str, Any] = {"month": mth, "n": len(monthly[mth])}
            for a in AXES:
                v = [ax[a]["score"] for ax in monthly[mth] if a in ax]
                row[a] = round(_quantile(v, RATING_QUANTILE), 1) if v else None
            levels_m.append(row)
        raw = self._user_raw(rival_id)
        rh = (raw.get("rank_history") or {}).get("data", [])
        n_all = len(plays)
        n_fail = sum(1 for p in plays if not p["passed"])
        return {"maps": in_reach[:8], "maps_total": len(maps), "recent": recent, "monthly_levels": levels_m,
                "rank_90d": ({"from": rh[0], "to": rh[-1]} if len(rh) > 1 else None),
                "plays_per_month": [{"month": x["start_date"][:7], "count": x["count"]}
                                    for x in raw.get("monthly_playcounts", [])][-5:],
                "fail_rate": round(n_fail / n_all, 2) if n_all else None}

    # ------------------------------------------------------------------ estado
    def state(self) -> dict[str, Any]:
        goal = self.goal()
        uid = goal.get("user_id")
        if not uid:
            return {"error": "Sem objetivo configurado (data/control/goal.json)."}
        data = self.explorer.player(int(uid))
        if data is None:
            return {"error": f"Jogador {uid} não existe na base de dados."}
        today = self._today()
        plays, me = data["plays"], data["player"]
        series = self.levels_by_week(plays, today)
        trends = {a: self.trend(series[a]) for a in series}
        levels, levels_age = self.current_levels(series)
        cons = self.consistency(plays, today, int(goal.get("weekly_days") or 3))
        raw = self._user_raw(int(uid))
        pp_now = me.get("pp") or (raw.get("statistics") or {}).get("pp")
        target_pp = goal.get("target_pp")
        stale = cons["days_since_last"] is None or cons["days_since_last"] > STALE_DAYS
        n_recent = sum(1 for p in plays if (d := _day(p["ended_at"])) and (today - d).days <= WINDOW_WEEKS * 7)
        out = {
            "now": datetime.utcnow().isoformat() + "Z",
            "player": {"user_id": int(uid), "username": me["username"], "pp": pp_now, "global_rank": me.get("global_rank"),
                       "country_rank": (raw.get("statistics") or {}).get("country_rank"), "country": me.get("country")},
            "goal": {**goal, "progress": (round(pp_now / target_pp, 3) if pp_now and target_pp else None),
                     "gap_pp": (round(target_pp - pp_now, 1) if pp_now and target_pp else None)},
            "series": series, "trends": trends, "levels": levels, "levels_age_weeks": levels_age,
            "consistency": cons,
            "rank_history": (raw.get("rank_history") or {}).get("data", []),
            "rival": self._rival_with_detail({**me, "user_id": int(uid), "pp": pp_now}, levels, plays, today),
            "week": self._week(int(uid), plays, goal, today),
            "projection": self._projection(pp_now, target_pp, series, cons),
            "styles": self._styles_safe(int(uid)),
            "data_quality": {"stale": stale, "last_check": (data.get("check") or {}).get("last_check_at"),
                             "plays_in_window": n_recent, "window_weeks": WINDOW_WEEKS},
        }
        if self.notifier is not None:
            try:  # avisos: nunca podem deitar a página abaixo
                from .notify import check_events

                check_events(self.notifier, out)
                out["notifications"] = self.notifier.list()
            except Exception:  # noqa: BLE001
                out["notifications"] = {"items": [], "unread": 0, "push": False}
        if self.live is not None:
            out["live"] = {k: v for k, v in self.live.last.items() if k in ("at", "error", "skipped")}
        return out

    # ------------------------------------------------------------------ ações (feedback, verificar)
    def styles(self) -> dict[str, Any] | None:
        """Perfis de estilo (cache de 10 min). None se não houver tags/BD: é um extra, nunca deita a página abaixo."""
        import time

        now = time.time()
        cached = getattr(self, "_styles_cache", None)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            from . import styles as S

            data = {"build": S.build(self.explorer.store), "maps": S.map_styles(self.explorer.store)}
        except Exception as exc:  # noqa: BLE001
            data = {"build": {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:160], "players": {}}, "maps": {}}
        self._styles_cache = (now, data)
        return data

    def styles_state(self, uid: int, rival_id: int | None) -> dict[str, Any]:
        d = self.styles()
        b = d["build"]
        if not b.get("ok"):
            return {"ok": False, "reason": b.get("reason")}
        me = b["players"].get(uid)
        if me is None:
            return {"ok": False, "reason": "poucos mapas com tags jogados para definir o teu estilo"}
        return {"ok": True, "labels": b["styles"], "base": b["base"], "me": me, "rival": b["players"].get(rival_id) if rival_id else None,
                "n_players": len(b["players"])}

    def _verdicts(self, uid: int) -> dict[int, str]:
        from sqlalchemy import select

        from ..storage import models as m

        f = m.recommendation_feedback
        with self.explorer.store.engine.connect() as c:
            rows = c.execute(select(f.c.beatmap_id, f.c.verdict).where(f.c.user_id == uid).order_by(f.c.created_at, f.c.id)).all()
        return {int(b): v for b, v in rows}  # o último veredicto de cada mapa

    def _with_verdicts(self, w: dict[str, Any], uid: int) -> dict[str, Any]:
        if not w or w.get("status") != "ready":
            return w
        import copy

        w = copy.deepcopy(w)
        v = self._verdicts(uid)
        groups = [w["test"]["maps"]] + [s.get(k, []) for s in w["sessions"] for k in ("maps", "alternative")]
        sd = self.styles()
        prof = (sd["build"].get("players") or {}).get(uid)
        from .styles import map_role

        for g in groups:
            for mp in g:
                mp["verdict"] = v.get(int(mp["beatmap_id"]))
                mp["style"] = map_role(sd["maps"].get(int(mp["beatmap_id"])), prof)
        return w

    def give_feedback(self, body: dict) -> dict[str, Any]:
        uid = (self.goal().get("user_id"))
        if not uid or self.recommender is None:
            return {"error": "recomendador indisponível"}
        res = self.recommender.feedback(int(uid), int(body["beatmap_id"]), str(body.get("verdict")), [], kind=str(body.get("kind") or "semana")[:24],
                                        note=str(body.get("note") or "")[:200])
        if self.live is not None and "error" not in res:
            self.live.run_once(force=True)  # liga já o resultado ao registo
        if body.get("verdict") == "nao_serve" and self.weekly is not None and "error" not in res:
            try:  # "Não serve" troca o mapa por outro com o mesmo papel no plano desta semana
                data = self.explorer.player(int(uid))
                rej = {b for b, v in self._verdicts(int(uid)).items() if v == "nao_serve"}
                res["swap"] = self.weekly.reject(int(uid), int(body["beatmap_id"]), data["plays"], rej, self._today())
            except Exception as exc:  # noqa: BLE001
                res["swap"] = {"replaced": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}
        return res

    def check_now(self, _body: dict) -> dict[str, Any]:
        uid = self.goal().get("user_id")
        if not uid or self.checker is None:
            return {"error": "verificação indisponível"}
        res = self.checker.force(int(uid))  # no máximo 1 pedido `recent` à osu!API, com as proteções do poll
        if "error" not in res and self.live is not None:
            res["live"] = self.live.run_once(force=True) is not None
        return res

    def read_notifications(self, body: dict) -> dict[str, Any]:
        return self.notifier.mark_read() if self.notifier is not None else {"items": [], "unread": 0, "push": False}

    def _week(self, uid: int, plays: list[dict[str, Any]], goal: dict[str, Any], today: date) -> dict[str, Any] | None:
        if self.weekly is None:
            return None
        try:
            rej = {b for b, v in self._verdicts(uid).items() if v == "nao_serve"}
            sd = self.styles()
            prof = (sd["build"].get("players") or {}).get(uid)
            rv = (self._rival_state().get("current") or {}).get("user_id")
            ctx = {"maps": sd["maps"], "profile": prof, "rival": (sd["build"].get("players") or {}).get(rv) if rv else None} if prof else None
            return self._with_verdicts(self.weekly.state(uid, plays, goal, today, rejected=rej, style_ctx=ctx), uid)
        except Exception as exc:  # a semana de treino é um extra: nunca pode deitar a página abaixo
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"[:200]}

    def _last_played(self) -> dict[int, date] | None:
        """Última jogada guardada de cada jogador (para saber quem está ativo); None sem BD."""
        try:
            from sqlalchemy import func, select

            from ..storage import models as m

            with self.explorer.store.engine.connect() as c:
                rows = c.execute(select(m.scores.c.user_id, func.max(m.scores.c.ended_at)).group_by(m.scores.c.user_id)).all()
            return {int(u): d for u, e in rows if (d := _day(e))}
        except Exception:  # noqa: BLE001 - sem BD (testes): trata todos como ativos
            return None

    def _rival_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.rival_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_rival_state(self, st: dict[str, Any]) -> None:
        try:
            self.rival_path.parent.mkdir(parents=True, exist_ok=True)
            self.rival_path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def dynamic_rival(self, me: dict[str, Any], levels: dict[str, Any], today: date) -> dict[str, Any] | None:
        """Rival que se reavalia sozinho: mantém-se enquanto fizer sentido e muda quando é alcançado, deixa de jogar ou
        aparece um candidato bem mais parecido (no máximo de 21 em 21 dias). Guarda o histórico em `rival.json`."""
        st = self._rival_state()
        active = self._last_played()
        history = st.get("history", [])
        exclude = {int(h["user_id"]) for h in history if h.get("reason") == "alcançado"}
        profiles = {p["user_id"]: p for p in self.explorer._profiles()}
        cur = st.get("current")
        reason = None
        if cur:
            pr = profiles.get(cur["user_id"])
            last = (active or {}).get(cur["user_id"])
            if pr is None or not pr.get("pp"):
                reason = "sem dados"
            elif me.get("pp") and me["pp"] >= pr["pp"] * RIVAL_REACHED:
                reason = "alcançado"
            elif active is not None and (last is None or (today - last).days > RIVAL_INACTIVE_DAYS):
                reason = "parou de jogar"
        cand = self.rival(me, levels, exclude=exclude | ({cur["user_id"]} if reason else set()), active=active, today=today)
        changed = None
        if cur is None or reason:
            if cand is not None:
                changed = reason or "primeiro rival"
        elif cand is not None and cand["user_id"] != cur["user_id"]:
            held = (today - date.fromisoformat(cur["since"])).days
            r = (profiles[cur["user_id"]].get("ratings")) or {}
            ok = all(r.get(f"{a}_rating") is not None and levels.get(a) is not None for a in AXES)
            cur_d = math.sqrt(sum((r[f"{a}_rating"] - levels[a]) ** 2 for a in AXES)) if ok else None
            if held >= RIVAL_HOLD_DAYS and cur_d and cand["distance"] <= RIVAL_BETTER * cur_d:
                changed = reason = "candidato mais parecido"
        if changed:
            if cur:
                history.append({"user_id": cur["user_id"], "username": cur.get("username"), "since": cur["since"], "until": today.isoformat(), "reason": reason or changed})
            cur = {"user_id": cand["user_id"], "username": cand["username"], "since": today.isoformat(), "pp_at_pick": cand["pp"], "my_pp_at_pick": me.get("pp")}
            st = {"current": cur, "history": history[-10:]}
            self._save_rival_state(st)
            if self.notifier is not None and history and changed != "primeiro rival":
                try:
                    self.notifier.add(f"rival_new:{cand['user_id']}:{today.isoformat()}", f"Novo rival: {cand['username']}",
                                      f"{history[-1]['username']} deixou de ser alvo ({history[-1]['reason']}). {cand['username']} tem {cand['pp']:.0f} pp.")
                except Exception:  # noqa: BLE001
                    pass
        if cur is None:
            return None
        pr = profiles.get(cur["user_id"])
        if pr is None or not pr.get("ratings") or any(pr["ratings"].get(f"{a}_rating") is None for a in AXES) or any(levels.get(a) is None for a in AXES):
            return None
        r = pr["ratings"]
        dist = math.sqrt(sum((r[f"{a}_rating"] - levels[a]) ** 2 for a in AXES))
        return {"user_id": pr["user_id"], "username": pr["username"], "pp": pr["pp"], "global_rank": pr["global_rank"], "distance": round(dist, 1),
                "axes": {a: {"them": round(r[f"{a}_rating"], 1), "me": levels[a], "gap": round(r[f"{a}_rating"] - levels[a], 1)} for a in AXES},
                "since": cur["since"], "days_as_rival": (today - date.fromisoformat(cur["since"])).days,
                "pp_gap": round(pr["pp"] - me["pp"], 1) if me.get("pp") else None,
                "pp_gap_at_pick": round(cur["pp_at_pick"] - cur["my_pp_at_pick"], 1) if cur.get("my_pp_at_pick") and cur.get("pp_at_pick") else None,
                "history": st.get("history", history)[-3:]}

    def _rival_with_detail(self, me: dict[str, Any], levels: dict[str, Any], plays: list[dict[str, Any]], today: date):
        rv = self.dynamic_rival(me, levels, today)
        if rv is not None:
            rv["detail"] = self.rival_detail(rv["user_id"], levels, plays, today)
        return rv

    # ------------------------------------------------------------------ projeção da meta
    def projection(self, pp_now: float | None, target_pp: float | None, series: dict[str, list[dict[str, Any]]], cons: dict[str, Any]) -> dict[str, Any]:
        from .projection import fit_pp_vs_level, mean_level_series, project

        pts = []
        for p in self.explorer._profiles():
            r = p.get("ratings") or {}
            if p.get("confident") and p.get("pp") and all(r.get(f"{a}_rating") is not None for a in AXES):
                pts.append((sum(r[f"{a}_rating"] for a in AXES) / len(AXES), float(p["pp"])))
        active_weeks = [w["days"] for w in cons["weeks"] if w["days"] > 0]
        days_hist = sum(active_weeks) / len(active_weeks) if active_weeks else None
        return project(pp_now, target_pp, fit_pp_vs_level(pts), mean_level_series({a: series[a] for a in AXES}, AXES), days_hist)

    def _styles_safe(self, uid: int) -> dict[str, Any]:
        try:
            rv = self._rival_state().get("current") or {}
            return self.styles_state(uid, rv.get("user_id"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}

    def _projection(self, pp_now: Any, target_pp: Any, series: Any, cons: Any) -> dict[str, Any]:
        try:
            return self.projection(pp_now, target_pp, series, cons)
        except Exception as exc:  # noqa: BLE001 - extra: nunca deita a página abaixo
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}

    def _user_raw(self, user_id: int) -> dict[str, Any]:
        from sqlalchemy import select

        from ..storage import models as m

        with self.explorer.store.engine.connect() as c:
            row = c.execute(select(m.users.c.raw).where(m.users.c.user_id == user_id)).first()
        if not row:
            return {}
        return json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
