"""Correção por jogador e recalibração periódica das previsões (0 pedidos à API), a partir do registo `prediction_log` (ver recommend/log.py).

**Correção por jogador** (`player_adjust.json` na pasta dos modelos): cada jogador desvia-se do modelo de forma sistemática (viés da accuracy de −0,6 a +5,1 pontos).
Estima-se só com as avaliações-sombra (perfil anterior a cada jogada; as sugestões não entram, para não haver circularidade), a partir dos valores brutos do modelo com a
calibração global atual, e encolhe-se para 0 quando há poucos dados:
- accuracy: `viés = Σ(previsto − real) / (n + K_ACC)` — subtrai-se à accuracy esperada ao passar;
- P(passar): deslocamento `δ` no logit, `δ = Σ(y − p) / (Σ p(1−p) + 1/τ²)` (um passo de Newton com prior N(0, τ²)) — só pares com tentativas do lazer (o stable não envia falhas).
Validado fora do tempo (cada previsão só com o passado do jogador; 3 843 pares, 73 jogadores): MAE da accuracy 4,37 → 4,07 pontos, Brier de P(passar) 0,195 → 0,183.

**Recalibração** (`recalibrate`): reajusta a calibração global (`calibration_pass_acc.json`: logit(p_cal) = a + b·logit(p) e o deslocamento da accuracy) com os pares do registo
e só substitui a atual se a validação cruzada por jogador melhorar. É a alternativa barata a re-treinar o modelo: re-treinar só com dumps novos.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

K_ACC = 10.0  # encolhimento da accuracy: n / (n + K_ACC)
TAU_PASS = 0.5  # desvio-padrão do prior do deslocamento do logit de P(passar)
MIN_PAIRS_ACC = 5  # abaixo disto não há correção (o encolhimento faria quase nada)
MIN_PAIRS_PASS = 10
ADJUST_FILE = "player_adjust.json"

MIN_RECAL_PAIRS = 500
MIN_RECAL_PLAYERS = 30
MIN_BRIER_GAIN = 0.002  # a nova calibração tem de melhorar o Brier em CV por jogador pelo menos isto (o mesmo para o MAE da accuracy em pontos/100)
MIN_MAE_GAIN = 0.0005


def _rows(store, since: datetime | None = None, model_fp: str | None = None) -> list[dict[str, Any]]:
    """Avaliações-sombra com resultado. `model_fp`: só as previsões feitas por esse modelo (depois de um retreino as antigas não servem para corrigir o novo)."""
    from sqlalchemy import select

    from ..storage import models as m

    pl = m.prediction_log
    q = select(pl).where(pl.c.kind == "sombra", pl.c.evaluated_at.isnot(None))
    if since is not None:
        q = q.where(pl.c.asof >= since)
    if model_fp is not None:
        q = q.where(pl.c.model_fp == model_fp)
    with store.engine.connect() as c:
        return [dict(r) for r in c.execute(q).mappings().all()]


def _current_calibration(models_dir: Path | None) -> tuple[dict | None, float]:
    if models_dir is None or not (Path(models_dir) / "calibration_pass_acc.json").exists():
        return None, 0.0
    d = json.loads((Path(models_dir) / "calibration_pass_acc.json").read_text(encoding="utf-8"))
    return d.get("pass"), float(d.get("acc_shift") or 0.0)


def compute_adjustments(store, models_dir: Path | None = None, since: datetime | None = None) -> dict[int, dict[str, Any]]:
    """{user_id: {acc_bias, n_acc, pass_offset, n_pass}}. `acc_bias` = previsto − real (encolhido): subtrai-se à accuracy prevista.
    Parte dos valores BRUTOS do modelo e aplica a calibração global **atual** (`models_dir`): assim o ajuste continua correto depois de uma recalibração."""
    import numpy as np

    from ..analysis.reach_calibration import logit, sigmoid

    from .core import models_fingerprint

    cal_pass, acc_shift = _current_calibration(models_dir)
    by_user: dict[int, dict[str, Any]] = {}
    for r in _rows(store, since, models_fingerprint(models_dir) if models_dir is not None else None):
        u = by_user.setdefault(int(r["user_id"]), {"d": [], "s": 0.0, "v": 0.0, "n_pass": 0})
        if r["best_acc"] is not None and r["acc_pass_raw"] is not None:
            u["d"].append(float(np.clip(r["acc_pass_raw"] + acc_shift, 0.0, 1.0)) - float(r["best_acc"]))
        if (r["n_lazer_attempts"] or 0) > 0 and r["p_pass_raw"] is not None and r["passed"] is not None:
            praw = float(np.clip(r["p_pass_raw"], 1e-4, 1 - 1e-4))
            p = float(sigmoid(cal_pass["a"] + cal_pass["b"] * logit(praw))) if cal_pass else praw
            p = float(np.clip(p, 1e-4, 1 - 1e-4))
            u["s"] += (1.0 if r["passed"] else 0.0) - p
            u["v"] += p * (1 - p)
            u["n_pass"] += 1
    out: dict[int, dict[str, Any]] = {}
    for uid, u in by_user.items():
        n = len(u["d"])
        entry: dict[str, Any] = {"n_acc": n, "acc_bias": 0.0, "n_pass": u["n_pass"], "pass_offset": 0.0}
        if n >= MIN_PAIRS_ACC:
            entry["acc_bias"] = round(float(np.sum(u["d"]) / (n + K_ACC)), 5)
        if u["n_pass"] >= MIN_PAIRS_PASS:
            entry["pass_offset"] = round(float(u["s"] / (u["v"] + 1.0 / TAU_PASS**2)), 4)
        if entry["acc_bias"] or entry["pass_offset"]:
            out[uid] = entry
    return out


def save_adjustments(models_dir: Path, adjustments: dict[int, dict[str, Any]], *, only: set[int] | None = None) -> Path:
    """Escreve `player_adjust.json`. `only` restringe a estes jogadores (o pacote leva só os jogadores pedidos)."""
    data = {str(k): v for k, v in sorted(adjustments.items()) if only is None or k in only}
    path = Path(models_dir) / ADJUST_FILE
    path.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "k_acc": K_ACC, "tau_pass": TAU_PASS, "players": data}, indent=2), encoding="utf-8")
    return path


def load_adjustments(models_dir: Path) -> dict[int, dict[str, Any]]:
    f = Path(models_dir) / ADJUST_FILE
    if not f.exists():
        return {}
    try:
        return {int(k): v for k, v in json.loads(f.read_text(encoding="utf-8")).get("players", {}).items()}
    except (ValueError, TypeError):
        return {}


def apply_adjustment(p_pass, acc, adj: dict[str, Any] | None):
    """(p_pass, acc) corrigidos para um jogador; sem ajuste devolve os valores iguais."""
    import numpy as np

    from ..analysis.reach_calibration import logit, sigmoid

    if not adj:
        return p_pass, acc
    p = sigmoid(logit(p_pass) + float(adj.get("pass_offset") or 0.0)) if adj.get("pass_offset") else p_pass
    a = np.clip(np.asarray(acc, dtype=np.float64) - float(adj.get("acc_bias") or 0.0), 0.0, 1.0) if adj.get("acc_bias") else acc
    return p, a


def refresh_adjustments(store, models_dir: Path) -> dict[str, Any]:
    """Recalcula e grava `player_adjust.json` (barato: só lê o registo). Chamado no fim do `poll` depois da avaliação."""
    adj = compute_adjustments(store, models_dir)
    save_adjustments(models_dir, adj)
    return {"players": len(adj)}


def recalibrate(store, models_dir: Path, *, apply: bool = False, folds: int = 5) -> dict[str, Any]:
    """Reajusta a calibração global com o registo. Devolve o que mudaria; só grava com `apply=True` **e** se a validação cruzada por jogador melhorar."""
    import numpy as np

    from ..analysis.reach_calibration import ece, fit_platt, logit, sigmoid

    from .core import models_fingerprint

    models_dir = Path(models_dir)
    rows = _rows(store, model_fp=models_fingerprint(models_dir))
    lz = [r for r in rows if (r["n_lazer_attempts"] or 0) > 0 and r["p_pass_raw"] is not None and r["passed"] is not None]
    users = sorted({r["user_id"] for r in lz})
    out: dict[str, Any] = {"n_pairs_lazer": len(lz), "n_players_lazer": len(users), "applied": False}
    if len(lz) < MIN_RECAL_PAIRS or len(users) < MIN_RECAL_PLAYERS:
        out["reason"] = f"poucos dados para recalibrar (precisa de ≥ {MIN_RECAL_PAIRS} pares e ≥ {MIN_RECAL_PLAYERS} jogadores do lazer)"
        return out
    cal_file = models_dir / "calibration_pass_acc.json"
    cur = json.loads(cal_file.read_text(encoding="utf-8")) if cal_file.exists() else {}
    cur_pass = cur.get("pass") or {"a": 0.0, "b": 1.0}
    P = np.array([r["p_pass_raw"] for r in lz], dtype=np.float64)
    Y = np.array([1.0 if r["passed"] else 0.0 for r in lz])
    U = np.array([users.index(r["user_id"]) for r in lz])
    a, b = fit_platt(Y, P)
    cv = np.zeros(len(P))
    for f in range(folds):
        tr, te = (U % folds) != f, (U % folds) == f
        if te.any() and tr.any():
            af, bf = fit_platt(Y[tr], P[tr])
            cv[te] = sigmoid(af + bf * logit(P[te]))
    now_p = sigmoid(cur_pass["a"] + cur_pass["b"] * logit(P))
    out["pass"] = {"current": {"a": cur_pass["a"], "b": cur_pass["b"], "brier": round(float(np.mean((now_p - Y) ** 2)), 4), "ece": round(ece(Y, now_p), 4)},
                   "new": {"a": round(a, 4), "b": round(b, 4), "brier_cv": round(float(np.mean((cv - Y) ** 2)), 4), "ece_cv": round(ece(Y, cv), 4)}}
    ac = [r for r in rows if r["best_acc"] is not None and r["acc_pass_raw"] is not None]
    new_shift = float(cur.get("acc_shift") or 0.0)
    acc_out = None
    if len(ac) >= 200:
        idx = {u: i for i, u in enumerate(sorted({r["user_id"] for r in ac}))}
        y_ = np.array([r["best_acc"] for r in ac]); p_ = np.array([r["acc_pass_raw"] for r in ac]); u_ = np.array([idx[r["user_id"]] for r in ac])
        new_shift = float(np.median(y_ - p_))
        cvp = np.zeros(len(y_))
        for f in range(folds):
            tr, te = (u_ % folds) != f, (u_ % folds) == f
            if te.any() and tr.any():
                cvp[te] = p_[te] + float(np.median(y_[tr] - p_[tr]))
        cur_shift = float(cur.get("acc_shift") or 0.0)
        acc_out = {"n": len(ac), "current_shift": cur_shift, "new_shift": round(new_shift, 4), "mae_current": round(float(np.mean(np.abs(p_ + cur_shift - y_))), 4),
                   "mae_new_cv": round(float(np.mean(np.abs(cvp - y_))), 4)}
        out["acc"] = acc_out
    better = out["pass"]["current"]["brier"] - out["pass"]["new"]["brier_cv"] >= MIN_BRIER_GAIN
    better_acc = acc_out is not None and acc_out["mae_current"] - acc_out["mae_new_cv"] >= MIN_MAE_GAIN
    out["improves"] = {"pass": bool(better), "acc": bool(better_acc)}
    if apply and (better or better_acc):
        if cal_file.exists():
            shutil.copy2(cal_file, cal_file.with_name(f"calibration_pass_acc.json.bak-{datetime.now(timezone.utc):%Y%m%d-%H%M}"))
        new = dict(cur)
        if better:
            new["pass"] = {"a": round(a, 4), "b": round(b, 4)}
        if better_acc:
            new["acc_shift"] = round(new_shift, 4)
        new["created_at"] = datetime.now(timezone.utc).isoformat()
        new["recalibrated_from_log"] = {"pairs_lazer": len(lz), "players": len(users)}
        cal_file.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
        out["applied"] = True
    elif apply:
        out["reason"] = "a calibração atual já é tão boa como a nova (ou melhor): nada foi alterado"
    return out
