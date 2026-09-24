"""Importador de scores do dump oficial: parsing de linhas reais (JSON escapado, apostrofos, NULLs), Parquet."""

from __future__ import annotations

import io
import tarfile

import pyarrow.parquet as pq

from osuml.external.scores_dump import import_scores, iter_scores, parse_line

ROW1 = ("(819417646,91594,0,824622,0,1,1,'A',1,0.950168,425,661736,'{\\\"mods\\\": [{\\\"acronym\\\": \\\"CL\\\"}], "
        "\\\"statistics\\\": {\\\"ok\\\": 20, \\\"meh\\\": 10, \\\"miss\\\": 3, \\\"great\\\": 462}, \\\"maximum_statistics\\\": "
        "{\\\"great\\\": 495, \\\"legacy_combo_increase\\\": 230}}',NULL,2904400123,3535942,NULL,'2019-09-28 23:44:25',1569714265,NULL)")
ROW2 = ("(900,7,0,55,1,0,1,'SH',1,0.99,700,900000,'{\\\"mods\\\": [{\\\"acronym\\\": \\\"DT\\\", \\\"settings\\\": {\\\"speed_change\\\": 1.3}}, "
        "{\\\"acronym\\\": \\\"HD\\\"}], \\\"statistics\\\": {\\\"great\\\": 600}, \\\"maximum_statistics\\\": {\\\"great\\\": 600}}',"
        "512.5,NULL,0,'2026-09-01 10:00:00','2026-09-01 10:03:00',1788000000,12)")
ROW_MANIA = ROW1.replace("(819417646,91594,0,", "(5,91594,3,")
ROW_QUOTE = ("(901,7,0,56,0,0,1,'B',1,0.9,10,1000,'{\\\"mods\\\": [], \\\"note\\\": \\\"it\\'s\\\", \\\"statistics\\\": {\\\"great\\\": 1}}',"
             "NULL,NULL,0,NULL,'2026-09-02 10:00:00',1788000001,NULL)")


def _sql() -> str:
    return ("-- MySQL dump\nCREATE TABLE `scores` (\n  `id` bigint\n) ENGINE=InnoDB;\n"
            f"INSERT INTO `scores` VALUES {ROW1},{ROW2},{ROW_MANIA},{ROW_QUOTE};\nINSERT INTO `other` VALUES (1);\n")


def _tar(path):
    data = _sql().encode()
    with tarfile.open(path, "w:bz2") as tf:
        info = tarfile.TarInfo("2026_09_01_performance_osu_random_10000/scores.sql")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def test_parse_line_handles_legacy_lazer_quotes_and_skips_other_rulesets():
    rows = list(parse_line(_sql()))
    assert [r["score_id"] for r in rows] == [819417646, 900, 901]  # mania (ruleset 3) fora
    legacy, lazer, quoted = rows
    assert legacy["mods"] == "CL" and legacy["mods_effective"] == "" and legacy["is_legacy"] is True
    assert legacy["n_great"] == 462 and legacy["n_miss"] == 3 and legacy["n_max_great"] == 495 and legacy["pp"] is None
    assert lazer["mods"] == "DT,HD" and lazer["speed_change"] == 1.3 and lazer["is_legacy"] is False
    assert lazer["pp"] == 512.5 and lazer["started_at"].year == 2026 and lazer["accuracy"] == 0.99 and lazer["max_combo"] == 700
    assert quoted["mods"] == "" and quoted["started_at"] is None and quoted["n_great"] == 1


def test_import_scores_writes_parquet_and_manifest_from_tar(tmp_path):
    tar = tmp_path / "2026_09_01_performance_osu_random_10000.tar.bz2"
    _tar(tar)
    assert len(list(iter_scores(tar))) == 3
    man = import_scores(tar, tmp_path / "out", "v1")
    assert man["rows"] == 3 and man["users"] == 2 and man["failed_rows"] == 0 and man["duplicate_score_ids"] == 0
    assert man["rows_by_year"] == {2019: 1, 2026: 2}
    t = pq.read_table(tmp_path / "out" / "v1" / "dump_scores_2026_09_01_random_10000.parquet").to_pylist()
    assert {r["score_id"] for r in t} == {819417646, 900, 901} and all(r["passed"] for r in t)
    assert (tmp_path / "out" / "v1" / "manifest_dump_scores_2026_09_01_random_10000.json").exists()


def test_parallel_import_gives_the_same_rows_and_manifest_as_the_serial_one(tmp_path):
    tar = tmp_path / "2026_09_01_performance_osu_random_10000.tar.bz2"
    _tar(tar)
    serial = import_scores(tar, tmp_path / "a", "v1")
    par = import_scores(tar, tmp_path / "b", "v1", workers=2)
    for k in ("rows", "users", "passed_rows", "failed_rows", "duplicate_score_ids", "rows_by_year"):
        assert par[k] == serial[k], k
    fa = pq.read_table(tmp_path / "a" / "v1" / "dump_scores_2026_09_01_random_10000.parquet").to_pylist()
    fb = pq.read_table(tmp_path / "b" / "v1" / "dump_scores_2026_09_01_random_10000.parquet").to_pylist()
    key = lambda r: r["score_id"]  # noqa: E731
    assert sorted(fa, key=key) == sorted(fb, key=key)
