"""Recomendador de mapas: dado um jogador e a(s) skill(s) que quer melhorar, sugere mapas.

O jogador escolhe **só as skills** (uma ou várias); estrelas, limiares e esticada são automáticos:
1. **Alcançável** (definição do utilizador, à Tillerino): duas quantidades separadas — **P(passar)** (não morrer no mapa; mínimo 80 %) e a **accuracy esperada SE PASSAR**
   (mediana; mínimo 88 %, ideal ~93 %). Vêm de `pass_model_A` (calibrado com jogadores da API) e de `acc_pass_A` (regressão nos passes dos dumps). Se houver menos de
   10 sugestões com 80 %, completa-se com mapas de P(passar) ≥ 70 % (a accuracy esperada continua ≥ 88 %). Os modelos `reach` ficam só para análise.
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

from ..analysis.profile_form import ages_in_days, best_pass_per_map, build_profile

TAB, NL, CR = chr(9), chr(10), chr(13)


def beatmap_url(beatmap_id: int, set_id: int | None = None) -> str:
    """Link da DIFICULDADE específica (não do set inteiro): `beatmapsets/<set>#osu/<id>` (verificado: `/b/<id>` e `/beatmaps/<id>` redirecionam para aí)."""
    if set_id:
        return f"https://osu.ppy.sh/beatmapsets/{int(set_id)}#osu/{int(beatmap_id)}"
    return f"https://osu.ppy.sh/beatmaps/{int(beatmap_id)}"
def parse_map_ref(text: Any) -> tuple[int | None, int | None]:
    """(beatmap_id, beatmapset_id) a partir de um link do osu! ou de um número (número solto = id da dificuldade). (None, None) se não reconhecer."""
    import re

    t = str(text or "").strip()
    m = re.search(r"beatmapsets/(\d+)(?:#\w+/(\d+))?", t)
    if m:
        return (int(m.group(2)) if m.group(2) else None), int(m.group(1))
    m = re.search(r"/(?:beatmaps|b)/(\d+)", t)
    if m:
        return int(m.group(1)), None
    return (int(t), None) if t.isdigit() else (None, None)


THRESHOLDS = (0.85, 0.88, 0.90, 0.93, 0.95, 0.97)
ACC_PP_CURVE = ((0.80, 0.408), (0.85, 0.456), (0.88, 0.492), (0.90, 0.524), (0.93, 0.589), (0.95, 0.652), (0.97, 0.746),
                (0.98, 0.81), (0.99, 0.893), (1.0, 1.0))  # pp(acc)/pp(100 %), mediana em 150 mapas (rosu-pp)
AXES = ("aim", "speed", "stamina", "reading")
AXIS_LABEL = {"aim": "Aim", "speed": "Speed", "stamina": "Stamina", "reading": "Reading"}
INDEX_AXES = ("aim", "speed", "stamina", "reading", "stars")

MIN_PASS_PROB = 0.80  # P(passar) mínima ("não morrer no mapa"; o utilizador: 80, talvez 90)
MIN_PASS_PROB_FALLBACK = 0.70  # se houver menos de MIN_SAFE_ITEMS sugestões com 0,80
MIN_EXPECTED_ACC = 0.88  # accuracy esperada SE PASSAR >= 88 % (abaixo disso não se aprende); também para repetir mapas
MIN_SAFE_ITEMS = 10
TARGET_ACC = 0.93  # zona de aprendizagem: ~93 %
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

    def __init__(self, models_dir: Path, thresholds=THRESHOLDS, calibration: bool = True) -> None:
        import json

        import lightgbm as lgb

        self.thresholds = thresholds
        self.calibration = None  # {acc85: {a, b}, ...}: corrige a subestimação nos jogadores da API (ver analysis/reach_calibration.py)
        cf = Path(models_dir) / "calibration.json"
        if calibration and cf.exists():
            self.calibration = json.loads(cf.read_text(encoding="utf-8")).get("params")
        self.boosters = []
        for t in thresholds:
            f = Path(models_dir) / f"reach_acc{int(round(t * 100))}_A.txt"
            if not f.exists():
                raise FileNotFoundError(f"falta o modelo {f.name} (osuml analyze reach-model --only-a ...)")
            self.boosters.append(lgb.Booster(model_file=str(f)))

    def predict_both(self, x):
        """(probabilidades brutas, calibradas), ambas monótonas em t. Sem `calibration.json` as duas são iguais."""
        import numpy as np

        from ..analysis.reach_calibration import apply_calibration

        raw = np.column_stack([b.predict(x) for b in self.boosters])
        cal = apply_calibration(raw, self.calibration, self.thresholds) if self.calibration else raw
        return monotone(raw), monotone(cal)

    def predict(self, x):
        return self.predict_both(x)[1]


class PassAccPredictor:
    """P(passar) (`pass_model_A.txt`) e accuracy esperada SE PASSAR (`acc_pass_A.txt`, mediana).

    Se existir `calibration_pass_acc.json` (ver analysis/pass_calibration.py) aplica-se: logit(p_cal) = a + b·logit(p) à probabilidade e um deslocamento à
    accuracy, ambos medidos em jogadores da API (o modelo bruto subestima: previsto 0,49 -> observado 0,83). `calibration=False` dá os valores brutos."""

    def __init__(self, models_dir: Path, calibration: bool = True) -> None:
        import json

        import lightgbm as lgb

        models_dir = Path(models_dir)
        self.pass_model = lgb.Booster(model_file=str(models_dir / "pass_model_A.txt"))
        self.acc_model = lgb.Booster(model_file=str(models_dir / "acc_pass_A.txt"))
        self.pass_cal, self.acc_shift = None, 0.0
        cf = models_dir / "calibration_pass_acc.json"
        if calibration and cf.exists():
            d = json.loads(cf.read_text(encoding="utf-8"))
            self.pass_cal, self.acc_shift = d.get("pass"), float(d.get("acc_shift") or 0.0)

    def predict_both(self, x) -> dict[str, Any]:
        import numpy as np

        from ..analysis.reach_calibration import logit, sigmoid

        p_raw = np.asarray(self.pass_model.predict(x), dtype=np.float64)
        p = sigmoid(self.pass_cal["a"] + self.pass_cal["b"] * logit(p_raw)) if self.pass_cal else p_raw
        a_raw = np.clip(np.asarray(self.acc_model.predict(x), dtype=np.float64), 0.0, 1.0)
        return {"p_pass": p, "p_pass_raw": p_raw, "acc": np.clip(a_raw + self.acc_shift, 0.0, 1.0), "acc_raw": a_raw}


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
                 cf: tuple | None = None, feedback_file: Path | None = None, profile_mode: str = "form", log_predictions: bool = False) -> None:
        self.log_predictions = log_predictions  # grava cada sugestão em `prediction_log` (recommend/log.py)
        self.profile_mode = profile_mode  # "form" (recência + esforço, ver analysis/profile_form.py) | "recency" | "base" (perfil de treino, sem pesos)
        self.store, self.index_dir, self.models_dir = store, Path(index_dir), Path(models_dir)
        self.feedback_file = Path(feedback_file) if feedback_file else None  # cópia em texto (TSV) de cada feedback, fácil de ler/analisar
        self._predictor, self._index, self._cf = predictor, index, cf
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, Any, Any]] = {}
        self._adjust: dict[int, dict[str, Any]] | None = None  # correção por jogador (recommend/adjust.py), lida de models/player_adjust.json

    # ------------------------------------------------------------------ carga
    def ready(self) -> tuple[bool, str]:
        need = [self.index_dir / "index.npz", self.index_dir / "meta.json"]
        miss = [p.name for p in need if not p.exists()]
        if self._index is None and miss:
            return False, "falta o índice do recomendador (" + ", ".join(miss) + "): osuml recommend build-index"
        if self._predictor is None and not all((self.models_dir / f).exists() for f in ("pass_model_A.txt", "acc_pass_A.txt")):
            return False, "faltam modelos em models/ (pass_model_A.txt e acc_pass_A.txt): osuml analyze pass-model / acc-model"
        return True, ""

    def _load_index_unlocked(self) -> None:
        import numpy as np

        if self._index is not None:
            return
        z = np.load(self.index_dir / "index.npz")
        labels = {}
        lf = self.index_dir / "labels.parquet"
        if lf.exists():
            import pyarrow.parquet as pq

            t = pq.read_table(lf).to_pydict()
            labels = {int(b): {"artist": a, "title": ti, "version": v, "creator": c, "set_id": s}
                      for b, a, ti, v, c, s in zip(t["beatmap_id"], t["artist"], t["title"], t["version"], t["creator"], t["set_id"])}
        set_ids = np.array([int(labels.get(int(b), {}).get("set_id") or 0) for b in z["ids"]], dtype=np.int64) if labels else np.zeros(len(z["ids"]), dtype=np.int64)
        self._index = {"ids": z["ids"], "x": z["x"], "axis": z["axis"], "labels": labels, "set_ids": set_ids}

    def catalog(self) -> dict[str, Any] | None:
        """Só o índice de mapas (ids, atributos, notas por eixo, nomes), sem carregar modelos nem a matriz de jogadores parecidos: é o que o Explorar usa para
        pesquisar TODOS os mapas do catálogo, e não só os que os jogadores acompanhados jogaram. None se não houver índice."""
        if self._index is None and not (self.index_dir / "index.npz").exists():
            return None
        with self._lock:
            self._load_index_unlocked()
        self._set_ids()
        return self._index

    def _load(self) -> None:
        import numpy as np

        with self._lock:
            self._load_index_unlocked()
            if self._cf is None:
                cfm, cfu = self.index_dir / "cf_matrix.npz", self.index_dir / "cf_users.npy"
                if cfm.exists() and cfu.exists():
                    import scipy.sparse as sp

                    self._cf = (sp.load_npz(cfm).tocsr(), np.load(cfu))
                else:
                    self._cf = (None, None)
            if self._predictor is None:
                self._predictor = PassAccPredictor(self.models_dir)
            if self._adjust is None:
                self.reload_adjustments()

    def reload_adjustments(self) -> int:
        """Relê `player_adjust.json` (o `poll`, que é outro processo, atualiza-o depois de cada avaliação)."""
        from .adjust import ADJUST_FILE, load_adjustments

        f = self.models_dir / ADJUST_FILE
        self._adjust_mtime = f.stat().st_mtime if f.exists() else None
        self._adjust = load_adjustments(self.models_dir)
        return len(self._adjust)

    def _adjustment(self, user_id: int) -> dict[str, Any] | None:
        """Correção do jogador; relê o ficheiro se mudou desde a última leitura."""
        from .adjust import ADJUST_FILE

        f = self.models_dir / ADJUST_FILE
        mt = f.stat().st_mtime if f.exists() else None
        if self._adjust is None or mt != getattr(self, "_adjust_mtime", None):
            self.reload_adjustments()
        return (self._adjust or {}).get(user_id)

    # ---------------------------------------------------------------- jogador
    def load_player(self, user_id: int, as_of=None) -> PlayerData:
        """`as_of` (datetime): usa só as jogadas anteriores a esse instante (avaliação-sombra: o que o modelo diria antes de o jogador jogar)."""
        import numpy as np
        from sqlalchemy import select

        from ..analysis import pass_model as pm
        from ..storage import models as m

        ids, x, axis = self._index["ids"], self._index["x"], self._index["axis"]
        with self.store.engine.connect() as c:
            name = c.execute(select(m.users.c.username).where(m.users.c.user_id == user_id)).scalar()
            rows = c.execute(select(m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.pp, m.scores.c.mod_acronyms, m.scores.c.ended_at)
                             .where(m.scores.c.user_id == user_id, m.scores.c.beatmap_id.isnot(None))).all()
        if as_of is not None:
            rows = [r for r in rows if r[5] is None or r[5] < as_of]
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
        # perfil de FORMA ATUAL: 1 passe por mapa (o de maior pp), peso por recência e por esforço (analysis/profile_form.py)
        ended = np.array([np.datetime64(r[5]) if r[5] is not None else np.datetime64("NaT") for r in rows])
        keep = best_pass_per_map(pos, pp, passed)
        built = build_profile(x[pos[keep]][:, [pm.MAP_FEATS.index(a) for a in pm.PROF_ATTRS]], axis[pos[keep]], pp[keep], acc[keep], flags[keep],
                              ages_in_days(ended, keep), len(played), 1.0, mode=self.profile_mode)
        if built is None:
            raise ValueError("perfil não calculável")
        profile, _, lv, info = built
        levels = dict(zip(INDEX_AXES, (float(v) for v in lv)))
        notes = []
        if info.get("mode") == "form":
            notes.append(f"perfil de forma atual: gama de pp recente {info['pp_floor']}–{info['pp_ceiling']} (meio {info['pp_mid']}); passes antigos e fáceis pesam menos")
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
        return PlayerData(user_id, name, profile, levels, played, best, int(passed.sum()), notes)

    # -------------------------------------------------------------- previsões
    def _predict_all(self, pdata: PlayerData, rows=None):
        """{p_pass, p_pass_raw, acc, acc_raw}: arrays (n_mapas,). `rows` (índices do catálogo) limita a previsão aos candidatos plausíveis
        (com 150 mil mapas prever tudo demora); as restantes linhas ficam a 0 (nunca são candidatas)."""
        import numpy as np

        from ..analysis import pass_model as pm

        x = self._index["x"]
        n = len(x)
        rows = np.arange(n) if rows is None else np.asarray(rows)
        xs = x[rows]
        feats = np.hstack([xs, np.repeat(pdata.profile[None, :], len(xs), axis=0), pm.gaps(xs, pdata.profile)]).astype(np.float32)
        keys = ("p_pass", "p_pass_raw", "acc", "acc_raw")
        out = {k: np.zeros(n, dtype=np.float32) for k in keys}
        if len(rows):
            part = self._predictor.predict_both(feats)
            for k in keys:
                out[k][rows] = np.asarray(part[k], dtype=np.float32)
        return out

    def predict_pairs(self, user_id: int, as_of, beatmap_ids) -> dict[int, dict[str, float]]:
        """Previsão para mapas concretos com o perfil do jogador só até `as_of` (avaliação-sombra). Mapas fora do catálogo ficam de fora; ValueError se faltarem passes."""
        import numpy as np

        from ..analysis import pass_model as pm

        self._load()
        pdata = self.load_player(user_id, as_of=as_of)
        ids, x, axis = self._index["ids"], self._index["x"], self._index["axis"]
        b = np.array(sorted({int(v) for v in beatmap_ids}), dtype=np.int64)
        pos = np.searchsorted(ids, b)
        pos[pos >= len(ids)] = 0
        ok = ids[pos] == b
        b, pos = b[ok], pos[ok]
        if not len(b):
            return {}
        xs = x[pos]
        feats = np.hstack([xs, np.repeat(pdata.profile[None, :], len(xs), axis=0), pm.gaps(xs, pdata.profile)]).astype(np.float32)
        pr = self._predictor.predict_both(feats)
        lv = np.array([pdata.levels[a] for a in AXES])
        chal = np.max(axis[pos][:, :4] - lv[None, :], axis=1)
        return {int(bb): {"p_pass": float(pr["p_pass"][i]), "acc_pass": float(pr["acc"][i]), "p_pass_raw": float(pr["p_pass_raw"][i]),
                          "acc_pass_raw": float(pr["acc_raw"][i]), "challenge": float(chal[i])} for i, bb in enumerate(b)}

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
        pr = self._cache.get(key)
        if pr is None:
            pr = self._predict_all(pdata, np.nonzero(cand)[0])
            self._cache = {key: pr}
        p_raw, acc_raw = pr["p_pass_raw"], pr["acc_raw"]
        p_model, acc_model = pr["p_pass"], pr["acc"]  # modelo + calibração global (é isto que fica no registo: o ajuste por jogador estima-se a partir dos valores brutos)
        from .adjust import apply_adjustment

        adj = self._adjustment(user_id)
        p_pass, acc = apply_adjustment(p_model, acc_model, adj)  # correção por jogador (encolhida para 0 com poucos dados)
        blocked = self._blocked_mask(user_id)
        gain_acc = np.where(passed_mask, acc - acc_cur, 0.0)
        gain_pp = np.where(passed_mask, (pp_factor(acc) / np.maximum(pp_factor(np.nan_to_num(acc_cur, nan=0.85)), 1e-6) - 1) * 100, 0.0)
        gain_norm = np.clip(gain_pp / 40.0, 0.0, 1.0)
        learn = np.exp(-(((acc - TARGET_ACC) / 0.05) ** 2))  # zona de aprendizagem: accuracy esperada ~93 %
        per_set: dict[Any, int] = {}
        taken: set[int] = set()

        def eligible(min_p):
            ok = (p_pass >= min_p) & (acc >= MIN_EXPECTED_ACC) & ~blocked
            new_ok = ~played_mask & ok & (sel_delta >= MIN_DELTA_NEW) & (other_excess <= OTHER_AXIS_LIMIT)
            retry_ok = played_mask & ~passed_mask & ok & (sel_delta >= MIN_DELTA_NEW) & (other_excess <= OTHER_AXIS_LIMIT)
            replay_ok = (passed_mask & ok & (gain_acc >= MIN_ACC_GAIN) & (gain_pp >= MIN_PP_GAIN_PCT) & (sel_delta >= MIN_DELTA_REPLAY)
                         & (other_excess <= OTHER_AXIS_LIMIT + 4))
            score = np.full(n_maps, -np.inf)
            base = 0.35 * challenge + 0.25 * learn + 0.20 * p_pass + 0.20 * style - 0.02 * other_excess
            score[new_ok | retry_ok] = base[new_ok | retry_ok]
            rep = 0.30 * challenge + 0.35 * gain_norm + 0.15 * p_pass + 0.20 * style - 0.02 * other_excess
            score[replay_ok] = rep[replay_ok]
            return score, new_ok, retry_ok, replay_ok

        def pick(min_p, limit, tier):
            score, new_ok, retry_ok, replay_ok = eligible(min_p)
            out: list[dict[str, Any]] = []
            for j in np.argsort(-score)[: max(n * 6, 200)]:
                if not np.isfinite(score[j]) or len(out) >= limit:
                    break
                if int(j) in taken:
                    continue
                lab = idx["labels"].get(int(idx["ids"][j]), {})
                sid = lab.get("set_id")
                if sid is not None and per_set.get(sid, 0) >= MAX_PER_SET:
                    continue
                per_set[sid] = per_set.get(sid, 0) + 1
                kind = "rejogar" if replay_ok[j] else ("tentar_de_novo" if retry_ok[j] else "novo")
                it = self._item(int(j), kind, lab, idx, score, p_pass, acc, p_raw, acc_raw, acc_cur, gain_acc, gain_pp, sel_delta, delta, style, pdata, skills)
                it["tier"] = tier
                it["p_pass_model"], it["acc_pass_model"] = round(float(p_model[j]), 3), round(float(acc_model[j]), 4)
                if tier == "arriscado":
                    it["why"] += f"; P(passar) abaixo de {MIN_PASS_PROB * 100:.0f} %: só aparece porque havia menos de {MIN_SAFE_ITEMS} sugestões com {MIN_PASS_PROB * 100:.0f} %"
                out.append(it)
                taken.add(int(j))
            return out

        items = pick(MIN_PASS_PROB, n, "seguro")  # 1.ª camada: P(passar) >= 80 % e accuracy esperada ao passar >= 88 %
        n_safe = len(items)
        if n_safe < MIN_SAFE_ITEMS:  # poucas: completa com P(passar) >= 70 % (a accuracy esperada continua >= 88 %)
            items += pick(MIN_PASS_PROB_FALLBACK, n - n_safe, "arriscado")
        _, new_ok, retry_ok, replay_ok = eligible(MIN_PASS_PROB)
        if self.log_predictions and items:
            try:
                from .log import log_recommendations

                log_recommendations(self.store, user_id, items, skills, self.model_info().get("fingerprint"))
            except Exception:  # o registo nunca pode impedir uma recomendação
                pass
        notes_adj = ([f"correção por jogador (medida nas tuas jogadas anteriores): accuracy {-float(adj.get('acc_bias') or 0.0) * 100:+.1f} pontos, "
                      f"P(passar) {float(adj.get('pass_offset') or 0.0):+.2f} no logit"] if adj else [])
        return {"player": {"user_id": user_id, "username": pdata.username, "levels": {a: round(v, 1) for a, v in pdata.levels.items()},
                           "n_passes_used": pdata.n_passes, "n_played_maps": len(pdata.played),
                           "adjust": None if not adj else {"acc_bias_pts": round(float(adj.get("acc_bias") or 0.0) * 100, 2), "pass_offset": adj.get("pass_offset"),
                                                           "n_acc": adj.get("n_acc"), "n_pass": adj.get("n_pass")},
                           "n_blocked": int(blocked.sum())},
                "skills": skills, "items": items,
                "counts": {"novo": int(new_ok.sum()), "tentar_de_novo": int(retry_ok.sum()), "rejogar": int(replay_ok.sum()), "sugestoes_seguras": n_safe},
                "notes": [*pdata.notes, *notes_adj, "Atributos dos mapas sem mods; previsão com o perfil de forma atual do jogador (não a forma de hoje, nem mods).",
                          "Nível = P90 dos melhores passes; nota 50 = mapa mediano, +20 por desvio-padrão.",
                          f"Regra: P(passar) ≥ {MIN_PASS_PROB * 100:.0f} % e accuracy esperada ao passar ≥ {MIN_EXPECTED_ACC * 100:.0f} % (ideal ~{TARGET_ACC * 100:.0f} %). "
                          "'arriscado' = P(passar) entre 70 % e 80 %."]}

    def _item(self, j, kind, lab, idx, score, p_pass, acc, p_raw, acc_raw, acc_cur, gain_acc, gain_pp, sel_delta, delta, style, pdata, skills):
        import numpy as np

        bid = int(idx["ids"][j])
        title = f'{lab.get("artist", "")} – {lab.get("title", "")}'.strip(" –") if lab.get("title") else f"mapa #{bid}"
        version = lab.get("version", "")
        d = {a: round(float(delta[a][j]), 1) for a in AXES}
        skill_txt = ", ".join(f"{AXIS_LABEL[a]} {d[a]:+.1f}" for a in skills)
        parts = [f"{skill_txt} vs o teu nível ({', '.join(f'{AXIS_LABEL[a]} {pdata.levels[a]:.0f}' for a in skills)})",
                 f"probabilidade de passar ≈ {p_pass[j] * 100:.0f} %; accuracy esperada ao passar ≈ {acc[j] * 100:.1f} %"]
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
                "p_pass": round(float(p_pass[j]), 3), "acc_pass": round(float(acc[j]), 4),
                "p_pass_raw": round(float(p_raw[j]), 3), "acc_pass_raw": round(float(acc_raw[j]), 4),
                "acc_cur": None if np.isnan(acc_cur[j]) else round(float(acc_cur[j]), 4),
                "pp_gain_pct": round(float(gain_pp[j]), 1) if kind == "rejogar" else None,
                "style_pct": round(float(style[j]) * 100, 1), "score": round(float(score[j]), 4), "why": "; ".join(parts)}

    # -------------------------------------------------------------- bloqueios
    def _blocked_mask(self, user_id: int):
        """Máscara (n_mapas,) dos mapas que este jogador pediu para não receber: `set` = todas as dificuldades do mapa, `diff` = só essa dificuldade."""
        import numpy as np
        from sqlalchemy import select

        from ..storage import models as m

        ids, set_ids = self._index["ids"], self._set_ids()
        mask = np.zeros(len(ids), dtype=bool)
        with self.store.engine.connect() as c:
            rows = c.execute(select(m.recommendation_blocks.c.scope, m.recommendation_blocks.c.beatmap_id, m.recommendation_blocks.c.beatmapset_id)
                             .where(m.recommendation_blocks.c.user_id == user_id)).all()
        bsets = {int(sid) for scope, _, sid in rows if scope == "set" and sid}
        bdiffs = {int(b) for scope, b, _ in rows if scope == "diff" and b}
        if bsets:
            mask |= np.isin(set_ids, list(bsets)) & (set_ids > 0)
        if bdiffs:
            mask |= np.isin(ids, list(bdiffs))
        return mask

    def _set_ids(self):
        """(n_mapas,) id do set de cada mapa do índice (0 se desconhecido); calculado uma vez."""
        import numpy as np

        idx = self._index
        if idx.get("set_ids") is None or len(idx["set_ids"]) != len(idx["ids"]):
            idx["set_ids"] = np.array([int((idx["labels"].get(int(b)) or {}).get("set_id") or 0) for b in idx["ids"]], dtype=np.int64)
        return idx["set_ids"]

    def _set_of(self, beatmap_id: int) -> int | None:
        sid = (self._index["labels"].get(int(beatmap_id)) or {}).get("set_id")
        if sid:
            return int(sid)
        from sqlalchemy import select

        from ..storage import models as m

        with self.store.engine.connect() as c:
            v = c.execute(select(m.beatmaps.c.beatmapset_id).where(m.beatmaps.c.beatmap_id == int(beatmap_id))).scalar()
        return int(v) if v else None

    def _set_label(self, set_id: int) -> str:
        for lab in self._index["labels"].values():
            if lab.get("set_id") == set_id and lab.get("title"):
                return f'{lab.get("artist", "")} - {lab.get("title", "")}'.strip(" -")
        return f"mapa (set {set_id})"

    def block_map(self, user_id: int, *, beatmap_id: int | None = None, beatmapset_id: int | None = None, scope: str = "set", note: str = "") -> dict[str, Any]:
        """Deixa de recomendar um mapa a este jogador. `scope="set"` (omissão): o mapa inteiro, todas as dificuldades — se o set for desconhecido, bloqueia só essa
        dificuldade. `scope="diff"`: só a dificuldade `beatmap_id`. Guarda a preferência na BD e uma linha no ficheiro de feedback."""
        from sqlalchemy import select

        from ..storage import models as m
        from ..storage.database import utcnow

        if scope not in ("set", "diff"):
            return {"error": "âmbito inválido (set ou diff)"}
        if beatmap_id is None and beatmapset_id is None:
            return {"error": "indica o mapa (id da dificuldade, id do set ou link)"}
        try:
            self._load()
        except Exception:  # sem índice (testes): bloqueia-se na mesma, sem etiquetas
            self._index = self._index or {"ids": [], "x": None, "axis": None, "labels": {}, "set_ids": []}
        if beatmapset_id is None and beatmap_id is not None:
            beatmapset_id = self._set_of(beatmap_id)
        if scope == "set" and not beatmapset_id:
            scope = "diff"  # set desconhecido: só se consegue bloquear a dificuldade
        if scope == "diff" and beatmap_id is None:
            return {"error": "para bloquear só uma dificuldade é preciso o id dessa dificuldade"}
        lab = (self._index["labels"].get(int(beatmap_id)) or {}) if beatmap_id is not None else {}
        label = f'{lab.get("artist", "")} - {lab.get("title", "")}'.strip(" -") if lab.get("title") else (self._set_label(int(beatmapset_id)) if beatmapset_id else "")
        if scope == "diff" and lab.get("version"):
            label += f' [{lab["version"]}]'
        bm = int(beatmap_id) if beatmap_id is not None else None
        st = int(beatmapset_id) if beatmapset_id else None
        bl = m.recommendation_blocks.c
        with self.store.engine.begin() as c:
            dup = c.execute(select(bl.id).where(bl.user_id == user_id, bl.scope == scope, (bl.beatmapset_id == st) if scope == "set" else (bl.beatmap_id == bm))).first()
            when = utcnow()
            name = c.execute(select(m.users.c.username).where(m.users.c.user_id == user_id)).scalar()
            if dup is None:
                c.execute(m.recommendation_blocks.insert().values(user_id=user_id, scope=scope, beatmap_id=bm, beatmapset_id=st, label=label[:300], note=note[:300], created_at=when))
        if dup is None:
            self._write_feedback_line(when, name or user_id, user_id, bm, st, label, "bloquear_mapa" if scope == "set" else "bloquear_dificuldade", "", [], None, note)
        return {"ok": True, "scope": scope, "beatmap_id": bm, "beatmapset_id": st, "label": label, "already": dup is not None}

    def unblock(self, user_id: int, block_id: int) -> dict[str, Any]:
        from sqlalchemy import select

        from ..storage import models as m
        from ..storage.database import utcnow

        bl = m.recommendation_blocks
        with self.store.engine.begin() as c:
            row = c.execute(select(bl).where(bl.c.id == block_id, bl.c.user_id == user_id)).mappings().first()
            if row is None:
                return {"error": "bloqueio não encontrado"}
            c.execute(bl.delete().where(bl.c.id == block_id))
            name = c.execute(select(m.users.c.username).where(m.users.c.user_id == user_id)).scalar()
        self._write_feedback_line(utcnow(), name or user_id, user_id, row["beatmap_id"], row["beatmapset_id"], row["label"] or "", "desbloquear", "", [], None, "")
        return {"ok": True}

    def blocks(self, user_id: int) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from ..storage import models as m

        bl = m.recommendation_blocks
        with self.store.engine.connect() as c:
            rows = c.execute(select(bl).where(bl.c.user_id == user_id).order_by(bl.c.created_at.desc(), bl.c.id.desc())).mappings().all()
        out = []
        for r in rows:
            url = (f"https://osu.ppy.sh/beatmapsets/{r['beatmapset_id']}" if r["scope"] == "set" and r["beatmapset_id"] else beatmap_url(r["beatmap_id"], r["beatmapset_id"]))
            out.append({"id": r["id"], "scope": r["scope"], "beatmap_id": r["beatmap_id"], "beatmapset_id": r["beatmapset_id"], "label": r["label"] or "",
                        "created_at": r["created_at"].isoformat() + "Z" if r["created_at"] else None, "url": url})
        return out

    # ------------------------------------------------------------- feedback
    def model_info(self) -> dict[str, Any]:
        """Que modelo está carregado: impressão digital dos ficheiros `reach_acc*_A.txt` + resumo do treino (se o pacote o traz)."""
        import hashlib

        files = [f for f in (self.models_dir / "pass_model_A.txt", self.models_dir / "acc_pass_A.txt") if f.exists()]
        h = hashlib.sha256()
        for f in files:
            h.update(f.name.encode())
            h.update(f.read_bytes())
        info: dict[str, Any] = {"fingerprint": h.hexdigest()[:12] if files else None, "n_models": len(files)}
        cal = self.models_dir / "calibration_pass_acc.json"
        if cal.exists():
            import json as _json

            try:
                c = _json.loads(cal.read_text(encoding="utf-8"))
                info["calibration"] = {"n_players": c.get("n_players"), "n_pairs": c.get("n_pairs"), "created_at": c.get("created_at")}
            except ValueError:
                pass
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
               "" if score is None else f"{score:.4f}", note,
               beatmap_url(beatmap_id, set_id) if beatmap_id is not None else (f"https://osu.ppy.sh/beatmapsets/{int(set_id)}" if set_id else "")]
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
