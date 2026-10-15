import gzip
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pyarrow.parquet as pq

from osuml import maintenance as mt
from osuml.mirror import client as mc
from osuml.storage.database import Store


def _store(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    return Store(f"sqlite:///{tmp_path / 'data' / 't.db'}", tmp_path / "data" / "raw")


class Rankings:
    """Mirror falso: guard verde + páginas de ranking (PT tem 120 jogadores)."""

    def __init__(self, total=120):
        self.total, self.calls = total, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/guard"):
            return httpx.Response(200, json={"pressure_active": False, "pain_last_60s": 0, "thresholds": {"pain_60s": 5, "retry_after_s": 120}})
        import json

        page = json.loads(req.url.params["cursor"])["page"]
        self.calls.append((page, req.url.params.get("country")))
        lo = (page - 1) * 50
        n = max(0, min(50, self.total - lo))
        rows = [{"id": 1000 + lo + i, "username": f"u{lo + i}", "pp": 9000.0 - (lo + i), "global_rank": 5000 + lo + i, "play_count": 10, "play_time": 99, "accuracy": 98.1, "country_code": "PT"} for i in range(n)]
        return httpx.Response(200, json={"data": {"cursor": {"page": page + 1}, "ranking": rows, "total": 10000}, "success": True})


def _client(store, fake):
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]

    return mc.MirrorClient(store, transport=httpx.MockTransport(fake), sleep=lambda s: None, clock=clock)


def test_ranking_snapshot_paginates_until_the_last_page_writes_parquet_and_is_idempotent(tmp_path):
    st, fake = _store(tmp_path), Rankings(120)
    out_dir = tmp_path / "rk"
    out = mc.snapshot_rankings(_client(st, fake), mc.Usage(tmp_path / "u.json"), out_dir, "PT", day="2026-10-15")
    assert out["complete"] and out["rows"] == 120 and out["pages"] == 3 and [p for p, _c in fake.calls] == [1, 2, 3] and fake.calls[0][1] == "PT"
    t = pq.read_table(out_dir / "PT_2026-10-15.parquet").to_pylist()
    assert len(t) == 120 and t[0]["rank"] == 1 and t[-1]["rank"] == 120 and t[0]["scope"] == "PT" and t[50]["user_id"] == 1050 and t[0]["play_time"] == 99
    again = mc.snapshot_rankings(_client(st, Rankings()), mc.Usage(tmp_path / "u2.json"), out_dir, "PT", day="2026-10-15")
    assert again["complete"] and again.get("skipped") and again["pages"] == 0  # o do dia já existe: 0 pedidos
    g = Rankings(10_000)
    glob = mc.snapshot_rankings(_client(st, g), mc.Usage(tmp_path / "u3.json"), out_dir, "global", day="2026-10-15", max_pages=4)
    assert glob["pages"] == 4 and glob["rows"] == 200 and g.calls[0][1] is None  # sem país no global; respeita o limite de páginas


def test_ranking_snapshot_stops_at_the_daily_cap_keeps_the_partial_and_resumes_next_time(tmp_path, monkeypatch):
    st = _store(tmp_path)
    out_dir = tmp_path / "rk"
    monkeypatch.setitem(mc.DAILY_CAPS, "rankings", 2)
    first = mc.snapshot_rankings(_client(st, Rankings(120)), mc.Usage(tmp_path / "u.json"), out_dir, "PT", day="2026-10-15")
    assert not first["complete"] and "teto diário" in first["aborted"] and first["rows"] == 100 and (out_dir / "PT_2026-10-15.partial.json.gz").exists()
    fake = Rankings(120)
    monkeypatch.setitem(mc.DAILY_CAPS, "rankings", 450)
    second = mc.snapshot_rankings(_client(st, fake), mc.Usage(tmp_path / "u2.json"), out_dir, "PT", day="2026-10-15")
    assert second["complete"] and [p for p, _c in fake.calls] == [3] and not (out_dir / "PT_2026-10-15.partial.json.gz").exists()  # retoma na página 3
    assert len(pq.read_table(out_dir / "PT_2026-10-15.parquet")) == 120


def test_db_backup_makes_a_consistent_compressed_copy_with_control_files_and_prunes_weekly_only(tmp_path):
    st = _store(tmp_path)
    with st.engine.begin() as c:
        c.exec_driver_sql("create table if not exists nota (x text)")
        c.exec_driver_sql("insert into nota values ('olá')")
    settings = SimpleNamespace(data_dir=tmp_path / "data", processed_dir=tmp_path / "data" / "processed", s3_bucket="b", s3_region="r")
    ctl = tmp_path / "data" / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "goal.json").write_text('{"x": 1}', encoding="utf-8")
    (ctl / "api.lock").write_text("lock", encoding="utf-8")
    sent: dict[str, bytes] = {}
    pruned: list[tuple[str, int]] = []

    def uploader(path, key):
        sent[key] = Path(path).read_bytes()
        return {"ok": True}

    when = datetime(2026, 10, 18, 4, 30, tzinfo=timezone.utc)
    out = mt.db_backup(settings, st, "weekly", now=when, uploader=uploader, pruner=lambda p, k: pruned.append((p, k)) or ["velho"])
    assert out["uploaded"] and out["pruned"] == ["velho"] and pruned == [("backups/db/weekly/", 8)]
    assert set(sent) == {"backups/db/weekly/osuml-weekly-20261018-0430.db.gz", "backups/db/weekly/control-weekly-20261018-0430.tar.gz"}
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(sent["backups/db/weekly/osuml-weekly-20261018-0430.db.gz"]))
    assert sqlite3.connect(restored).execute("select x from nota").fetchone() == ("olá",)  # a cópia abre e tem os dados
    ctl_tar = tmp_path / "ctl.tgz"
    ctl_tar.write_bytes(sent["backups/db/weekly/control-weekly-20261018-0430.tar.gz"])
    names = tarfile.open(ctl_tar).getnames()
    assert "control/goal.json" in names and not any(n.endswith(".lock") for n in names)
    assert not list((tmp_path / "data" / "backups").glob("*"))  # cópias locais temporárias apagadas depois do envio
    assert mt.load_state(settings)["db_backup"]["weekly"]["key"].endswith(".db.gz")
    pruned.clear()
    out2 = mt.db_backup(settings, st, "retrain-2026_10_01", now=when, uploader=uploader, pruner=lambda p, k: pruned.append((p, k)) or [])
    assert out2["uploaded"] and pruned == [] and "backups/db/retrain-2026_10_01/" in out2["keys"][0]  # as do retreino nunca se apagam


def test_db_backup_keeps_the_local_copy_when_the_upload_fails(tmp_path):
    st = _store(tmp_path)
    settings = SimpleNamespace(data_dir=tmp_path / "data", processed_dir=tmp_path / "data" / "processed", s3_bucket="b", s3_region="r")

    def boom(path, key):
        raise RuntimeError("sem rede")

    out = mt.db_backup(settings, st, "weekly", uploader=boom)
    assert out["uploaded"] is False and "sem rede" in out["error"] and list((tmp_path / "data" / "backups").glob("osuml-weekly-*.db.gz"))
