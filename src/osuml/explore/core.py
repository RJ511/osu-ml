"""Consultas só de leitura para o painel "Explorar": pesquisar jogadores e mapas e ver os atributos
(categorias `map_categories` / perfis `player_profiles`) e as plays de cada um. Não faz pedidos à API."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from ..dataset.derived import mods_effective
from ..storage import models as m
from ..storage.database import Store, utcnow

AXES = ("aim", "speed", "stamina", "reading", "stars")
CHECK_COOLDOWN_S = 5.0
_SCORES_PATH = re.compile(r"/users/(\d+)/scores/")
_CAT_KEYS = ("stars", "aim", "speed", "stamina", "reading")


def _j(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _axes_of(scores: dict | None) -> dict | None:
    if not scores:
        return None
    return {a: {"score": scores.get(f"{a}_score"), "grade": scores.get(f"{a}_grade"), "pct": scores.get(f"{a}_pct")}
            for a in _CAT_KEYS if scores.get(f"{a}_score") is not None}


def _utc(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _label(artist: str | None, title: str | None, version: str | None, beatmap_id: int) -> str:
    base = f"{artist} – {title}" if artist and title else (title or f"mapa #{beatmap_id}")
    return f"{base} [{version}]" if version else base


class Explorer:
    def __init__(self, store: Store, cooldown_s: float = CHECK_COOLDOWN_S, catalog: Callable[[], dict | None] | None = None,
                 attributes: Path | None = None) -> None:
        self.store, self.cooldown_s = store, cooldown_s
        self._catalog_fn = catalog  # devolve o índice do recomendador (152 mil mapas): pesquisa e ficha de mapas que nenhum jogador acompanhado jogou
        self.attributes = Path(attributes) if attributes else None  # catalog/v2/map_attributes.parquet: atributos por mapa × mods
        self.attempts: dict[int, datetime] = {}  # última tentativa de verificação manual (mesmo que falhe)

    # ---------------------------------------------------------- verificação
    def _last_checks(self) -> dict[int, datetime]:
        """Última verificação bem-sucedida por jogador: último pedido 200 a `/users/{id}/scores/*`
        (recolhas, `poll` agendado e verificações manuais deixam todos esse rasto em `api_requests`)."""
        with self.store.engine.connect() as c:
            rows = c.execute(select(m.api_requests.c.path, func.max(m.api_requests.c.requested_at))
                             .where(m.api_requests.c.status == 200, m.api_requests.c.path.like("%/users/%/scores/%"))
                             .group_by(m.api_requests.c.path)).all()
        out: dict[int, datetime] = {}
        for path, ts in rows:
            mt = _SCORES_PATH.search(path)
            if mt and ts and (int(mt.group(1)) not in out or ts > out[int(mt.group(1))]):
                out[int(mt.group(1))] = ts
        return out

    def check_info(self, user_id: int, *, last_checks: dict[int, datetime] | None = None) -> dict[str, Any]:
        now = utcnow()
        last = (last_checks if last_checks is not None else self._last_checks()).get(user_id)
        attempt = self.attempts.get(user_id)
        ref = max([t for t in (last, attempt) if t], default=None)
        remaining = max(0.0, self.cooldown_s - (now - ref).total_seconds()) if ref else 0.0
        with self.store.engine.connect() as c:
            tr = c.execute(select(m.tracked_players).where(m.tracked_players.c.user_id == user_id)).mappings().first()
            prof = c.execute(select(m.player_profiles.c.computed_at, m.player_profiles.c.scheme)
                             .where(m.player_profiles.c.user_id == user_id)).mappings().first()
        return {"now": _utc(now), "last_check_at": _utc(last), "last_categorized_at": _utc(prof["computed_at"]) if prof else None,
                "scheme": prof["scheme"] if prof else None, "cooldown_s": self.cooldown_s,
                "cooldown_remaining_s": round(remaining, 2),
                "tracked_status": tr["status"] if tr else None, "next_poll_at": _utc(tr["next_poll_at"]) if tr else None,
                "note": tr["note"] if tr else None}

    # ------------------------------------------------------------ jogadores
    def _profiles(self) -> list[dict[str, Any]]:
        with self.store.engine.connect() as c:
            users = {r["user_id"]: r for r in c.execute(select(m.users.c.user_id, m.users.c.username, m.users.c.raw)).mappings()}
            profs = {r["user_id"]: r for r in c.execute(select(m.player_profiles)).mappings()}
            n_scores = dict(c.execute(select(m.scores.c.user_id, func.count()).group_by(m.scores.c.user_id)).all())
        checks = self._last_checks()
        out = []
        for uid, u in users.items():
            raw = _j(u["raw"]) or {}
            st = raw.get("statistics") or {}
            p = profs.get(uid)
            ratings = _j(p["ratings"]) if p else None
            out.append({
                "user_id": uid, "username": u["username"],
                "pp": (p["pp"] if p and p["pp"] is not None else st.get("pp")),
                "global_rank": (p["global_rank"] if p and p["global_rank"] is not None else st.get("global_rank")),
                "n_scores": n_scores.get(uid, 0),
                "n_evidence": p["n_evidence"] if p else None, "n_missing": p["n_missing"] if p else None,
                "confident": bool(ratings and ratings.get("confident")),
                "grades": {a: ratings.get(f"{a}_grade") for a in AXES} if ratings else {},
                "ratings": ratings, "scheme": p["scheme"] if p else None,
                "last_check_at": _utc(checks.get(uid)),
                "country": (raw.get("country_code") or (raw.get("country") or {}).get("code")),
            })
        return out

    def search_players(self, q: str = "", limit: int = 100) -> dict[str, Any]:
        q = (q or "").strip().lower()
        rows = self._profiles()
        if q:
            rows = [r for r in rows if q in (r["username"] or "").lower() or q == str(r["user_id"])]
        rows.sort(key=lambda r: -(r["pp"] or 0))
        light = [{k: v for k, v in r.items() if k not in ("ratings",)} for r in rows]
        return {"total": len(rows), "items": light[:limit]}

    def player(self, user_id: int) -> dict[str, Any] | None:
        prof = next((r for r in self._profiles() if r["user_id"] == user_id), None)
        if prof is None:
            return None
        with self.store.engine.connect() as c:
            rows = c.execute(
                select(m.scores.c.score_id, m.scores.c.beatmap_id, m.scores.c.mod_acronyms, m.scores.c.passed,
                       m.scores.c.accuracy, m.scores.c.pp, m.scores.c.rank, m.scores.c.max_combo,
                       m.scores.c.ended_at, m.scores.c.legacy_score_id, m.scores.c.total_score)
                .where(m.scores.c.user_id == user_id)).mappings().all()
            bids = {r["beatmap_id"] for r in rows if r["beatmap_id"] is not None}
            meta = {r["beatmap_id"]: r for r in c.execute(
                select(m.beatmaps.c.beatmap_id, m.beatmaps.c.version, m.beatmaps.c.status, m.beatmapsets.c.artist,
                       m.beatmapsets.c.title).select_from(m.beatmaps.outerjoin(
                    m.beatmapsets, m.beatmapsets.c.beatmapset_id == m.beatmaps.c.beatmapset_id))
                .where(m.beatmaps.c.beatmap_id.in_(bids))).mappings()} if bids else {}
            cats = {(r["beatmap_id"], r["mods"]): r for r in c.execute(
                select(m.map_categories).where(m.map_categories.c.beatmap_id.in_(bids))).mappings()} if bids else {}
        plays = []
        for r in rows:
            bid = r["beatmap_id"]
            mods = mods_effective(r["mod_acronyms"])
            mt = meta.get(bid) or {}
            cat = cats.get((bid, mods))
            plays.append({
                "score_id": r["score_id"], "beatmap_id": bid, "map": _label(mt.get("artist"), mt.get("title"), mt.get("version"), bid),
                "map_status": mt.get("status"), "mods": r["mod_acronyms"] or "", "mods_key": mods, "passed": bool(r["passed"]),
                "accuracy": r["accuracy"], "pp": r["pp"], "rank": r["rank"], "max_combo": r["max_combo"],
                "ended_at": r["ended_at"], "lazer": r["legacy_score_id"] is None,
                "cat_status": cat["status"] if cat else None,
                "axes": _axes_of(_j(cat["scores"])) if cat and cat["status"] == "ok" else None,
                "raw": ({k: v for k, v in (_j(cat["raw"]) or {}).items() if k in ("stars", "ar", "cs", "od", "hp", "n_objects")}
                        if cat and cat["status"] == "ok" else None),
            })
        plays.sort(key=lambda p: str(p["ended_at"] or ""), reverse=True)
        return {"player": prof, "plays": plays, "check": self.check_info(user_id)}

    # ---------------------------------------------------------------- mapas
    def _catalog(self) -> dict | None:
        """Índice do catálogo com o texto de pesquisa (artista título dificuldade mapper, em minúsculas) por mapa, calculado uma vez."""
        if self._catalog_fn is None:
            return None
        try:
            cat = self._catalog_fn()
        except Exception:  # noqa: BLE001 — sem índice o Explorar continua a funcionar só com a BD
            return None
        if cat is not None and "hay" not in cat:
            labels = cat["labels"]
            cat["hay"] = [" ".join(str(labels.get(int(b), {}).get(k) or "") for k in ("artist", "title", "version", "creator")).lower() for b in cat["ids"]]
        return cat

    def _catalog_only(self, cat: dict, q: str, db_ids: set[int]) -> list[int]:
        """Posições (no índice) dos mapas do catálogo que casam com a pesquisa e não estão na BD (não jogados pelos jogadores acompanhados)."""
        import numpy as np

        ids = cat["ids"]
        if not q:
            sel = np.arange(len(ids))
        elif q.isdigit():
            sel = np.nonzero((ids == int(q)) | (cat["set_ids"] == int(q)))[0]
        else:
            words = q.lower().split()
            sel = np.array([i for i, h in enumerate(cat["hay"]) if all(w in h for w in words)], dtype=np.int64)
        if not len(sel):
            return []
        keep = sel[~np.isin(ids[sel], list(db_ids))] if db_ids else sel
        return [int(i) for i in keep[np.argsort(-ids[keep])]]  # mapas mais recentes primeiro

    def search_maps(self, q: str = "", limit: int = 100) -> dict[str, Any]:
        q = (q or "").strip()
        plays = select(m.scores.c.beatmap_id.label("bid"), func.count().label("n_plays"),
                       func.count(func.distinct(m.scores.c.user_id)).label("n_players"),
                       func.max(m.scores.c.pp).label("max_pp")).where(m.scores.c.beatmap_id.isnot(None)) \
            .group_by(m.scores.c.beatmap_id).subquery()
        stmt = (select(m.beatmaps.c.beatmap_id, m.beatmaps.c.version, m.beatmaps.c.status, m.beatmaps.c.difficulty_rating,
                       m.beatmapsets.c.artist, m.beatmapsets.c.title, m.beatmapsets.c.creator,
                       plays.c.n_plays, plays.c.n_players, plays.c.max_pp)
                .select_from(m.beatmaps.join(plays, plays.c.bid == m.beatmaps.c.beatmap_id)
                             .outerjoin(m.beatmapsets, m.beatmapsets.c.beatmapset_id == m.beatmaps.c.beatmapset_id)))
        if q:
            if q.isdigit():
                stmt = stmt.where(m.beatmaps.c.beatmap_id == int(q))
            else:
                for word in q.lower().split():
                    stmt = stmt.where(
                        func.lower(func.coalesce(m.beatmapsets.c.artist, "")).contains(word, autoescape=True)
                        | func.lower(func.coalesce(m.beatmapsets.c.title, "")).contains(word, autoescape=True)
                        | func.lower(func.coalesce(m.beatmaps.c.version, "")).contains(word, autoescape=True)
                        | func.lower(func.coalesce(m.beatmapsets.c.creator, "")).contains(word, autoescape=True))
        with self.store.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = c.execute(stmt.order_by(plays.c.n_plays.desc(), m.beatmaps.c.beatmap_id).limit(limit)).mappings().all()
            all_db_ids = {r[0] for r in c.execute(select(stmt.subquery().c.beatmap_id)).all()} if self._catalog_fn is not None else set()
            ids = [r["beatmap_id"] for r in rows]
            stars = {r["beatmap_id"]: _j(r["raw"]) for r in c.execute(
                select(m.map_categories.c.beatmap_id, m.map_categories.c.raw).where(
                    m.map_categories.c.beatmap_id.in_(ids), m.map_categories.c.mods == "",
                    m.map_categories.c.status == "ok")).mappings()} if ids else {}
        items = [{"beatmap_id": r["beatmap_id"], "label": _label(r["artist"], r["title"], r["version"], r["beatmap_id"]),
                  "creator": r["creator"], "status": r["status"], "n_plays": r["n_plays"], "n_players": r["n_players"],
                  "max_pp": r["max_pp"], "stars": (stars.get(r["beatmap_id"]) or {}).get("stars", r["difficulty_rating"])}
                 for r in rows]
        cat = self._catalog()
        if cat is not None:
            extra = self._catalog_only(cat, q, all_db_ids)
            total += len(extra)
            for i in extra[: max(0, limit - len(items))]:
                b = int(cat["ids"][i])
                lab = cat["labels"].get(b, {})
                items.append({"beatmap_id": b, "label": _label(lab.get("artist"), lab.get("title"), lab.get("version"), b), "creator": lab.get("creator"), "status": "catálogo",
                              "n_plays": 0, "n_players": 0, "max_pp": None, "stars": float(cat["x"][i, 0]), "catalog_only": True})
        return {"total": total, "items": items, "catalog": cat is not None, "catalog_size": int(len(cat["ids"])) if cat is not None else None}

    def _catalog_map(self, beatmap_id: int) -> dict[str, Any] | None:
        """Ficha de um mapa do catálogo: nomes do índice, notas por eixo (só sem mods, na escala da pool) e atributos reais por combinação de mods (catálogo v2)."""
        import numpy as np

        from ..beatmaps.skills import grade

        cat = self._catalog()
        if cat is None:
            return None
        i = int(np.searchsorted(cat["ids"], beatmap_id))
        if i >= len(cat["ids"]) or int(cat["ids"][i]) != beatmap_id:
            return None
        lab = cat["labels"].get(beatmap_id, {})
        axes_nomod = {a: {"score": float(cat["axis"][i, k]), "grade": grade(float(cat["axis"][i, k])), "pct": None}
                      for k, a in enumerate(("aim", "speed", "stamina", "reading", "stars"))}
        variants = []
        if self.attributes is not None and self.attributes.exists():
            import pyarrow.parquet as pq

            keys = ("stars", "aim", "speed", "reading", "ar", "cs", "od", "hp", "n_objects", "density")
            for r in pq.read_table(self.attributes, filters=[("beatmap_id", "=", beatmap_id)]).to_pylist():
                variants.append({"mods": r["mods"], "status": "ok", "error": None, "scheme": "catálogo v2",
                                 "axes": axes_nomod if r["mods"] == "" else None, "raw": {k: r.get(k) for k in keys}})
        variants.sort(key=lambda v: (v["mods"] != "", v["mods"]))
        sid = lab.get("set_id") or None
        return {"beatmapset_id": sid, "label": _label(lab.get("artist"), lab.get("title"), lab.get("version"), beatmap_id), "creator": lab.get("creator"),
                "difficulty_rating": float(cat["x"][i, 0]), "variants": variants,
                "url": f"https://osu.ppy.sh/beatmapsets/{sid}#osu/{beatmap_id}" if sid else f"https://osu.ppy.sh/beatmaps/{beatmap_id}"}

    def map(self, beatmap_id: int) -> dict[str, Any] | None:
        with self.store.engine.connect() as c:
            meta = c.execute(
                select(m.beatmaps.c.beatmap_id, m.beatmaps.c.beatmapset_id, m.beatmaps.c.version, m.beatmaps.c.status,
                       m.beatmaps.c.difficulty_rating, m.beatmaps.c.checksum, m.beatmapsets.c.artist, m.beatmapsets.c.title,
                       m.beatmapsets.c.creator).select_from(m.beatmaps.outerjoin(
                    m.beatmapsets, m.beatmapsets.c.beatmapset_id == m.beatmaps.c.beatmapset_id))
                .where(m.beatmaps.c.beatmap_id == beatmap_id)).mappings().first()
            cats = c.execute(select(m.map_categories).where(m.map_categories.c.beatmap_id == beatmap_id)
                             .order_by(m.map_categories.c.mods)).mappings().all()
            has_file = c.execute(select(m.beatmap_files.c.beatmap_id).where(m.beatmap_files.c.beatmap_id == beatmap_id)).first() is not None
            plays = c.execute(
                select(m.scores.c.score_id, m.scores.c.user_id, m.scores.c.mod_acronyms, m.scores.c.passed, m.scores.c.accuracy,
                       m.scores.c.pp, m.scores.c.rank, m.scores.c.max_combo, m.scores.c.ended_at, m.users.c.username)
                .select_from(m.scores.outerjoin(m.users, m.users.c.user_id == m.scores.c.user_id))
                .where(m.scores.c.beatmap_id == beatmap_id)).mappings().all()
            pp_of = {r["user_id"]: r for r in c.execute(select(m.player_profiles.c.user_id, m.player_profiles.c.pp,
                                                              m.player_profiles.c.global_rank)).mappings()}
        variants = []
        for r in cats:
            raw = _j(r["raw"]) or {}
            variants.append({"mods": r["mods"], "status": r["status"], "error": r["error"], "scheme": r["scheme"],
                             "axes": _axes_of(_j(r["scores"])) if r["status"] == "ok" else None,
                             "raw": raw if r["status"] == "ok" else None})
        variants.sort(key=lambda v: (v["mods"] != "", v["mods"]))
        cm = self._catalog_map(beatmap_id) if (meta is None or not variants) else None
        if meta is None and cm is None:
            return None
        note = None
        if cm is not None and not variants:
            variants = cm["variants"]
            note = ("Mapa do catálogo (dumps do data.ppy.sh): atributos por mods calculados localmente. As notas 0–100 só existem sem mods; "
                    "não há plays dos jogadores acompanhados." if meta is None else "Atributos do catálogo (ainda não categorizado para os jogadores acompanhados); notas só sem mods.")
        out_plays = [{
            "score_id": p["score_id"], "user_id": p["user_id"], "username": p["username"] or f"#{p['user_id']}",
            "player_pp": (pp_of.get(p["user_id"]) or {}).get("pp"), "player_rank": (pp_of.get(p["user_id"]) or {}).get("global_rank"),
            "mods": p["mod_acronyms"] or "", "passed": bool(p["passed"]), "accuracy": p["accuracy"], "pp": p["pp"],
            "rank": p["rank"], "max_combo": p["max_combo"], "ended_at": p["ended_at"]} for p in plays]
        out_plays.sort(key=lambda p: -(p["pp"] or -1))
        if meta is None:
            return {"beatmap_id": beatmap_id, "beatmapset_id": cm["beatmapset_id"], "label": cm["label"], "creator": cm["creator"], "status": "catálogo",
                    "difficulty_rating": cm["difficulty_rating"], "has_file": False, "variants": variants, "plays": out_plays, "note": note, "url": cm["url"]}
        sid = meta["beatmapset_id"]
        return {"beatmap_id": beatmap_id, "beatmapset_id": sid,
                "label": _label(meta["artist"], meta["title"], meta["version"], beatmap_id), "creator": meta["creator"],
                "status": meta["status"], "difficulty_rating": meta["difficulty_rating"], "has_file": has_file,
                "variants": variants, "plays": out_plays, "note": note,
                "url": f"https://osu.ppy.sh/beatmapsets/{sid}#osu/{beatmap_id}" if sid else f"https://osu.ppy.sh/beatmaps/{beatmap_id}"}
