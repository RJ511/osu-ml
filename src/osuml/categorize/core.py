"""Categorização de todos os mapas e jogadores (`osuml categorize`).

- **Mapa**: cada par (beatmap_id, mods jogados sem `CL`) é categorizado UMA vez (Aim/Speed/Stamina/
  Reading em escala aberta 50+20z + nota, `SkillScorer` contra a pool de referência) e guardado em `map_categories`;
  mapas repetidos — entre jogadores ou entre execuções — são reaproveitados, nunca recalculados.
  Sem `.osu` (ex.: mapas fora do dump) → `no_file`: fica marcado, não se inventa nada.
- **Jogador**: rating por eixo = percentil 90 do eixo nas plays PASSADAS com accuracy >= 90%
  (capacidade máxima demonstrada), mediana como "típico", `n_evidence` para ver a confiança. É uma
  heurística v1, sem ML e sem replays (só resultado agregado por play) — hipótese, não verdade.
  Nome, pp e rank vêm do objeto de utilizador da API guardado na BD.
- O controlador corre em thread, com pausa/retoma/cancelar e um "ritmo" opcional para se poder ver.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..beatmaps.difficulty import calc_difficulty
from ..beatmaps.hitfeatures import compute_from_parsed
from ..beatmaps.parser import parse_osu
from ..beatmaps.skills import SkillScorer, grade
from ..dataset.derived import mods_effective
from ..storage import models as m
from ..storage.database import Store, utcnow

SCHEME = "ref_v3/cat_v2"  # cat_v2: Reading oficial do lazer (com mods) + escala aberta 50+20z
AXES = ("aim", "speed", "stamina", "reading")
EVIDENCE_MIN_ACCURACY = 0.90
RATING_QUANTILE = 0.90
CONFIDENT_EVIDENCE = 10  # abaixo disto o rating aparece marcado como pouco fiável


def quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 3) if den else None


class MapCategorizer:
    def __init__(self, store: Store, scorer: SkillScorer, scheme: str = SCHEME) -> None:
        self.store, self.scorer, self.scheme = store, scorer, scheme
        self._parsed: dict[int, tuple[dict, dict]] = {}  # beatmap_id -> (summary, hitfeatures), pequena cache

    def _saved(self, beatmap_id: int, mods: str) -> dict | None:
        with self.store.engine.connect() as c:
            row = c.execute(select(m.map_categories).where(
                m.map_categories.c.beatmap_id == beatmap_id, m.map_categories.c.mods == mods,
                m.map_categories.c.scheme == self.scheme)).mappings().first()
            if row is None:
                return None
            if row["status"] == "no_file" and c.execute(select(m.beatmap_files.c.beatmap_id).where(
                    m.beatmap_files.c.beatmap_id == beatmap_id)).first():
                return None  # o ficheiro apareceu entretanto: recalcula
            return dict(row)

    def _save(self, beatmap_id: int, mods: str, status: str, raw=None, scores=None, error=None) -> dict:
        values = dict(status=status, scheme=self.scheme, raw=raw, scores=scores, error=error, computed_at=utcnow())
        with self.store.engine.begin() as c:
            exists = c.execute(select(m.map_categories.c.beatmap_id).where(
                m.map_categories.c.beatmap_id == beatmap_id, m.map_categories.c.mods == mods)).first()
            if exists:
                c.execute(m.map_categories.update().where(
                    m.map_categories.c.beatmap_id == beatmap_id, m.map_categories.c.mods == mods).values(**values))
            else:
                c.execute(m.map_categories.insert().values(beatmap_id=beatmap_id, mods=mods, **values))
        return {"beatmap_id": beatmap_id, "mods": mods, **values}

    def get(self, beatmap_id: int, mods: str) -> tuple[dict, bool]:
        """(linha categorizada, veio_da_cache). Nunca recalcula um par já categorizado."""
        saved = self._saved(beatmap_id, mods)
        if saved is not None:
            return saved, True
        with self.store.engine.connect() as c:
            f = c.execute(select(m.beatmap_files.c.rel_path).where(m.beatmap_files.c.beatmap_id == beatmap_id)).first()
        if f is None:
            return self._save(beatmap_id, mods, "no_file", error="sem ficheiro .osu (fora do dump)"), False
        try:
            text = (self.store.raw.raw_dir / f[0]).read_bytes().decode("utf-8", errors="replace")
            if beatmap_id not in self._parsed:
                pb = parse_osu(text)
                if pb.mode != 0:
                    raise ValueError(f"modo {pb.mode} (só osu!standard)")
                if len(self._parsed) > 16:
                    self._parsed.clear()
                self._parsed[beatmap_id] = (pb.summary(), compute_from_parsed(pb))
            summary, hit = self._parsed[beatmap_id]
            diff = calc_difficulty(text, mods)
            raw = {k: diff[k] for k in ("stars", "aim", "speed", "reading")} | hit | {
                k: summary[k] for k in ("ar", "cs", "od", "hp", "n_objects")}
            return self._save(beatmap_id, mods, "ok", raw=raw, scores=self.scorer.score(raw)), False
        except Exception as exc:  # um mapa estranho não pára a corrida
            return self._save(beatmap_id, mods, "error", error=f"{type(exc).__name__}: {exc}"[:250]), False


def rate_player(plays: list[dict[str, Any]]) -> dict[str, Any]:
    """`plays`: {"passed", "accuracy", "scores": dict de notas do mapa | None}."""
    evidence = [p for p in plays if p["scores"] and p["passed"] and (p["accuracy"] or 0) >= EVIDENCE_MIN_ACCURACY]
    ratings: dict[str, Any] = {}
    for axis in (*AXES, "stars"):
        vals = [p["scores"][f"{axis}_score"] for p in evidence if p["scores"].get(f"{axis}_score") is not None]
        if vals:
            r = round(quantile(vals, RATING_QUANTILE), 2)
            ratings.update({f"{axis}_rating": r, f"{axis}_typical": round(quantile(vals, 0.5), 2),
                            f"{axis}_grade": grade(r)})
    ratings["confident"] = len(evidence) >= CONFIDENT_EVIDENCE
    return {"ratings": ratings, "n_evidence": len(evidence)}


class CategorizeController:
    def __init__(self, store: Store, categorizer: MapCategorizer, *, exclude: set[int] | None = None,
                 only: int | None = None) -> None:
        self.store, self.cat, self.exclude, self.only = store, categorizer, exclude or set(), only
        self.status = "idle"  # idle | running | paused | cancelling | done
        self.message = ""
        self.delay_s = 0.0
        self._gate = threading.Event()
        self._gate.set()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=120)
        self.seq = 0
        self.counts = {"visits": 0, "new": 0, "cached": 0, "no_file": 0, "error": 0}
        self.players: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.pairs_total = 0
        self.started_at: float | None = None
        self._labels: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------ carga
    def _load(self) -> None:
        with self.store.engine.connect() as c:
            users = {r["user_id"]: r["raw"] for r in c.execute(select(m.users.c.user_id, m.users.c.raw)).mappings()}
            rows = c.execute(select(m.scores.c.user_id, m.scores.c.beatmap_id, m.scores.c.mod_acronyms,
                                    m.scores.c.pp).where(m.scores.c.beatmap_id.isnot(None))).all()
            self._labels = {r["beatmap_id"]: {"artist": r["artist"], "title": r["title"], "version": r["version"]}
                            for r in c.execute(select(m.beatmaps.c.beatmap_id, m.beatmaps.c.version, m.beatmapsets.c.artist,
                                                      m.beatmapsets.c.title).select_from(m.beatmaps.outerjoin(
                                m.beatmapsets, m.beatmapsets.c.beatmapset_id == m.beatmaps.c.beatmapset_id))).mappings()}
        per_user: dict[int, dict[tuple[int, str], float]] = {}
        for uid, bid, mods, pp in rows:
            if uid in self.exclude or (self.only is not None and uid != self.only):
                continue
            key = (int(bid), mods_effective(mods))
            best = per_user.setdefault(uid, {})
            best[key] = max(best.get(key, -1.0), pp if pp is not None else -1.0)
        players = []
        for uid, keys in per_user.items():
            raw = users.get(uid)
            raw = json.loads(raw) if isinstance(raw, str) else (raw or {})
            st = raw.get("statistics") or {}
            players.append({"user_id": uid, "username": raw.get("username"), "pp": st.get("pp"),
                            "global_rank": st.get("global_rank"), "n_maps": len(keys), "maps_done": 0,
                            "status": "queued", "profile": None,
                            "_keys": [k for k, _ in sorted(keys.items(), key=lambda kv: -kv[1])]})
        players.sort(key=lambda p: -(p["pp"] or 0))
        self.players = players
        self.pairs_total = len({k for p in players for k in p["_keys"]})

    def _label(self, bid: int) -> dict[str, Any]:
        return self._labels.get(bid, {})

    # --------------------------------------------------------- controlo
    def start(self, delay_ms: float = 0) -> str | None:
        if self._thread is not None and self._thread.is_alive():
            return "já está em curso"
        self.delay_s = max(0.0, float(delay_ms)) / 1000
        self._cancel.clear()
        self._gate.set()
        self.counts = {k: 0 for k in self.counts}
        self.events.clear()
        self.message = ""
        self._load()
        self.status = "running"
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="categorize", daemon=True)
        self._thread.start()
        return None

    def recategorize_player(self, user_id: int) -> dict[str, Any] | None:
        """Recalcula o perfil de UM jogador, de forma síncrona (mapas já categorizados vêm da cache; os novos
        são calculados). Não mexe no estado da corrida em direto. `None` se o jogador não tem scores."""
        if self.status in ("running", "paused", "cancelling"):
            raise RuntimeError("categorização em curso")
        one = CategorizeController(self.store, self.cat, only=user_id)
        one._load()
        if not one.players:
            return None
        one._run_player(0, one.players[0])
        return one.players[0]["profile"]

    def pause(self) -> None:
        if self.status == "running":
            self.status = "paused"
            self._gate.clear()

    def resume(self) -> None:
        if self.status == "paused":
            self.status = "running"
            self._gate.set()

    def cancel(self) -> None:
        if self.status in ("running", "paused"):
            self.status = "cancelling"
            self._cancel.set()
            self._gate.set()

    def set_delay(self, delay_ms: float) -> None:
        self.delay_s = max(0.0, float(delay_ms)) / 1000

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ----------------------------------------------------------- worker
    def _run(self) -> None:
        try:
            for idx, p in enumerate(self.players):
                if self._cancel.is_set():
                    break
                self._run_player(idx, p)
            if not self._cancel.is_set():
                from .export import export_categories

                export_categories(self.store, self.store.raw.raw_dir.parent / "processed" / "categories" / "v2", SCHEME)
        except Exception as exc:
            self.message = f"{type(exc).__name__}: {exc}"
        finally:
            self.current = None
            self.status = "done" if not self._cancel.is_set() and not self.message else "idle"

    def _run_player(self, idx: int, p: dict[str, Any]) -> None:
        p["status"] = "running"
        with self.store.engine.connect() as c:
            plays_raw = c.execute(select(m.scores.c.beatmap_id, m.scores.c.mod_acronyms, m.scores.c.passed,
                                         m.scores.c.accuracy).where(m.scores.c.user_id == p["user_id"])).all()
        cats: dict[tuple[int, str], dict | None] = {}
        for key in p["_keys"]:
            self._gate.wait()
            if self._cancel.is_set():
                p["status"] = "queued"
                return
            row, cached = self.cat.get(*key)
            cats[key] = row["scores"] if row["status"] == "ok" else None
            self.counts["visits"] += 1
            self.counts["cached" if cached else "new"] += 1
            if row["status"] != "ok":
                self.counts[row["status"]] += 1
            p["maps_done"] += 1
            self.seq += 1
            lab = self._label(key[0])
            ev = {"seq": self.seq, "t": time.time(), "user_id": p["user_id"], "username": p["username"], "pp": p["pp"],
                  "global_rank": p["global_rank"], "player_idx": idx + 1, "n_players": len(self.players),
                  "map_idx": p["maps_done"], "n_maps": p["n_maps"], "beatmap_id": key[0], "mods": key[1],
                  "artist": lab.get("artist"), "title": lab.get("title"), "version": lab.get("version"),
                  "status": row["status"], "cached": cached, "error": row.get("error"),
                  "raw": row.get("raw"), "scores": row.get("scores")}
            self.events.append(ev)
            self.current = ev
            if self.delay_s and not cached:
                time.sleep(self.delay_s)
        plays = [{"passed": bool(passed), "accuracy": acc, "scores": cats.get((int(bid), mods_effective(mods)))}
                 for bid, mods, passed, acc in plays_raw]
        prof = rate_player(plays)
        n_missing = sum(1 for pl in plays if pl["scores"] is None)
        p["profile"] = {**prof, "n_scores": len(plays), "n_missing": n_missing}
        with self.store.engine.begin() as c:
            values = dict(username=p["username"], pp=p["pp"], global_rank=p["global_rank"], scheme=SCHEME,
                          n_scores=len(plays), n_evidence=prof["n_evidence"], n_missing=n_missing,
                          ratings=prof["ratings"], computed_at=utcnow())
            if c.execute(select(m.player_profiles.c.user_id).where(m.player_profiles.c.user_id == p["user_id"])).first():
                c.execute(m.player_profiles.update().where(m.player_profiles.c.user_id == p["user_id"]).values(**values))
            else:
                c.execute(m.player_profiles.insert().values(user_id=p["user_id"], **values))
        p["status"] = "done"

    # ----------------------------------------------------------- estado
    def _correlations(self) -> dict[str, Any]:
        done = [p for p in self.players if p["profile"] and p["pp"] and p["profile"]["ratings"].get("confident")]
        out: dict[str, Any] = {"n": len(done)}
        for axis in (*AXES, "stars"):
            pairs = [(p["pp"], p["profile"]["ratings"][f"{axis}_rating"]) for p in done
                     if f"{axis}_rating" in p["profile"]["ratings"]]
            out[axis] = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= 10 else None
        return out

    def state(self) -> dict[str, Any]:
        elapsed = (time.time() - self.started_at) if self.started_at else 0
        players = [{k: v for k, v in p.items() if k != "_keys"} for p in self.players]
        with self.store.engine.connect() as c:
            cats = dict(c.execute(select(m.map_categories.c.status, func.count())
                                  .where(m.map_categories.c.scheme == SCHEME)
                                  .group_by(m.map_categories.c.status)).all())
        return {"status": self.status, "message": self.message, "scheme": SCHEME, "delay_ms": round(self.delay_s * 1000),
                "counts": self.counts, "pairs_total": self.pairs_total, "pairs_saved": cats,
                "players_total": len(players), "players_done": sum(1 for p in players if p["status"] == "done"),
                "players": players, "current": self.current, "events": list(self.events)[-60:][::-1],
                "rate_per_s": round(self.counts["visits"] / elapsed, 1) if elapsed > 1 else None,
                "elapsed_s": round(elapsed), "correlations": self._correlations(), "now": time.time(),
                "evidence_rule": f"passadas com accuracy ≥ {int(EVIDENCE_MIN_ACCURACY * 100)}% · rating = P{int(RATING_QUANTILE * 100)}"}
