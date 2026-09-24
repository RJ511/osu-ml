"""Recomendador de mapas: dado um jogador e a(s) skill(s) que quer melhorar, sugere mapas.

O jogador escolhe **só as skills** (uma ou várias); estrelas, limiares e esticada são automáticos:
1. **Alcançável** (definição do utilizador): chegar a ≥ 88 % de accuracy (93 % seria melhor). Vem de modelos `reach` treinados nos dumps,
   um por limiar (85/88/90/93/95/97 %); deles sai a hipótese de ≥ 88 % e a *accuracy provável* (mediana da distribuição prevista).
2. **Desafio nas skills pedidas**: a exigência do mapa nesses eixos deve ficar um pouco acima do nível do jogador (pico ~+6 pontos na
   escala 50+20·z), sem que os outros eixos passem muito do nível dele. O nível = P90 dos seus melhores passes (top 200 por pp).
3. **Estilo**: "jogadores parecidos" (filtragem colaborativa sobre os dumps): mapas que jogadores com histórico parecido jogam.
4. **Mapas já jogados voltam a ser recomendados** se a accuracy provável ultrapassar bastante a atual (≥ 3 pontos e ≥ 10 % de pp estimado,
   pela curva pp×accuracy do rosu-pp) — decisão do utilizador. Mapas tentados e nunca passados podem voltar como "tentar de novo".
Limites v0: atributos **nomod** (o playcount dos dumps não diz os mods), sem ritmo de evolução, sem forma do dia; a previsão descreve o
jogador "típico" com este perfil, não a jogada de hoje. O ritmo de evolução (rank_history/monthly_playcounts) fica para depois.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

TAB, NL, CR = chr(9), chr(10), chr(13)


def beatmap_url(beatmap_id: int, set_id: int | None = None) -> str:
    """Link da DIFICULDADE específica (não do set inteiro): `beatmapsets/<set>#osu/<id>` (verificado: `/b/<id>` e `/beatmaps/<id>` redirecionam para aí)."""
    if set_id:
        return f"https://osu.ppy.sh/beatmapsets/{int(set_id)}#osu/{int(beatmap_id)}"
    return f"https://osu.ppy.sh/beatmaps/{int(beatmap_id)}"
THRESHOLDS = (0.85, 0.88, 0.90, 0.93, 0.95, 0.97)
ACC_PP_CURVE = ((0.80, 0.408), (0.85, 0.456), (0.88, 0.492), (0.90, 0.524), (0.93, 0.589), (0.95, 0.652), (0.97, 0.746),
                (0.98, 0.81), (0.99, 0.893), (1.0, 1.0))  # pp(acc)/pp(100 %), mediana em 150 mapas (rosu-pp)
AXES = ("aim", "speed", "stamina", "reading")
AXIS_LABEL = {"aim": "Aim", "speed": "Speed", "stamina": "Stamina", "reading": "Reading"}
INDEX_AXES = ("aim", "speed", "stamina", "reading", "stars")

MIN_REACH_NEW = 0.30  # P(>=88 %) mínima; nos jogadores da API, previsto 0,30-0,40 => observado 0,53-0,66 (ver reach_api_check): "mais provável que não"
MIN_ACC_GAIN = 0.03         # ganho mínimo de accuracy provável para repetir um mapa (3 pontos)
MIN_PP_GAIN_PCT = 10.0      # e de pp estimado
MIN_DELTA_NEW = 1.0         # exigência mínima acima do nível nas skills pedidas (mapas novos)
MIN_DELTA_REPLAY = -10.0    # (repetições) o mapa não pode ser muito mais fácil do que o nível do jogador nas skills pedidas
CHALLENGE_PEAK, CHALLENGE_WIDTH = 6.0, 6.0
OTHER_AXIS_TOLERANCE, OTHER_AXIS_LIMIT = 4.0, 8.0
MAX_PER_SET = 2
CF_NEIGHBORS = 50


def pp_factor(acc):
    import numpy as np

    xs, ys = zip(*ACC_PP_CURVE)
    return np.interp(acc, xs, ys)


def monotone(p):
    import numpy as np

    return np.minimum.accumulate(np.clip(p, 0.0, 1.0), axis=1)


def median_accuracy(p, thresholds=THRESHOLDS):
    """Accuracy em que a probabilidade prevista de a atingir cai para 50 % (interpolação linear entre limiares)."""
    import numpy as np

    p = monotone(np.asarray(p, dtype=float))
    t = np.asarray(thresholds, dtype=float)
    n, k = p.shape
    out = np.empty(n)
    below = p[:, 0] < 0.5
    out[below] = t[0] - 0.15 * (0.5 - p[below, 0]) / 0.5             # menos de metade chega ao 1.º limiar
    above = p[:, -1] >= 0.5
    out[above] = t[-1] + 0.03 * np.minimum(1.0, (p[above, -1] - 0.5) / 0.5)
    mid = ~below & ~above
    if mid.any():
        pm_ = p[mid]
        j = (pm_ >= 0.5).sum(axis=1)                                    # 1.º índice com p < 0,5
        lo, hi = pm_[np.arange(len(pm_)), j - 1], pm_[np.arange(len(pm_)), j]
        frac = (lo - 0.5) / np.maximum(lo - hi, 1e-9)
        out[mid] = t[j - 1] + frac * (t[j] - t[j - 1])
    return np.clip(out, 0.5, 1.0)


class Predictor(Protocol):
    def predict(self, x) -> Any: ...


class LightGbmPredictor:
    """Um modelo `reach` por limiar de accuracy (`reach_acc<NN>_A.txt`)."""

    def __init__(self, models_dir: Path, thresholds=THRESHOLDS) -> None:
        import lightgbm as lgb

        self.thresholds = thresholds
        self.boosters = []
        for t in thresholds:
            f = Path(models_dir) / f"reach_acc{int(round(t * 100))}_A.txt"
            if not f.exists():
                raise FileNotFoundError(f"falta o modelo {f.name} (osuml analyze reach-model --only-a ...)")
            self.boosters.append(lgb.Booster(model_file=str(f)))

    def predict(self, x):
        import numpy as np

        return monotone(np.column_stack([b.predict(x) for b in self.boosters]))


@dataclass
class PlayerData:
    user_id: int
    username: str | None
    profile: Any                       # vetor de perfil (pass_model.PROFILE_FEATS)
    levels: dict[str, float]           # nível por eixo (P90 dos melhores passes, nota 50+20z)
    played: set[int]                   # índices (no catálogo) de mapas com algum score
    best: dict[int, dict[str, Any]]    # índice -> {acc, pp, mods} do melhor passe
    n_passes: int
    notes: list[str] = field(default_factory=list)


class Recommender:
    def __init__(self, store, index_dir: Path, models_dir: Path, *, predictor: Predictor | None = None, index: dict | None = None,
                 cf: tuple | None = None, feedback_file: Path | None = None) -> None:
        self.store, self.index_dir, self.models_dir = store, Path(index_dir), Path(models_dir)
        self.feedback_file = Path(feedback_file) if feedback_file else None  # cópia em texto (TSV) de cada feedback, fácil de ler/analisar
        self._predictor, self._index, self._cf = predictor, index, cf
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, Any, Any]] = {}

    # ------------------------------------------------------------------ carga
    def ready(self) -> tuple[bool, str]:
        need = [self.index_dir / "index.npz", self.index_dir / "meta.json"]
        miss = [p.name for p in need if not p.exists()]
        if self._index is None and miss:
            return False, "falta o índice do recomendador (" + ", ".join(miss) + "): osuml recommend build-index"
        if self._predictor is None and not all((self.models_dir / f"reach_acc{int(round(t * 100))}_A.txt").exists() for t in THRESHOLDS):
            return False, "faltam modelos de accuracy (osuml analyze reach-model --only-a --thresholds ...)"
        return True, ""

    def _load(self) -> None:
        import numpy as np

        with self._lock:
            if self._index is None:
                z = np.load(self.index_dir / "index.npz")
                labels = {}
                lf = self.index_dir / "labels.parquet"
                if lf.exists():
                    import pyarrow.parquet as pq

                    t = pq.read_table(lf).to_pydict()
                    labels = {int(b): {"artist": a, "title": ti, "version": v, "creator": c, "set_id": s}
                              for b, a, ti, v, c, s in zip(t["beatmap_id"], t["artist"], t["title"], t["version"], t["creator"], t["set_id"])}
                self._index = {"ids": z["ids"], "x": z["x"], "axis": z["axis"], "labels": labels}
            if self._cf is None:
                cfm, cfu = self.index_dir / "cf_matrix.npz", self.index_dir / "cf_users.npy"
                if cfm.exists() and cfu.exists():
                    import scipy.sparse as sp

                    self._cf = (sp.load_npz(cfm).tocsr(), np.load(cfu))
                else:
                    self._cf = (None, None)
            if self._predictor is None:
                self._predictor = LightGbmPredictor(self.models_dir)

    # ---------------------------------------------------------------- jogador
    def load_player(self, user_id: int) -> PlayerData:
        import numpy as np
        from sqlalchemy import select

        from ..analysis import pass_model as pm
        from ..storage import models as m

        ids, x, axis = self._index["ids"], self._index["x"], self._index["axis"]
        with self.store.engine.connect() as c:
            name = c.execute(select(m.users.c.username).where(m.users.c.user_id == user_id)).scalar()
            rows = c.execute(select(m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.pp, m.scores.c.mod_acronyms)
                             .where(m.scores.c.user_id == user_id, m.scores.c.beatmap_id.isnot(None))).all()
        if not rows:
            raise ValueError("jogador sem scores na base de dados")
        bid = np.array([r[0] for r in rows], dtype=np.int64)
        pos = np.searchsorted(ids, bid)
        pos[pos >= len(ids)] = 0
        in_cat = ids[pos] == bid
        played = set(pos[in_cat].tolist())
        passed = np.array([bool(r[1]) for r in rows]) & in_cat
        if passed.sum() < pm.MIN_PASSES:
            raise ValueError(f"poucos passes em mapas do catálogo ({int(passed.sum())}); são precisos ≥ {pm.MIN_PASSES}")
        acc = np.array([r[2] if r[2] is not None else 0.0 for r in rows], dtype=np.float32)
        pp = np.array([r[3] if r[3] is not None else np.nan for r in rows], dtype=np.float32)
        mods = [r[4] or "" for r in rows]
        flags = np.array([(1 if ("DT" in md or "NC" in md) else 0) | (2 if "HD" in md else 0) | (4 if "HR" in md else 0) for md in mods], dtype=np.uint8)
        sel = np.nonzero(passed)[0]
        attrs5 = x[pos[sel]][:, [pm.MAP_FEATS.index(a) for a in pm.PROF_ATTRS]]
        res = pm.profile_vector(attrs5, pp[sel], acc[sel], flags[sel], len(played), 1.0)
        if res is None:
            raise ValueError("perfil não calculável")
        profile, _ = res
        top = sel[np.argsort(-np.nan_to_num(pp[sel], nan=-1.0), kind="stable")[:pm.TOP_PASSES]]
        levels = {a: float(np.percentile(axis[pos[top], INDEX_AXES.index(a)], 90)) for a in INDEX_AXES}
        best: dict[int, dict[str, Any]] = {}
        for i in sel:
            j = int(pos[i])
            cur = best.get(j)
            if cur is None:
                best[j] = {"acc": float(acc[i]), "pp": None if np.isnan(pp[i]) else float(pp[i]), "mods": mods[i]}
            else:
                cur["acc"] = max(cur["acc"], float(acc[i]))
                if not np.isnan(pp[i]) and (cur["pp"] is None or pp[i] > cur["pp"]):
                    cur["pp"], cur["mods"] = float(pp[i]), mods[i]
        return PlayerData(user_id, name, profile, levels, played, best, int(passed.sum()))

    # -------------------------------------------------------------- previsões
    def _predict_all(self, pdata: PlayerData, rows=None):
        """Probabilidades P(≥ t) e accuracy provável. `rows` (índices do catálogo) limita a previsão aos candidatos plausíveis:
        com 150 mil mapas × 6 modelos, prever tudo demorava ~23 s; as restantes linhas ficam a 0 (nunca são candidatas)."""
        import numpy as np

        from ..analysis import pass_model as pm

        x = self._index["x"]
        n = len(x)
        rows = np.arange(n) if rows is None else np.asarray(rows)
        xs = x[rows]
        feats = np.hstack([xs, np.repeat(pdata.profile[None, :], len(xs), axis=0), pm.gaps(xs, pdata.profile)]).astype(np.float32)
        part = np.asarray(self._predictor.predict(feats)) if len(rows) else np.zeros((0, len(THRESHOLDS)))
        p = np.zeros((n, part.shape[1] if part.ndim == 2 else len(THRESHOLDS)), dtype=np.float32)
        p[rows] = part
        acc = np.full(n, 0.0, dtype=np.float32)
        if len(rows):
            acc[rows] = median_accuracy(part)
        return p, acc

    def _style(self, pdata: PlayerData):
        """Percentil (0-1) de 'jogadores parecidos jogam este mapa'; zeros se não houver matriz de filtragem colaborativa."""
        import numpy as np
        import scipy.sparse as sp

        n = len(self._index["ids"])
        mat, users = self._cf
        if mat is None or not pdata.played:
            return np.zeros(n, dtype=np.float32)
        v = sp.csr_matrix((np.ones(len(pdata.played), dtype=np.float32), (np.zeros(len(pdata.played), dtype=int), list(pdata.played))), shape=(1, n))
        nnz = np.diff(mat.indptr).astype(np.float32)
        sims = np.asarray((v @ mat.T).todense()).ravel() / (np.sqrt(len(pdata.played)) * np.sqrt(np.maximum(nnz, 1)))
        sims[users == pdata.user_id] = 0.0  # o próprio jogador (se estiver nos dumps) não conta como vizinho
        k = min(CF_NEIGHBORS, len(sims) - 1)
        nb = np.argpartition(-sims, k)[:k]
        w = sp.csr_matrix((sims[nb], (np.zeros(k, dtype=int), nb)), shape=(1, mat.shape[0]))
        scores = np.asarray((w @ mat).todense()).ravel()
        order = np.argsort(np.argsort(scores))
        return (order / max(n - 1, 1)).astype(np.float32)

    # ------------------------------------------------------------ recomendar
    def recommend(self, user_id: int, skills: list[str], n: int = 20) -> dict[str, Any]:
        import numpy as np

        skills = [s for s in dict.fromkeys(skills) if s in AXES]
        if not skills:
            return {"error": "escolhe pelo menos uma skill (aim, speed, stamina, reading)"}
        ok, why = self.ready()
        if not ok:
            return {"error": why}
        try:
            self._load()
            pdata = self.load_player(user_id)
        except ValueError as exc:
            return {"error": str(exc)}
        style = self._style(pdata)
        idx = self._index
        axis = idx["axis"]
        delta = {a: axis[:, INDEX_AXES.index(a)] - pdata.levels[a] for a in AXES}
        sel_delta = np.mean([delta[a] for a in skills], axis=0)
        others = [a for a in AXES if a not in skills]
        other_excess = np.max([np.maximum(delta[a] - OTHER_AXIS_TOLERANCE, 0.0) for a in others], axis=0) if others else np.zeros(len(sel_delta))
        challenge = np.exp(-(((sel_delta - CHALLENGE_PEAK) / CHALLENGE_WIDTH) ** 2))

        n_maps = len(idx["ids"])
        acc_cur = np.full(n_maps, np.nan)
        for j, b in pdata.best.items():
            acc_cur[j] = b["acc"]
        played_mask = np.zeros(n_maps, dtype=bool)
        played_mask[list(pdata.played)] = True
        passed_mask = ~np.isnan(acc_cur)
        # só se prevê para quem pode vir a ser candidato (mesmos filtros de desafio/eixos que abaixo)
        cand = (~passed_mask & (sel_delta >= MIN_DELTA_NEW) & (other_excess <= OTHER_AXIS_LIMIT)) | \
               (passed_mask & (sel_delta >= MIN_DELTA_REPLAY) & (other_excess <= OTHER_AXIS_LIMIT + 4))
        key = (user_id, tuple(sorted(skills)), len(pdata.played) + pdata.n_passes)
        cached = self._cache.get(key)
        if cached is not None:
            p, acc_pred = cached
        else:
            p, acc_pred = self._predict_all(pdata, np.nonzero(cand)[0])
            self._cache = {key: (p, acc_pred)}
        p88 = p[:, THRESHOLDS.index(0.88)]
        p93 = p[:, THRESHOLDS.index(0.93)]
        gain_acc = np.where(passed_mask, acc_pred - acc_cur, 0.0)
        gain_pp = np.where(passed_mask, (pp_factor(acc_pred) / np.maximum(pp_factor(np.nan_to_num(acc_cur, nan=0.85)), 1e-6) - 1) * 100, 0.0)

        new_ok = ~played_mask & (p88 >= MIN_REACH_NEW) & (sel_delta >= MIN_DELTA_NEW) & (other_excess <= OTHER_AXIS_LIMIT)
        retry_ok = played_mask & ~passed_mask & (p88 >= MIN_REACH_NEW) & (sel_delta >= MIN_DELTA_NEW) & (other_excess <= OTHER_AXIS_LIMIT)
        replay_ok = passed_mask & (gain_acc >= MIN_ACC_GAIN) & (gain_pp >= MIN_PP_GAIN_PCT) & (sel_delta >= MIN_DELTA_REPLAY) & (other_excess <= OTHER_AXIS_LIMIT + 4)
        gain_norm = np.clip(gain_pp / 40.0, 0.0, 1.0)
        score = np.full(n_maps, -np.inf)
        base = 0.40 * challenge + 0.35 * p88 + 0.25 * style - 0.02 * other_excess
        score[new_ok | retry_ok] = base[new_ok | retry_ok]
        rep = 0.30 * challenge + 0.40 * gain_norm + 0.15 * p88 + 0.15 * style - 0.02 * other_excess
        score[replay_ok] = rep[replay_ok]

        order = np.argsort(-score)
        items: list[dict[str, Any]] = []
        per_set: dict[Any, int] = {}
        for j in order[: max(n * 6, 200)]:
            if not np.isfinite(score[j]):
                break
            lab = idx["labels"].get(int(idx["ids"][j]), {})
            sid = lab.get("set_id")
            if sid is not None and per_set.get(sid, 0) >= MAX_PER_SET:
                continue
            per_set[sid] = per_set.get(sid, 0) + 1
            kind = "rejogar" if replay_ok[j] else ("tentar_de_novo" if retry_ok[j] else "novo")
            items.append(self._item(int(j), kind, lab, idx, score, p88, p93, acc_pred, acc_cur, gain_acc, gain_pp, sel_delta, delta, style, pdata, skills))
            if len(items) >= n:
                break
        return {"player": {"user_id": user_id, "username": pdata.username, "levels": {a: round(v, 1) for a, v in pdata.levels.items()},
                           "n_passes_used": pdata.n_passes, "n_played_maps": len(pdata.played)},
                "skills": skills, "items": items,
                "counts": {"novo": int(new_ok.sum()), "tentar_de_novo": int(retry_ok.sum()), "rejogar": int(replay_ok.sum())},
                "notes": ["Atributos dos mapas sem mods; previsão do jogador 'típico' com este perfil (não a forma de hoje).",
                          "Nível = P90 dos melhores passes; nota 50 = mapa mediano, +20 por desvio-padrão."]}

    def _item(self, j, kind, lab, idx, score, p88, p93, acc_pred, acc_cur, gain_acc, gain_pp, sel_delta, delta, style, pdata, skills):
        import numpy as np

        bid = int(idx["ids"][j])
        title = f'{lab.get("artist", "")} – {lab.get("title", "")}'.strip(" –") if lab.get("title") else f"mapa #{bid}"
        version = lab.get("version", "")
        d = {a: round(float(delta[a][j]), 1) for a in AXES}
        skill_txt = ", ".join(f"{AXIS_LABEL[a]} {d[a]:+.1f}" for a in skills)
        parts = [f"{skill_txt} vs o teu nível ({', '.join(f'{AXIS_LABEL[a]} {pdata.levels[a]:.0f}' for a in skills)})",
                 f"hipótese de ≥88 %: {p88[j] * 100:.0f} % (≥93 %: {p93[j] * 100:.0f} %); accuracy provável ≈ {acc_pred[j] * 100:.1f} %"]
        if kind == "rejogar":
            cur = pdata.best[j]
            parts.append(f"já jogaste ({acc_cur[j] * 100:.1f} %{'' if cur['pp'] is None else f', {cur['pp']:.0f} pp'}): esperam-se ≈ +{gain_acc[j] * 100:.1f} pontos de accuracy "
                         f"(≈ +{gain_pp[j]:.0f} % de pp nesse mapa)")
        elif kind == "tentar_de_novo":
            parts.append("já tentaste mas ainda não passaste; o teu perfil sugere que é alcançável")
        if style[j] > 0:
            parts.append(f"jogado por jogadores parecidos (top {max(1, round((1 - float(style[j])) * 100))} % dos mapas para o teu perfil)")
        return {"beatmap_id": bid, "artist": lab.get("artist", ""), "title": lab.get("title", "") or f"mapa #{bid}", "version": version,
                "creator": lab.get("creator", ""), "label": f"{title} [{version}]" if version else title,
                "beatmapset_id": lab.get("set_id") or None, "url": beatmap_url(bid, lab.get("set_id")), "kind": kind, "stars": round(float(idx["x"][j, 0]), 2),
                "axis": {a: round(float(idx["axis"][j, INDEX_AXES.index(a)]), 1) for a in AXES}, "delta": d,
                "p88": round(float(p88[j]), 3), "p93": round(float(p93[j]), 3), "acc_pred": round(float(acc_pred[j]), 4),
                "acc_cur": None if np.isnan(acc_cur[j]) else round(float(acc_cur[j]), 4),
                "pp_gain_pct": round(float(gain_pp[j]), 1) if kind == "rejogar" else None,
                "style_pct": round(float(style[j]) * 100, 1), "score": round(float(score[j]), 4), "why": "; ".join(parts)}

    # ------------------------------------------------------------- feedback
    def model_info(self) -> dict[str, Any]:
        """Que modelo está carregado: impressão digital dos ficheiros `reach_acc*_A.txt` + resumo do treino (se o pacote o traz)."""
        import hashlib

        files = sorted(self.models_dir.glob("reach_acc*_A.txt"))
        h = hashlib.sha256()
        for f in files:
            h.update(f.name.encode())
            h.update(f.read_bytes())
        info: dict[str, Any] = {"fingerprint": h.hexdigest()[:12] if files else None, "n_models": len(files)}
        for name in ("training.json", "../manifest.json"):
            f = (self.models_dir / name).resolve()
            if f.exists():
                try:
                    import json

                    data = json.loads(f.read_text(encoding="utf-8"))
                    info["training"] = data.get("training", data) if name != "training.json" else data
                    break
                except ValueError:
                    pass
        return info

    def players(self) -> list[dict[str, Any]]:
        """Jogadores com scores na base de dados (para escolher por nome)."""
        from sqlalchemy import func, select

        from ..storage import models as m

        with self.store.engine.connect() as c:
            n = dict(c.execute(select(m.scores.c.user_id, func.count()).group_by(m.scores.c.user_id)).all())
            rows = c.execute(select(m.users.c.user_id, m.users.c.username)).all()
        return sorted(({"user_id": int(u), "username": name, "n_scores": int(n.get(u, 0))} for u, name in rows if n.get(u, 0)),
                      key=lambda d: d["username"].lower())

    def find_player(self, text: Any) -> tuple[int, str] | None:
        """(user_id, nome) a partir do nome (sem distinguir maiúsculas) ou do id."""
        t = str(text or "").strip()
        if not t:
            return None
        for pl in self.players():
            if pl["username"].lower() == t.lower() or str(pl["user_id"]) == t:
                return pl["user_id"], pl["username"]
        return None

    FEEDBACK_HEADER = ("data_hora_utc", "jogador", "user_id", "beatmap_id", "beatmapset_id", "mapa", "veredicto", "tipo", "skills", "score", "nota", "link")

    def _write_feedback_line(self, when, name, user_id, beatmap_id, set_id, label, verdict, kind, skills, score, note) -> None:
        """Acrescenta uma linha ao ficheiro de texto (TSV com cabeçalho): abre-se em qualquer editor ou folha de cálculo."""
        if self.feedback_file is None:
            return

        def clean(v: Any) -> str:
            return str(v if v is not None else "").replace(TAB, " ").replace(CR, " ").replace(NL, " ").strip()

        self.feedback_file.parent.mkdir(parents=True, exist_ok=True)
        new = not self.feedback_file.exists() or self.feedback_file.stat().st_size == 0
        row = [when.strftime("%Y-%m-%d %H:%M:%S"), name, user_id, beatmap_id, set_id or "", label, verdict, kind, ",".join(skills),
               "" if score is None else f"{score:.4f}", note, beatmap_url(beatmap_id, set_id)]
        with self.feedback_file.open("a", encoding="utf-8", newline="") as f:
            if new:
                f.write(TAB.join(self.FEEDBACK_HEADER) + NL)
            f.write(TAB.join(clean(x) for x in row) + NL)

    def feedback(self, user_id: int, beatmap_id: int, verdict: str, skills: list[str], kind: str = "", score: float | None = None,
                 note: str = "") -> dict[str, Any]:
        from sqlalchemy import select

        from ..storage import models as m
        from ..storage.database import utcnow

        if verdict not in ("serve", "nao_serve"):
            return {"error": "veredicto inválido"}
        when = utcnow()
        with self.store.engine.begin() as c:
            c.execute(m.recommendation_feedback.insert().values(user_id=user_id, beatmap_id=beatmap_id, verdict=verdict, kind=kind,
                                                                 skills=",".join(skills), score=score, created_at=when))
            name = c.execute(select(m.users.c.username).where(m.users.c.user_id == user_id)).scalar()
        label, set_id = "", None
        try:
            self._load()
            lab = self._index["labels"].get(int(beatmap_id), {})
            set_id = lab.get("set_id") or None
            label = f'{lab.get("artist", "")} - {lab.get("title", "")} [{lab.get("version", "")}]' if lab.get("title") else ""
        except Exception:  # sem índice (testes) ou sem etiquetas: o feedback guarda-se na mesma
            pass
        self._write_feedback_line(when, name or user_id, user_id, beatmap_id, set_id, label, verdict, kind, skills, score, note)
        return {"ok": True, "file": str(self.feedback_file) if self.feedback_file else None}
