"""Importador genérico de tabelas numéricas de dumps (.tar.bz2), em streaming."""

from __future__ import annotations

import io
import tarfile

import pyarrow.parquet as pq

from osuml.external.table_dump import import_numeric_table
from osuml.progress import Progress


def _tar(path):
    sql = ("-- dump\nCREATE TABLE `osu_user_beatmap_playcount` (\n  `user_id` int unsigned NOT NULL,\n"
           "  `beatmap_id` mediumint unsigned NOT NULL,\n  `playcount` mediumint unsigned NOT NULL,\n  PRIMARY KEY (`user_id`)\n) ENGINE=InnoDB;\n"
           "INSERT INTO `osu_user_beatmap_playcount` VALUES (1,10,3),(1,11,1),(2,10,7);\n"
           "INSERT INTO `osu_user_beatmap_playcount` VALUES (3,12,2);\nINSERT INTO `outra` VALUES (9,9,9);\n").encode()
    other = b"INSERT INTO `x` VALUES (1,1,1);\n"
    with tarfile.open(path, "w:bz2") as tf:
        for name, data in (("d/other.sql", other), ("d/osu_user_beatmap_playcount.sql", sql)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_import_numeric_table_reads_only_the_requested_table_and_reports_progress(tmp_path):
    tar = tmp_path / "d.tar.bz2"
    _tar(tar)
    prog = Progress(tmp_path / "p.json", "t", tar.stat().st_size, "bytes", min_interval=0)
    out = import_numeric_table(tar, "osu_user_beatmap_playcount", tmp_path / "o.parquet", prog)
    assert out["rows"] == 4 and out["columns"] == ["user_id", "beatmap_id", "playcount"]
    rows = pq.read_table(tmp_path / "o.parquet").to_pylist()
    assert sorted((r["user_id"], r["beatmap_id"], r["playcount"]) for r in rows) == [(1, 10, 3), (1, 11, 1), (2, 10, 7), (3, 12, 2)]
    import json

    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "done"
