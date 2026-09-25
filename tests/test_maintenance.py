"""Manutenção periódica: dumps novos, decisão/envio do pacote, simulação mensal, fecho local do retreino e auxiliares do pipeline do pod."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from osuml import maintenance as mt
from osuml.storage import models as m
from osuml.storage.database import Store, utcnow

LISTING = """<a href="2026_08_01_performance_osu_random_10000.tar.bz2">x</a><a href="2026_08_01_performance_osu_top_10000.tar.bz2">x</a><a href="2026_08_01_osu_files.tar.bz2">x</a>
<a href="2026_09_01_performance_osu_random_10000.tar.bz2">x</a><a href="2026_09_01_performance_osu_top_10000.tar.bz2">x</a><a href="2026_09_01_osu_files.tar.bz2">x</a>
<a href="2026_10_01_performance_osu_random_10000.tar.bz2">x</a><a href="2026_10_01_performance_osu_top_1000.tar.bz2">x</a>"""  # 10_01 ainda incompleto


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path / "data", processed_dir=tmp_path / "data" / "processed", s3_bucket="b", s3_region="r")


# ------------------------------------------------------------------ dumps novos
def test_only_complete_snapshots_count():
    assert mt.complete_snapshots(LISTING) == ["2026_08_01", "2026_09_01"]  # 2026_10_01 não tem top_10000 nem osu_files: ainda não
    assert mt.complete_snapshots(LISTING + '<a href="2026_10_01_performance_osu_top_10000.tar.bz2">x</a><a href="2026_10_01_osu_files.tar.bz2">x</a>')[-1] == "2026_10_01"
    assert mt.complete_snapshots("") == []


def test_dumps_check_only_acts_in_the_first_week_and_marks_a_retrain_once(tmp_path):
    st = _settings(tmp_path)

    def fetch_none():
        raise AssertionError("fora da janela não se vai à rede")

    out = mt.dumps_check(st, today=date(2026, 9, 25), fetch=fetch_none)
    assert out["checked"] is False and "1.ª semana" in out["reason"] and not (st.data_dir / "control" / "retrain_pending.json").exists()
    out = mt.dumps_check(st, today=date(2026, 10, 2), fetch=lambda: LISTING)  # 09_01 é o que já está treinado
    assert out["checked"] is True and out["new"] is False and out["latest_complete_snapshot"] == "2026_09_01"
    full = LISTING + '<a href="2026_10_01_performance_osu_top_10000.tar.bz2">x</a><a href="2026_10_01_osu_files.tar.bz2">x</a>'
    out = mt.dumps_check(st, today=date(2026, 10, 3), fetch=lambda: full)
    assert out["new"] is True and out["pending"]["snapshot"] == "2026_10_01" and out["pending"]["random_snapshots"][-1] == "2026_10_01"
    pend = json.loads((st.data_dir / "control" / "retrain_pending.json").read_text(encoding="utf-8"))
    assert pend["previous_trained"] == "2026_09_01" and pend["top_snapshot"] == "2026_10_01"
    again = mt.dumps_check(st, today=date(2026, 10, 4), fetch=lambda: full)
    assert again["new"] is True and "já pendente" in again["reason"]
    assert mt.dumps_check(st, today=date(2026, 10, 20), force=True, fetch=lambda: full)["checked"] is True  # --force ignora a janela
    assert (st.data_dir / "logs" / "maintenance.jsonl").read_text(encoding="utf-8").count("dumps-check") >= 2


# ------------------------------------------------------------------ quando vale a pena enviar o pacote
def _snap(model="m1", cal="c1", index="i1", acc=0.0, pas=0.0):
    return {"model": model, "calibration": cal, "index": index, "adjust": {"PXD": {"acc_bias": acc, "pass_offset": pas}}}


def test_pack_pertinence_rules():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert mt.pack_pertinence(None, _snap())[0] is True                                                                     # nunca enviado
    for changed in (_snap(model="m2"), _snap(cal="c2"), _snap(index="i2")):                                                # modelo/calibração/índice: logo
        ok, why = mt.pack_pertinence(_snap(), changed, last_upload=now - timedelta(days=1), now=now)
        assert ok and any("mudou" in w for w in why)
    assert mt.pack_pertinence(_snap(), _snap(acc=0.004, pas=0.05), last_upload=now - timedelta(days=30), now=now) == (False, ["nada mudou de forma material desde o último envio"])
    ok, why = mt.pack_pertinence(_snap(), _snap(acc=0.02), last_upload=now - timedelta(days=2), now=now)                   # material, mas muito recente
    assert ok is False and "menos de 7 dias" in why[0]
    ok, why = mt.pack_pertinence(_snap(), _snap(pas=-0.3), last_upload=now - timedelta(days=9), now=now)
    assert ok is True and "PXD" in why[0]


def _pack_world(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    models, index = st.processed_dir / "recommend" / "models", st.processed_dir / "recommend" / "index"
    models.mkdir(parents=True)
    index.mkdir(parents=True)
    (models / "pass_model_A.txt").write_text("p1")
    (models / "acc_pass_A.txt").write_text("a1")
    (models / "calibration_pass_acc.json").write_text(json.dumps({"pass": {"a": 1.0, "b": 1.0}, "acc_shift": -0.01}))
    np.savez_compressed(index / "index.npz", ids=np.arange(3), x=np.zeros((3, 2)), axis=np.zeros((3, 5)))
    (index / "meta.json").write_text(json.dumps({"created_at": "t0", "maps": 3}))
    store = Store(f"sqlite:///{tmp_path}/p.db", tmp_path / "raw")
    with store.engine.begin() as c:
        c.execute(m.users.insert().values(user_id=7, username="Teste", raw={}, first_seen_at=utcnow(), fetched_at=utcnow(), request_id=None))
    monkeypatch.chdir(tmp_path)  # o pacote sai para ./dist
    return st, store, models


def test_pack_check_builds_uploads_once_and_records_what_was_sent(tmp_path, monkeypatch):
    st, store, models = _pack_world(tmp_path, monkeypatch)
    sent = []

    def fake_upload(zip_path, key):
        sent.append((Path(zip_path).name, key))
        return {"key": key, "uploaded_bytes": Path(zip_path).stat().st_size}

    dry = mt.pack_check(st, store, upload=False, players=["Teste"], uploader=fake_upload)
    assert dry["pertinent"] and dry["uploaded"] is False and not sent                                                       # sem --upload só decide
    out = mt.pack_check(st, store, upload=True, players=["Teste"], uploader=fake_upload)
    assert out["uploaded"] is True and len(sent) == 1 and sent[0][1].startswith("recommend/osuml-pack-") and out["sha256"]
    state = mt.load_state(st)
    assert state["pack"]["key"] == sent[0][1] and state["pack"]["players"] == ["Teste"]
    assert (tmp_path / "dist" / sent[0][0]).exists()
    again = mt.pack_check(st, store, upload=True, uploader=fake_upload)                                                     # nada mudou: não reenvia
    assert again["pertinent"] is False and again["uploaded"] is False and len(sent) == 1
    (models / "pass_model_A.txt").write_text("p2")                                                                          # modelo novo: reenvia
    third = mt.pack_check(st, store, upload=True, uploader=fake_upload)
    assert third["uploaded"] is True and len(sent) == 2 and "modelo mudou" in third["reasons"]


# ------------------------------------------------------------------ simulação mensal
def test_monthly_sim_writes_a_report_and_never_changes_the_calibration(tmp_path, monkeypatch):
    st, store, models = _pack_world(tmp_path, monkeypatch)
    before = (models / "calibration_pass_acc.json").read_text()
    out = mt.monthly_sim(st, store, now=datetime(2026, 11, 1, 3, 30))
    txt = Path(out["report_file"]).read_text(encoding="utf-8")
    assert out["month"] == "2026-11" and "simulação: nada foi alterado" in txt and "poucos dados" in txt
    assert (st.data_dir / "reports" / "manutencao" / "2026-11.json").exists() and (models / "calibration_pass_acc.json").read_text() == before
    assert list(models.glob("*.bak-*")) == []


# ------------------------------------------------------------------ retreino: plano e fecho local
def test_retrain_plan_lists_what_goes_to_the_pod_and_what_is_missing(tmp_path):
    st = _settings(tmp_path)
    assert mt.retrain_plan(st) == {"pending": False}
    ctl = st.data_dir / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "retrain_pending.json").write_text(json.dumps({"snapshot": "2026_10_01", "random_snapshots": ["2026_09_01", "2026_10_01"]}))
    dsc = st.processed_dir / "dump_scores" / "v1"
    dsc.mkdir(parents=True)
    (dsc / "dump_scores_2026_09_01_random_10000.parquet").write_text("x")
    plan = mt.retrain_plan(st)
    assert plan["pending"] and plan["snapshot"] == "2026_10_01" and "--top-snap 2026_10_01" in plan["pipeline_args"]
    assert len(plan["upload_to_pod_inputs"]) == 1 and len(plan["missing_local_files"]) > 5
    assert not any("top_10000" in p for p in plan["upload_to_pod_inputs"] + plan["missing_local_files"])                     # o top_10000 antigo é substituído: não segue


def _outputs(tmp_path, auc, mae):
    o = tmp_path / "out"
    for d in ("models", "results", "parquet", "catalog"):
        (o / d).mkdir(parents=True, exist_ok=True)
    (o / "models" / "pass_model_A.txt").write_text("novo-pass")
    (o / "models" / "acc_pass_A.txt").write_text("novo-acc")
    (o / "results" / "pass_model_results.json").write_text(json.dumps({"results": {"A": {"all": {"auc": auc}}}}))
    (o / "results" / "acc_model_results.json").write_text(json.dumps({"results": {"model": {"mae": mae}}}))
    (o / "parquet" / "dump_scores_2026_10_01_random_10000.parquet").write_text("s")
    (o / "parquet" / "osu_user_beatmap_playcount_2026_10_01_random_10000.parquet").write_text("p")
    (o / "catalog" / "map_attributes.parquet").write_text("catalogo-novo")
    return o


def _trained_world(tmp_path):
    st = _settings(tmp_path)
    models = st.processed_dir / "recommend" / "models"
    models.mkdir(parents=True)
    (models / "pass_model_A.txt").write_text("velho-pass")
    (models / "acc_pass_A.txt").write_text("velho-acc")
    (models / "calibration_pass_acc.json").write_text(json.dumps({"pass": {"a": 1.0, "b": 1.0}, "acc_shift": -0.01}))
    (models / "training.json").write_text(json.dumps({"pass_model": {"auc": 0.79}, "acc_model": {"mae": 0.037}}))
    (st.processed_dir / "recommend" / "index").mkdir()
    (st.processed_dir / "recommend" / "index" / "meta.json").write_text("{}")
    cat = st.processed_dir / "catalog" / "v2"
    cat.mkdir(parents=True)
    (cat / "map_attributes.parquet").write_text("catalogo-velho")
    (st.processed_dir / "dump_tables" / "v1").mkdir(parents=True)
    (st.processed_dir / "dump_tables" / "v1" / "osu_user_beatmap_playcount_2026_09_01_top_10000.parquet").write_text("top-velho")
    (st.processed_dir / "dump_tables" / "v1" / "osu_user_beatmap_playcount_2026_09_01_random_10000.parquet").write_text("rnd")
    ctl = st.data_dir / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "retrain_pending.json").write_text(json.dumps({"snapshot": "2026_10_01"}))
    return st, models


def test_retrain_finish_refuses_a_worse_model_and_touches_nothing(tmp_path):
    st, models = _trained_world(tmp_path)
    store = Store(f"sqlite:///{tmp_path}/f.db", tmp_path / "raw")
    out = mt.retrain_finish(st, store, _outputs(tmp_path, auc=0.70, mae=0.037), "2026_10_01")
    assert out["installed"] is False and "AUC" in out["refused"]
    assert (models / "pass_model_A.txt").read_text() == "velho-pass" and (st.data_dir / "control" / "retrain_failed.json").exists()
    assert (st.data_dir / "control" / "retrain_pending.json").exists()  # continua pendente
    worse_mae = mt.retrain_finish(st, store, _outputs(tmp_path, auc=0.79, mae=0.06), "2026_10_01")
    assert "MAE" in worse_mae["refused"]


def test_retrain_finish_installs_keeps_the_old_folders_and_updates_the_state(tmp_path, monkeypatch):
    st, models = _trained_world(tmp_path)
    store = Store(f"sqlite:///{tmp_path}/f.db", tmp_path / "raw")
    built = {}

    def fake_build_index(inputs, pool, bundle, out_dir, **kw):
        built["inputs"] = sorted(p.name for p in Path(inputs).iterdir())
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "meta.json").write_text(json.dumps({"maps": 1}))
        return {"maps": 1}

    def fake_calibration(*a, **k):
        raise RuntimeError("poucos pares do lazer fora do tempo (12) para calibrar")  # 1.ª semana do mês

    monkeypatch.setattr("osuml.recommend.index.build_index", fake_build_index)
    monkeypatch.setattr("osuml.analysis.pass_calibration.run_pass_calibration", fake_calibration)
    out = mt.retrain_finish(st, store, _outputs(tmp_path, auc=0.795, mae=0.036), "2026_10_01")
    assert out["installed"] is True and out["calibration"]["refit"] is False and "poucos pares" in out["calibration"]["reason"]
    assert (models / "pass_model_A.txt").read_text() == "novo-pass" and json.loads((models / "training.json").read_text())["snapshot"] == "2026_10_01"
    assert json.loads((models / "calibration_pass_acc.json").read_text())["acc_shift"] == -0.01                             # calibração anterior mantida
    prev = list((st.processed_dir / "recommend").glob("models_prev_*"))
    assert len(prev) == 1 and (prev[0] / "pass_model_A.txt").read_text() == "velho-pass"                                     # nada foi apagado
    assert (st.processed_dir / "catalog" / "v2" / "map_attributes.parquet").read_text() == "catalogo-novo"
    assert list((st.processed_dir / "catalog" / "v2").glob("map_attributes_prev_*.parquet"))
    assert (st.processed_dir / "dump_scores" / "v1" / "dump_scores_2026_10_01_random_10000.parquet").exists()
    assert "osu_user_beatmap_playcount_2026_09_01_top_10000.parquet" not in built["inputs"] and "map_attributes.parquet" in built["inputs"]  # top antigo substituído
    state = mt.load_state(st)
    assert state["trained_snapshot"] == "2026_10_01" and state["random_snapshots"][-1] == "2026_10_01"
    assert not (st.data_dir / "control" / "retrain_pending.json").exists()
    assert mt.dumps_check(st, today=date(2026, 10, 5), fetch=lambda: LISTING + '<a href="2026_10_01_performance_osu_top_10000.tar.bz2">x</a><a href="2026_10_01_osu_files.tar.bz2">x</a>')["new"] is False


# ------------------------------------------------------------------ pipeline do pod
def _pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK", str(tmp_path / "work"))
    spec = importlib.util.spec_from_file_location("pod_pipeline_under_test", Path(__file__).resolve().parents[1] / "scripts" / "pod_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    for d in (mod.PROG, mod.LOGS, mod.DUMPS, mod.INPUTS, mod.DATA):
        d.mkdir(parents=True, exist_ok=True)
    return mod


def test_pod_pipeline_skips_downloads_already_imported_and_keeps_top_out_of_the_pass_inputs(tmp_path, monkeypatch):
    pp = _pipeline(tmp_path, monkeypatch)
    sc, tb = pp.DATA / "processed" / "dump_scores" / "v1", pp.DATA / "processed" / "dump_tables" / "v1"
    sc.mkdir(parents=True)
    tb.mkdir(parents=True)
    for stem in ("2026_10_01_random_10000", "2026_10_01_top_10000"):
        (sc / f"dump_scores_{stem}.parquet").write_text("s")
        (sc / f"manifest_dump_scores_{stem}.json").write_text("{}")
        (tb / f"osu_user_beatmap_playcount_{stem}.parquet").write_text("p")
        (tb / f"osu_user_beatmap_playcount_{stem}.progress.json").write_text(json.dumps({"status": "done"}))
    assert pp._is_imported("2026_10_01", "random_10000") and not pp._is_imported("2026_11_01", "random_10000")
    monkeypatch.setattr(pp, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("não devia descarregar")))
    pp.import_snapshot("2026_10_01", "random_10000")
    pp.import_snapshot("2026_10_01", "top_10000", 6, 24)
    assert (pp.INPUTS / "dump_scores_2026_10_01_random_10000.parquet").is_symlink() and (pp.INPUTS / "osu_user_beatmap_playcount_2026_10_01_top_10000.parquet").is_symlink()
    pp._prepare_inputs_pass()
    names = {f.name for f in pp.INPUTS_PASS.iterdir()}
    assert "dump_scores_2026_10_01_random_10000.parquet" in names and not any("top_10000" in n for n in names)
    assert pp.NEW_SNAPS == [("2026_10_01", "random_10000"), ("2026_10_01", "top_10000")]


def test_pod_pipeline_packs_exactly_what_the_local_finish_expects(tmp_path, monkeypatch):
    import tarfile

    pp = _pipeline(tmp_path, monkeypatch)
    pp.VERSION = "full"
    res = pp.WORK / "results"
    for rel in ("pass_model/full/pass_model_A.txt", "pass_model/full/results.json", "acc_model/full/acc_pass_A.txt", "acc_model/full/results.json"):
        (res / rel).parent.mkdir(parents=True, exist_ok=True)
        (res / rel).write_text("x")
    cat = pp.DATA / "processed" / "catalog" / "v2"
    cat.mkdir(parents=True)
    (cat / "map_attributes.parquet").write_text("c")
    pp.NEW_SNAPS[:] = [("2026_10_01", "random_10000")]
    with pytest.raises(FileNotFoundError):  # faltam os Parquet novos: não empacota em silêncio
        pp.stage_pack()
    for sub, name in (("dump_scores", "dump_scores_2026_10_01_random_10000.parquet"), ("dump_tables", "osu_user_beatmap_playcount_2026_10_01_random_10000.parquet")):
        d = pp.DATA / "processed" / sub / "v1"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("x")
    pp.stage_pack()
    with tarfile.open(pp.WORK / "retrain_outputs.tar") as tf:
        names = set(tf.getnames())
    assert {"models/pass_model_A.txt", "models/acc_pass_A.txt", "results/pass_model_results.json", "results/acc_model_results.json", "catalog/map_attributes.parquet",
            "parquet/dump_scores_2026_10_01_random_10000.parquet", "parquet/osu_user_beatmap_playcount_2026_10_01_random_10000.parquet"} <= names


def test_retrain_prepare_builds_the_source_zip_and_the_api_plays_input(tmp_path):
    import zipfile

    st = _settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path}/prep.db", tmp_path / "raw")
    out = mt.retrain_prepare(st, store)
    names = set(zipfile.ZipFile(out["files"]["osuml_src.zip"]).namelist())
    assert "pyproject.toml" in names and "src/osuml/maintenance.py" in names and not any("__pycache__" in n for n in names)
    assert all(Path(p).exists() for p in out["files"].values())
