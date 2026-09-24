"""Parser .osu e aquisição de ficheiros (sem rede)."""

from __future__ import annotations

import hashlib
import io
import tarfile

import httpx
import pyarrow.parquet as pq

from osuml.beatmaps.acquire import OsuWebFetcher, files_report, import_from_path, wanted_beatmaps
from osuml.beatmaps.export import export_beatmaps
from osuml.beatmaps.parser import parse_osu
from osuml.storage.database import Store


def osu_text(title: str = "Song", extra: str = "") -> str:
    return f"""\ufeffosu file format v14

[General]
AudioFilename: audio.mp3
Mode: 0

[Metadata]
Title:{title}
Version:Insane

[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:8
ApproachRate:9
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,500,4,2,0,60,1,0
1000,-50,4,2,0,60,0,1

[HitObjects]
256,192,500,5,0,0:0:0:0:
100,100,1500,2,0,B|200:100|300:150,2,140,0|0|0
256,192,3000,12,0,4000,0:0:0:0:
{extra}"""


def test_parser_slider_end_time_and_types():
    pb = parse_osu(osu_text())
    assert pb.format_version == 14 and pb.mode == 0
    c, sl, sp = pb.hit_objects
    assert c.kind == "circle" and c.new_combo
    assert sl.kind == "slider" and sl.curve_type == "B" and sl.curve_points == [(200, 100), (300, 150)]
    # 140 / (1.4 * 100 * SV 2.0) * 500 ms = 250 ms por passagem, 2 passagens
    assert sl.sv == 2.0 and sl.beat_length == 500 and sl.end_time == 2000
    assert sp.kind == "spinner" and sp.end_time == 4000 and sp.new_combo
    s = pb.summary()
    assert s["ar"] == 9 and s["n_sliders"] == 1 and s["last_object_end_ms"] == 4000
    assert pb.timing_points[1].kiai and not pb.warnings


def test_parser_old_format_defaults():
    txt = "osu file format v3\n[Difficulty]\nOverallDifficulty:6\n[TimingPoints]\n0,400\n[HitObjects]\n1,2,10,1,0\n"
    pb = parse_osu(txt)
    assert pb.summary()["ar"] == 6  # AR ausente = OD
    assert pb.timing_points[0].uninherited


def _store_with_maps(tmp_path, texts: dict[int, str]) -> Store:
    store = Store(f"sqlite:///{tmp_path}/b.db", tmp_path / "raw")
    objs = []
    for i, (bid, txt) in enumerate(texts.items()):
        objs.append({
            "id": 1000 + i, "user_id": 7, "beatmap_id": bid, "passed": True, "ended_at": "2026-09-22T20:00:00Z",
            "beatmap": {"id": bid, "beatmapset_id": bid, "status": "ranked" if bid != 3 else "graveyard",
                        "checksum": hashlib.md5(txt.encode()).hexdigest()},
        })
    rid = store.record_request("run", "osu", _fake_result())
    store.ingest_scores(objs, "best", rid)
    return store


def _fake_result():
    from osuml.api.http import HttpResult

    return HttpResult("GET", "u", "/p", {}, 200, b"[]", 1, 1)


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, txt in files.items():
            data = txt.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_import_from_dump_by_md5_and_id_fallback(tmp_path):
    texts = {1: osu_text("A"), 2: osu_text("B"), 3: osu_text("C")}
    store = _store_with_maps(tmp_path, texts)
    dump = tmp_path / "osu_files.tar.bz2"
    dump.write_bytes(_tar({
        "x/random_name.osu": texts[1],          # encontrado por MD5 apesar do nome
        "2.osu": osu_text("B-versão-antiga"),   # só o nome coincide -> checksum_match False
        "999.osu": osu_text("irrelevante"),
    }))
    wanted = wanted_beatmaps(store)
    assert len(wanted) == 3
    stats = import_from_path(store, dump, wanted, "dump")
    assert stats.matched_md5 == 1 and stats.matched_id_only == 1 and stats.not_found == [3]
    rep = files_report(store)
    assert rep["with_file"] == 2 and rep["checksum_mismatch"] == [2]
    assert rep["missing_by_status"] == {"graveyard": 1}
    # segunda importação não duplica nem volta a pedir o que já existe
    assert len(wanted_beatmaps(store)) == 1


def test_fetch_fallback_verifies_and_skips_unavailable(tmp_path):
    texts = {10: osu_text("X"), 11: osu_text("Y")}
    store = _store_with_maps(tmp_path, texts)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/osu/10":
            return httpx.Response(200, content=texts[10].encode())
        return httpx.Response(200, content=b"")  # mapa indisponível

    f = OsuWebFetcher(user_agent="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    out = f.fetch_missing(store, wanted_beatmaps(store))
    assert out["downloaded"] == 1 and out["unavailable"] == [11] and not out["checksum_mismatch"]
    assert "Authorization" not in str(seen)


def test_export_beatmaps_parquet(tmp_path):
    texts = {1: osu_text("A")}
    store = _store_with_maps(tmp_path, texts)
    folder = tmp_path / "songs"
    folder.mkdir()
    (folder / "a.osu").write_bytes(texts[1].encode("utf-8"))
    import_from_path(store, folder, wanted_beatmaps(store), "folder")
    man = export_beatmaps(store, 7, tmp_path / "processed", "v0.2")
    objs = pq.read_table(tmp_path / "processed" / "v0.2" / "hitobjects_7.parquet").to_pylist()
    assert man["files"]["beatmaps_7.parquet"]["rows"] == 1 and len(objs) == 3
    assert objs[1]["kind"] == "slider" and objs[1]["end_time"] == 2000
