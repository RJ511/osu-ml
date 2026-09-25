"""Registo de previsões e comparação automática com a realidade (0 pedidos à API).

Duas fontes de linhas em `prediction_log`:
- **recomendacao**: cada sugestão devolvida ao jogador (com o modelo que a fez). Quando o jogador joga esse mapa depois, preenche-se o resultado.
- **sombra**: para QUALQUER mapa que o jogador tenha jogado desde a última avaliação (jogadas novas do `poll`/`collect`), prevê-se o que o modelo teria dito com o perfil
  **só até ao início dessas jogadas** e compara-se com o que aconteceu. Os mapas recomendados são poucos e enviesados; a sombra dá milhares de pontos de avaliação.

Resultado de cada par: tentativas, se passou, se passou à 1.ª tentativa, melhor accuracy dos passes e, nas falhas, quantas foram reinício certo / possível morte (teste de HP,
`analysis/fail_points.classify`). **Falhas só existem no lazer** (o stable não as envia): `passed`/`first_try_passed` só se usam nos pares com tentativas do lazer
(`n_lazer_attempts > 0`); a accuracy dos passes usa todos os passes.

O relatório (`report`) devolve calibração de P(passar), viés/erro da accuracy (por exigência e por jogador, com encolhimento) — a base para recalibrar (camada barata) e para uma
correção por jogador, em vez de re-treinar o modelo constantemente.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DEATH_CLASSES = ("pode_ser_morte", "morte_provavel")
SHRINK_K = 20.0  # encolhimento do viés por jogador: n / (n + K)


def _now() -> datetime:
    from ..storage.database import utcnow

    return utcnow()


def log_recommendations(store, user_id: int, items: list[dict[str, Any]], skills: list[str], model_fp: str | None, when: datetime | None = None) -> int:
    """Grava as sugestões devolvidas (uma linha por mapa)."""
    from ..storage import models as m

    when = when or _now()
    rows = [{"user_id": int(user_id), "beatmap_id": int(it["beatmap_id"]), "kind": "recomendacao", "model_fp": model_fp, "created_at": when, "asof": when,
             "skills": ",".join(skills), "tier": it.get("tier"), "p_pass": it.get("p_pass"), "acc_pass": it.get("acc_pass"), "p_pass_raw": it.get("p_pass_raw"),
             "acc_pass_raw": it.get("acc_pass_raw"), "challenge": max(it["delta"].values()) if it.get("delta") else None} for it in items]
    if rows:
        with store.engine.begin() as c:
            c.execute(m.prediction_log.insert(), rows)
    return len(rows)


def _plays(store, user_id: int, beatmap_id: int, after: datetime | None = None, before: datetime | None = None):
    from sqlalchemy import select

    from ..storage import models as m

    sc = m.scores.c
    q = select(sc.passed, sc.accuracy, sc.statistics, sc.mod_acronyms, sc.legacy_score_id, sc.ended_at).where(sc.user_id == user_id, sc.beatmap_id == beatmap_id).order_by(sc.ended_at)
    if after is not None:
        q = q.where(sc.ended_at > after)
    if before is not None:
        q = q.where(sc.ended_at < before)
    with store.engine.connect() as c:
        return c.execute(q).all()


def outcome(plays, hp: float | None) -> dict[str, Any] | None:
    """Resultado de um par a partir das tentativas: (passed, accuracy, statistics, mods, legacy_score_id, ended_at)."""
    from ..analysis.fail_points import classify

    if not plays:
        return None
    lazer = [p for p in plays if p[4] is None]
    basis = lazer or list(plays)  # com tentativas do lazer só se contam essas para passar/1.ª tentativa (o stable não regista falhas)
    passes = [p for p in plays if p[0]]
    deaths = restarts = 0
    for p in lazer:
        if not p[0] and hp is not None:
            k, _ = classify(p[2] or {}, hp, p[3] or "")
            deaths += k in DEATH_CLASSES
            restarts += k == "reinicio_certo"
    return {"n_attempts": len(plays), "n_lazer_attempts": len(lazer), "passed": bool(any(p[0] for p in basis)),
            "first_try_passed": bool(basis[0][0]) if lazer else None, "best_acc": float(max(p[1] or 0.0 for p in passes)) if passes else None,
            "n_deaths_possible": deaths, "n_restarts": restarts}


def _hp_of(rec, beatmap_id: int) -> float | None:
    import numpy as np

    from ..analysis import pass_model as pm

    ids, x = rec._index["ids"], rec._index["x"]
    i = int(np.searchsorted(ids, beatmap_id))
    return float(x[i, pm.MAP_FEATS.index("hp")]) if i < len(ids) and ids[i] == beatmap_id else None


def link_recommendations(store, rec, now: datetime | None = None) -> int:
    """Preenche o resultado das recomendações cujo mapa foi jogado depois de recomendado. Devolve quantas."""
    from sqlalchemy import select

    from ..storage import models as m

    pl = m.prediction_log
    n = 0
    with store.engine.connect() as c:
        pending = c.execute(select(pl.c.id, pl.c.user_id, pl.c.beatmap_id, pl.c.created_at).where(pl.c.kind == "recomendacao", pl.c.evaluated_at.is_(None))).all()
    for rid, uid, bid, created in pending:
        oc = outcome(_plays(store, uid, bid, after=created), _hp_of(rec, bid))
        if oc:
            with store.engine.begin() as c:
                c.execute(pl.update().where(pl.c.id == rid).values(evaluated_at=now or _now(), **oc))
            n += 1
    return n


def shadow_evaluate(store, rec, *, since: datetime | None = None, batch: str = "new", now: datetime | None = None) -> dict[str, int]:
    """Avaliação-sombra das jogadas novas. `batch="new"`: tudo o que entrou na BD desde a última avaliação (por jogador); `batch="day"`: uma passagem por dia (para recuperar o
    histórico desde `since`). O perfil de cada lote usa só passes anteriores à sua primeira jogada."""
    from sqlalchemy import select

    from ..storage import models as m

    sc, pl, ss = m.scores.c, m.prediction_log, m.shadow_state
    now = now or _now()
    with store.engine.connect() as c:
        state = dict(c.execute(select(ss.c.user_id, ss.c.last_seen_at)).all())
        q = select(sc.user_id, sc.beatmap_id, sc.ended_at, sc.first_seen_at).where(sc.beatmap_id.isnot(None), sc.ended_at.isnot(None))
        if since is not None:
            q = q.where(sc.ended_at >= since)
        rows = c.execute(q).all()
    by_user: dict[int, list] = {}
    init: dict[int, datetime] = {}  # jogadores sem estado: só se começa a seguir daqui para a frente (o histórico recupera-se com `since` + batch="day")
    for uid, bid, ended, seen in rows:
        if batch == "new" and since is None and uid not in state:
            init[int(uid)] = max(init.get(int(uid), seen), seen)
            continue
        if batch == "new" and uid in state and seen <= state[uid]:
            continue
        by_user.setdefault(int(uid), []).append((int(bid), ended, seen))
    with store.engine.begin() as c:
        for uid, newest in init.items():
            c.execute(ss.insert().values(user_id=uid, last_seen_at=newest))
    stats = {"users": 0, "pairs": 0, "skipped": 0}
    for uid, items in by_user.items():
        groups: dict[Any, list] = {}
        for bid, ended, seen in items:
            groups.setdefault(ended.date() if batch == "day" else "new", []).append((bid, ended, seen))
        did = False
        for _, g in sorted(groups.items(), key=lambda kv: min(e for _, e, _ in kv[1])):
            asof = min(e for _, e, _ in g)
            maps = sorted({b for b, _, _ in g})
            with store.engine.connect() as c:
                done = {r[0] for r in c.execute(select(pl.c.beatmap_id).where(pl.c.user_id == uid, pl.c.kind == "sombra", pl.c.asof == asof)).all()}
            maps = [b for b in maps if b not in done]
            if not maps:
                continue
            try:
                preds = rec.predict_pairs(uid, asof, maps)
            except ValueError:  # poucos passes antes desta jogada
                stats["skipped"] += len(maps)
                continue
            last = max(e for _, e, _ in g)
            out_rows = []
            for b in maps:
                pr = preds.get(b)
                oc = outcome(_plays(store, uid, b, after=asof - timedelta(seconds=1), before=last + timedelta(seconds=1)), _hp_of(rec, b))
                if pr is None or oc is None:
                    stats["skipped"] += 1
                    continue
                out_rows.append({"user_id": uid, "beatmap_id": b, "kind": "sombra", "model_fp": rec.model_info().get("fingerprint"), "created_at": now, "asof": asof,
                                 "evaluated_at": now, **pr, **oc})
            if out_rows:
                with store.engine.begin() as c:
                    c.execute(pl.insert(), out_rows)
                stats["pairs"] += len(out_rows)
                did = True
        if did or uid in by_user:
            newest = max(seen for _, _, seen in items)
            with store.engine.begin() as c:
                if uid in state:
                    c.execute(ss.update().where(ss.c.user_id == uid).values(last_seen_at=newest))
                else:
                    c.execute(ss.insert().values(user_id=uid, last_seen_at=newest))
            stats["users"] += 1
    return stats


def evaluate_pending(store, rec, *, since: datetime | None = None, batch: str = "new") -> dict[str, Any]:
    """Uma passagem completa: liga as recomendações que já foram jogadas e faz a avaliação-sombra das jogadas novas."""
    linked = link_recommendations(store, rec)
    return {"linked_recommendations": linked, **shadow_evaluate(store, rec, since=since, batch=batch)}


def report(store, *, since: datetime | None = None) -> dict[str, Any]:
    """Previsto vs real: calibração de P(passar) (pares com tentativas do lazer), erro da accuracy ao passar, por exigência e por jogador."""
    import numpy as np
    from sqlalchemy import select

    from ..storage import models as m

    pl = m.prediction_log
    q = select(pl).where(pl.c.evaluated_at.isnot(None))
    if since is not None:
        q = q.where(pl.c.asof >= since)
    with store.engine.connect() as c:
        rows = [dict(r) for r in c.execute(q).mappings().all()]
        names = dict(c.execute(select(m.users.c.user_id, m.users.c.username)).all())
    out: dict[str, Any] = {"n_rows": len(rows), "by_kind": {k: sum(r["kind"] == k for r in rows) for k in ("sombra", "recomendacao")}}
    lz = [r for r in rows if (r["n_lazer_attempts"] or 0) > 0 and r["p_pass"] is not None]
    if len(lz) >= 20:
        p = np.array([r["p_pass"] for r in lz]); y = np.array([1.0 if r["passed"] else 0.0 for r in lz])
        f1 = [r for r in lz if r["first_try_passed"] is not None]
        out["pass"] = {"n": len(lz), "predicted_mean": round(float(p.mean()), 4), "observed": round(float(y.mean()), 4), "brier": round(float(np.mean((p - y) ** 2)), 4),
                       "first_try_observed": round(float(np.mean([bool(r["first_try_passed"]) for r in f1])), 4) if f1 else None,
                       "reliability": [{"range": [lo, hi], "n": int(mm.sum()), "predicted": round(float(p[mm].mean()), 3), "observed": round(float(y[mm].mean()), 3)}
                                       for lo, hi in ((0, .5), (.5, .7), (.7, .8), (.8, .9), (.9, 1.01)) for mm in [(p >= lo) & (p < hi)] if mm.sum() >= 10]}
        att = sum(r["n_attempts"] for r in lz); dth = sum(r["n_deaths_possible"] or 0 for r in lz); rst = sum(r["n_restarts"] or 0 for r in lz)
        out["attempts"] = {"attempts": att, "possible_deaths": dth, "certain_restarts": rst, "pass_attempts": att - sum((r["n_deaths_possible"] or 0) + (r["n_restarts"] or 0) for r in lz)}
    ac = [r for r in rows if r["best_acc"] is not None and r["acc_pass"] is not None]
    if len(ac) >= 20:
        d = np.array([r["acc_pass"] - r["best_acc"] for r in ac])
        chal = np.array([r["challenge"] if r["challenge"] is not None else np.nan for r in ac])
        out["accuracy"] = {"n": len(ac), "bias_pred_minus_real": round(float(d.mean()), 4), "mae": round(float(np.abs(d).mean()), 4),
                           "by_challenge": {lab: {"n": int(mm.sum()), "bias": round(float(d[mm].mean()), 4)} for lab, mm in (("<=0", chal <= 0), ("0..3", (chal > 0) & (chal <= 3)), (">3", chal > 3)) if mm.sum() >= 10}}
        per: dict[int, list[float]] = {}
        for r, di in zip(ac, d):
            per.setdefault(r["user_id"], []).append(float(di))
        out["players_accuracy_bias"] = {names.get(u, str(u)): {"n": len(v), "bias": round(float(np.mean(v)), 4), "shrunk_bias": round(float(np.sum(v) / (len(v) + SHRINK_K)), 4)}
                                        for u, v in sorted(per.items(), key=lambda kv: -len(kv[1])) if len(v) >= 15}
    rec_rows = [r for r in rows if r["kind"] == "recomendacao" and r["passed"] is not None]
    if rec_rows:
        out["recommended"] = {"n": len(rec_rows), "passed": round(float(np.mean([bool(r["passed"]) for r in rec_rows])), 3),
                              "predicted_p_pass": round(float(np.mean([r["p_pass"] for r in rec_rows if r["p_pass"] is not None])), 3)}
    return out
