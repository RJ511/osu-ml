"""Ganho de pp de uma sugestão: quanto sobem os pp TOTAIS do jogador se fizer aquele mapa, e preditor do pp oficial para os candidatos.

Fórmula do jogo: os melhores scores (um por mapa) ordenados por pp descendente, ponderados por `0,95^i`; total = Σ 0,95^i · pp_i.
Acrescentar um score de pp `x` na posição `k` (nº de scores com pp ≥ x) dá
    ganho(x) = 0,95^k · x − 0,05 · Σ_{i ≥ k} 0,95^i · pp_i
(o novo score entra com o seu peso e empurra todos os de baixo uma posição, o que lhes tira 5 % do peso). Para repetir um mapa já jogado
retira-se primeiro o score antigo desse mapa. Não conta o bónus por nº de scores nem o limite do top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

DECAY = 0.95
FOCUS_WEIGHT = {"skill": 0.0, "equilibrado": 0.25, "pp": 0.55}  # peso do ganho de pp na pontuação (o resto: desafio, aprendizagem, P(passar), estilo)
DEFAULT_FOCUS = "equilibrado"
AMBITIOUS_SHARE = {"skill": 0.0, "equilibrado": 0.25, "pp": 0.6}  # fração da lista reservada ao nível "ambicioso" (mapas que mexem nos pp totais)
AMBITIOUS_MIN_PASS = 0.40   # P(passar) mínima do nível ambicioso (a accuracy esperada continua ≥ 88 %)
AMBITIOUS_MIN_GAIN = 3.0    # ganho mínimo nos pp totais (se passares) para um mapa contar como "ambicioso"
MAX_AXIS_SHARE = 0.6  # com ≥ 2 skills e foco ≠ "skill": no máx. 60 % da lista com o mesmo eixo dominante (evita a lista inclinar só para o skillset que mais pp dá)


def total_pp(pps) -> float:
    import numpy as np

    p = np.sort(np.asarray(pps, dtype=np.float64))[::-1]
    return float((p * DECAY ** np.arange(len(p))).sum())


def marginal_gain(pps_desc, x) -> Any:
    """Ganho nos pp totais por acrescentar (um mapa novo) um score de pp `x` (escalar ou array) à lista `pps_desc` (já ordenada, descendente)."""
    import numpy as np

    p = np.asarray(pps_desc, dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    w = DECAY ** np.arange(len(p) + 1)
    wp = w[: len(p)] * p
    suffix = np.concatenate([np.cumsum(wp[::-1])[::-1], [0.0]])  # S_k = Σ_{i>=k} w_i p_i
    k = np.searchsorted(-p, -x, side="right")  # nº de scores com pp >= x (p é descendente)
    return w[k] * x - (1.0 - DECAY) * suffix[k]


def replace_gain(pps_desc, old: float, x: float) -> float:
    """Ganho nos pp totais por substituir o score antigo `old` (do mesmo mapa) por `x` (só conta se x > old)."""
    import numpy as np

    if x <= old:
        return 0.0
    p = np.asarray(pps_desc, dtype=np.float64)
    i = int(np.argmin(np.abs(p - old)))
    rest = np.delete(p, i)
    return total_pp(np.append(rest, x)) - total_pp(p)


class PpPredictor:
    """pp oficial esperado de um mapa a uma dada accuracy e com uns mods: `analysis/pp_model.py` + atributos do catálogo (as linhas "cruas" do mods pedido,
    exatamente como no treino, com as flags de mods)."""

    def __init__(self, models_dir: Path, catalog_path: Path) -> None:
        self.models_dir, self.catalog_path = Path(models_dir), Path(catalog_path)
        self._model = None
        self._rows: dict[str, tuple[Any, Any]] = {}

    def available(self) -> bool:
        return (self.models_dir / "pp_official.txt").exists() and self.catalog_path.exists()

    def _rows_for(self, mods: str):
        if mods in self._rows:
            return self._rows[mods]
        import lightgbm as lgb
        import numpy as np
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        from ..analysis.pp_model import MAP_COLS

        if self._model is None:
            self._model = lgb.Booster(model_file=str(self.models_dir / "pp_official.txt"))
        t = pq.read_table(self.catalog_path, columns=["beatmap_id", "mods", *MAP_COLS])
        t = t.filter(pc.equal(t.column("mods"), mods))
        ids = t.column("beatmap_id").to_numpy().astype(np.int64)
        x = np.column_stack([t.column(c).to_numpy(zero_copy_only=False).astype(np.float64) for c in MAP_COLS]).astype(np.float32)
        order = np.argsort(ids)
        self._rows[mods] = (ids[order], x[order])
        return self._rows[mods]

    def predict(self, beatmap_ids, acc, *, mods: str = "", legacy: bool = False):
        """pp oficial esperado (NaN se o mapa não tem linha para esses mods). `acc` em fração (0-1), escalar ou array; `mods` = chave do catálogo ("", "DT", ...)."""
        import numpy as np

        from ..analysis.pp_model import FEATURES, flags_of

        ids_c, x_c = self._rows_for(mods)
        ids = np.asarray(beatmap_ids, dtype=np.int64)
        acc = np.broadcast_to(np.asarray(acc, dtype=np.float32), ids.shape)
        pos = np.searchsorted(ids_c, ids)
        pos[pos >= len(ids_c)] = len(ids_c) - 1
        ok = ids_c[pos] == ids
        out = np.full(len(ids), np.nan)
        if ok.any():
            n = int(ok.sum())
            flags = np.tile(np.array(flags_of(mods), dtype=np.float32), (n, 1))
            feats = np.column_stack([acc[ok], np.full(n, float(legacy), dtype=np.float32), flags, x_c[pos[ok]]])
            assert feats.shape[1] == len(FEATURES)
            out[ok] = np.exp(self._model.predict(feats))
        return out

    def predict_nomod(self, beatmap_ids, acc, *, legacy: bool = False):
        return self.predict(beatmap_ids, acc, mods="", legacy=legacy)


def player_best_pp(store: Any, user_id: int) -> dict[int, float]:
    """Melhor pp por mapa (passes com pp) do jogador, da BD local."""
    from sqlalchemy import select

    from ..storage import models as m

    best: dict[int, float] = {}
    with store.engine.connect() as c:
        for bid, pp in c.execute(select(m.scores.c.beatmap_id, m.scores.c.pp).where(m.scores.c.user_id == user_id, m.scores.c.passed.is_(True),
                                                                                  m.scores.c.pp.isnot(None), m.scores.c.beatmap_id.isnot(None))):
            if bid not in best or pp > best[bid]:
                best[int(bid)] = float(pp)
    return best
