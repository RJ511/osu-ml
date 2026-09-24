"""Leitor de dumps SQL do data.ppy.sh (sem rede, sem MySQL)."""

from __future__ import annotations

import io
import tarfile

from osuml.external.sqldump import columns, extract_members, iter_table, parse_values

CREATE = """CREATE TABLE `t` (
  `user_id` int NOT NULL,
  `name` varchar(20) DEFAULT NULL,
  `score` float NOT NULL,
  PRIMARY KEY (`user_id`),
  KEY `name` (`name`)
) ENGINE=InnoDB;
"""


def test_parse_values_handles_strings_escapes_null_and_numbers():
    text = r"(1,'a,b',NULL,-2.5),(2,'it''s (ok)',3,1e3),(3,'x\'y\n',0,7);"
    assert list(parse_values(text)) == [
        (1, "a,b", None, -2.5),
        (2, "it's (ok)", 3, 1000.0),
        (3, "x'y\n", 0, 7),
    ]


def test_columns_ignores_key_lines():
    assert columns(CREATE) == ["user_id", "name", "score"]


def test_iter_table_yields_dicts(tmp_path):
    p = tmp_path / "t.sql"
    p.write_text(CREATE + "INSERT INTO `t` VALUES (1,'ana',0.5),(2,NULL,9);\nINSERT INTO `t` VALUES (3,'bo',1);\n",
                 encoding="utf-8")
    rows = list(iter_table(p))
    assert rows == [
        {"user_id": 1, "name": "ana", "score": 0.5},
        {"user_id": 2, "name": None, "score": 9},
        {"user_id": 3, "name": "bo", "score": 1},
    ]


def test_extract_members_only_requested(tmp_path):
    tar_path = tmp_path / "pack.tar.bz2"
    with tarfile.open(tar_path, "w:bz2") as tar:
        for name, body in (("pack/big.sql", b"x" * 10), ("pack/small.sql", b"hello")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    found = extract_members(tar_path, {"small.sql"}, tmp_path / "out")
    assert list(found) == ["small.sql"] and found["small.sql"].read_bytes() == b"hello"
