"""Vista DT dos mapas: as mesmas colunas do índice (`pass_model.MAP_FEATS`), mas com os valores **efetivos** de jogar com DT.

O DT muda muita coisa ao mesmo tempo (clock rate 1,5×): BPM, AR, janelas de acerto (OD), densidade por segundo, duração e as dificuldades de aim/speed/reading.
As linhas DT do catálogo (`map_attributes.parquet`, mods == "DT") já trazem `stars/aim/speed/reading` recalculados pelo rosu-pp / port do lazer com o clock rate e `ar_mod`
(AR efetivo), mas **não** escalam a `density`, a `length_ms` nem o `od`. Aqui corrige-se isso:
- `density` (objetos/s) × clock; `length_s` ÷ clock; `ar` = `ar_mod` (já efetivo);
- `od` efetivo: janela de 300 = 80 − 6·OD ms, dividida pelo clock ⇒ OD_eff = (80 − (80 − 6·OD)/clock) / 6 (ex.: OD 9 → 10,4);
- `hp` = `hp_mod`; `cs` e `n_objects` não mudam.
As notas por eixo (aim, speed, stamina, reading, stars) usam a mesma escala de referência (pool v3, nomod) aplicada aos valores DT; a stamina usa a densidade já ×clock.
Limitações: só DT/NC puro (sem HD/HR); a escala de referência é nomod (um mapa DT "mais difícil que 95 % da pool" é-o contra mapas nomod); a curva real do DT para um jogador
depende de ele ter treino a essa velocidade — isso vem do ajuste medido com jogadores da API (`calibration_dt.json`), não da vista.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SUPPORTED = ("DT", "HT", "HD", "HR", "EZ")  # FL fica de fora por decisão do projeto
AXES5 = ("aim", "speed", "stamina", "reading", "stars")
IGNORED_MODS = {"NF", "SO", "SD", "PF", "TD", "AC", "MR", "DA", "RX", "AP", "TP", "BL", "CS", "WU", "WD", "AS", "CL"}  # sem efeito na dificuldade/AR


def normalize_mods(mods) -> str | None:
    """"dt, hd" / ["HD","NC"] → chave do catálogo ("DT,HD", ordem alfabética, NC=DT); "" = sem mods; None se inválido (mods fora de DT/HT/HD/HR/EZ ou combinações impossíveis)."""
    if isinstance(mods, str):
        mods = [x for x in mods.replace("+", ",").replace(" ", ",").split(",") if x]
    ms = {"DT" if str(x).upper() == "NC" else str(x).upper() for x in (mods or [])} - {"NM", "CL"}
    if not ms <= set(SUPPORTED) or {"DT", "HT"} <= ms or {"HR", "EZ"} <= ms:
        return None
    return ",".join(sorted(ms))


def clock_of(mods: str) -> float:
    ms = set(mods.split(","))
    return 1.5 if "DT" in ms else (0.75 if "HT" in ms else 1.0)


def stable_key(mods_acronyms: str | None) -> str:
    """mods de um score → chave do catálogo, ignorando mods sem efeito na dificuldade (NF, SO, CL, ...)."""
    ms = {("DT" if x == "NC" else x) for x in (mods_acronyms or "").split(",") if x} - IGNORED_MODS
    return ",".join(sorted(ms))
_COLS = ["beatmap_id", "mods", "stars", "aim", "speed", "reading", "density", "ar_mod", "cs", "od", "hp_mod", "n_objects", "length_ms"]


def effective_od(od: float, clock: float) -> float:
    """OD equivalente quando as janelas de acerto (em ms reais) encolhem com o clock rate."""
    w300 = 80.0 - 6.0 * od
    return (80.0 - w300 / clock) / 6.0


def build_view(catalog_path: Path, ids, reference_pool: Path, mods: str = "DT") -> dict[str, Any]:
    """{'x': (n, 11) float32, 'axis': (n, 5) float32, 'valid': (n,) bool} alinhado com `ids` (ordenados) do índice base; `valid` = existe linha `mods` no catálogo."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from ..beatmaps.skills import ReferenceScale

    clock = clock_of(mods)
    ms = set(mods.split(","))
    t = pq.read_table(catalog_path, columns=_COLS)
    t = t.filter(pc.equal(t.column("mods"), mods))
    bid = t.column("beatmap_id").to_numpy().astype(np.int64)
    col = {c: t.column(c).to_numpy(zero_copy_only=False).astype(np.float64) for c in _COLS if c not in ("beatmap_id", "mods")}
    n = len(ids)
    pos = np.searchsorted(ids, bid)
    pos[pos >= n] = 0
    ok = ids[pos] == bid
    pos = pos[ok]
    c = {k: v[ok] for k, v in col.items()}
    od = c["od"]
    cs = c["cs"]
    if "HR" in ms:
        od, cs = np.minimum(od * 1.4, 10.0), np.minimum(cs * 1.3, 10.0)
    elif "EZ" in ms:
        od, cs = od / 2.0, cs / 2.0
    od_eff = (80.0 - (80.0 - 6.0 * od) / clock) / 6.0
    density = c["density"] * clock
    rows = np.column_stack([c["stars"], c["aim"], c["speed"], np.log1p(np.clip(c["reading"], 0, 1000)), density, c["ar_mod"], cs, od_eff,
                            c["hp_mod"], c["n_objects"], c["length_ms"] / 1000.0 / clock])
    rows = np.clip(np.nan_to_num(rows, nan=0.0, posinf=1e6, neginf=-1e6), -1e6, 1e6).astype(np.float32)
    scale = ReferenceScale.from_parquet(reference_pool)
    field = {"aim": c["aim"], "speed": c["speed"], "stamina": density, "reading": c["reading"], "stars": c["stars"]}
    name = {"aim": "aim", "speed": "speed", "stamina": "density", "reading": "reading", "stars": "stars"}
    ax = np.array([[scale.score(name[a], float(v)) for v in field[a]] for a in AXES5], dtype=np.float32).T
    x = np.zeros((n, rows.shape[1]), dtype=np.float32)
    axis = np.zeros((n, len(AXES5)), dtype=np.float32)
    valid = np.zeros(n, dtype=bool)
    x[pos], axis[pos], valid[pos] = rows, ax, True
    return {"x": x, "axis": axis, "valid": valid, "mods": mods, "clock": clock}


def mods_state(store: Any, user_id: int, ids, mods: str = "DT"):
    """Jogadas do jogador **com exatamente esses mods** (NC=DT; mods sem efeito na dificuldade, como NF/SO, ignorados): posições jogadas e melhor accuracy dos passes, no índice `ids`."""
    import numpy as np
    from sqlalchemy import select

    from ..storage import models as m

    with store.engine.connect() as c:
        rows = c.execute(select(m.scores.c.beatmap_id, m.scores.c.passed, m.scores.c.accuracy, m.scores.c.mod_acronyms)
                         .where(m.scores.c.user_id == user_id, m.scores.c.beatmap_id.isnot(None))).all()
    played: set[int] = set()
    best: dict[int, float] = {}
    for bid, passed, acc, mod in rows:
        if stable_key(mod) != mods:
            continue
        p = int(np.searchsorted(ids, bid))
        if p >= len(ids) or ids[p] != bid:
            continue
        played.add(p)
        if passed and acc is not None:
            best[p] = max(best.get(p, 0.0), float(acc))
    return played, best


class CatalogLookup:
    """(beatmap_id, mods) → (AR efetivo, estrelas) do catálogo, para medir o que o jogador já passou (limites pessoais)."""

    def __init__(self, catalog_path: Path) -> None:
        import numpy as np
        import pyarrow.parquet as pq

        t = pq.read_table(catalog_path, columns=["beatmap_id", "mods", "ar_mod", "stars"])
        mods = t.column("mods").to_pylist()
        self.codes = {m: i for i, m in enumerate(sorted(set(mods)))}
        key = t.column("beatmap_id").to_numpy().astype(np.int64) * 64 + np.array([self.codes[m] for m in mods], dtype=np.int64)
        order = np.argsort(key)
        self.keys = key[order]
        self.ar = t.column("ar_mod").to_numpy(zero_copy_only=False).astype(np.float32)[order]
        self.stars = t.column("stars").to_numpy(zero_copy_only=False).astype(np.float32)[order]

    def get(self, beatmap_ids, mods_key: str):
        import numpy as np

        code = self.codes.get(mods_key)
        ids = np.asarray(beatmap_ids, dtype=np.int64)
        if code is None or not len(ids):
            return np.full(len(ids), np.nan, dtype=np.float32), np.full(len(ids), np.nan, dtype=np.float32)
        k = ids * 64 + code
        pos = np.searchsorted(self.keys, k)
        pos[pos >= len(self.keys)] = len(self.keys) - 1
        ok = self.keys[pos] == k
        return np.where(ok, self.ar[pos], np.nan), np.where(ok, self.stars[pos], np.nan)


AR_MARGIN = 0.3          # folga sobre o AR efetivo mais alto já passado
STARS_MARGIN = 0.8       # folga sobre as estrelas (com esses mods) mais altas já passadas
MIN_ACC_DEMONSTRATED = 0.90
MIN_PASSES_FOR_CEILING = 3


def player_limits(store: Any, user_id: int, lookup: CatalogLookup, mods: str) -> dict[str, Any]:
    """Limites pessoais **medidos nos passes do jogador** (accuracy ≥ 90 %): AR efetivo máximo (qualquer mods) e estrelas máximas **com esses mods** (+ folgas).
    Sem passes suficientes com esses mods, o tecto de estrelas é cauteloso (o melhor passe sem mods − 1★), porque o modelo não consegue extrapolar."""
    import numpy as np
    from sqlalchemy import select

    from ..storage import models as m

    with store.engine.connect() as c:
        rows = c.execute(select(m.scores.c.beatmap_id, m.scores.c.accuracy, m.scores.c.mod_acronyms)
                         .where(m.scores.c.user_id == user_id, m.scores.c.passed.is_(True), m.scores.c.beatmap_id.isnot(None),
                                m.scores.c.accuracy >= MIN_ACC_DEMONSTRATED)).all()
    by_key: dict[str, list[int]] = {}
    for bid, _acc, mod in rows:
        by_key.setdefault(stable_key(mod), []).append(int(bid))
    ars, stars_by = [], {}
    for key, bids in by_key.items():
        ar, st = lookup.get(bids, key)
        ars.extend(ar[~np.isnan(ar)].tolist())
        stars_by[key] = st[~np.isnan(st)]
    out: dict[str, Any] = {"ar_cap": None, "stars_ceiling": None, "n_passes_same_mods": 0, "source": ""}
    if ars:
        out["ar_cap"] = round(float(max(ars)) + AR_MARGIN, 2)
    same = stars_by.get(mods, np.array([]))
    out["n_passes_same_mods"] = int(len(same))
    if len(same) >= MIN_PASSES_FOR_CEILING:
        out["stars_ceiling"] = round(float(same.max()) + STARS_MARGIN, 2)
        out["source"] = f"passes teus com {mods or 'sem mods'} (máx. {float(same.max()):.2f}★)"
    elif len(same):
        out["stars_ceiling"] = round(float(same.max()) + STARS_MARGIN, 2)
        out["source"] = f"só {len(same)} passes teus com {mods or 'sem mods'} (máx. {float(same.max()):.2f}★): tecto muito incerto"
    else:
        nm = stars_by.get("", np.array([]))
        if len(nm):
            out["stars_ceiling"] = round(float(nm.max()) - 1.0, 2) if mods else round(float(nm.max()) + STARS_MARGIN, 2)
            out["source"] = f"sem passes teus com {mods or 'sem mods'}: tecto cauteloso a partir do teu melhor passe sem mods ({float(nm.max()):.2f}★)"
    return out
