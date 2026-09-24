"""Aquisição dos ficheiros .osu dos mapas referenciados nos scores.

Fontes, por ordem de preferência:

1. `dump`   — arquivo oficial do data.ppy.sh com todos os .osu ranked/loved
              (tar.bz2/tar.gz/zip) ou a pasta extraída. É a fonte recomendada
              pela própria documentação da API para dados em volume. O arquivo
              é lido em streaming: só os mapas pedidos são guardados.
2. `folder` — qualquer pasta com .osu (ex.: cópia da pasta Songs de outra
              máquina). Mesmo mecanismo.
3. `osu_web`— fallback opcional, um ficheiro de cada vez, via
              https://osu.ppy.sh/osu/{beatmap_id}. Esta rota NÃO faz parte da
              osu!API v2 documentada; usar só para os poucos mapas que o dump
              não cobre (unranked/graveyard) e sempre com rate limit.

Todas as fontes identificam o mapa pelo MD5 do ficheiro, comparado com o
`checksum` que a API devolveu para esse beatmap. Não há adivinhação por nome
de ficheiro, exceto como último recurso (ficheiros "<id>.osu" do dump), e
nesse caso `checksum_match` fica False se o MD5 não coincidir.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import httpx
from sqlalchemy import select

from ..api.http import ApiError, HttpClient
from ..api.rate_limit import MinIntervalLimiter
from ..storage import models as m
from ..storage.database import Store

log = logging.getLogger(__name__)

OSU_HEADER = b"osu file format v"
_ID_NAME = re.compile(r"^(\d+)\.osu$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Wanted:
    """Mapas em falta, indexados por checksum e por id."""

    by_md5: dict[str, int]
    by_id: dict[int, str | None]

    def __len__(self) -> int:
        return len(self.by_id)


@dataclass
class ImportStats:
    scanned: int = 0
    matched_md5: int = 0
    matched_id_only: int = 0
    already_present: int = 0
    not_found: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_scanned": self.scanned,
            "imported_by_md5": self.matched_md5,
            "imported_by_id_checksum_mismatch": self.matched_id_only,
            "still_missing": len(self.not_found),
            "missing_ids": self.not_found[:50],
        }


def wanted_beatmaps(store: Store, user_id: int | None = None) -> Wanted:
    """Mapas referenciados em scores que ainda não têm ficheiro guardado."""
    s, b, f = m.scores, m.beatmaps, m.beatmap_files
    q = (
        select(s.c.beatmap_id, b.c.checksum)
        .select_from(s.outerjoin(b, b.c.beatmap_id == s.c.beatmap_id))
        .where(s.c.beatmap_id.isnot(None))
        .where(s.c.beatmap_id.notin_(select(f.c.beatmap_id)))
        .distinct()
    )
    if user_id is not None:
        q = q.where(s.c.user_id == user_id)
    with store.engine.connect() as c:
        rows = c.execute(q).all()
    by_id = {int(r.beatmap_id): r.checksum for r in rows}
    by_md5 = {cs: bid for bid, cs in by_id.items() if cs}
    return Wanted(by_md5=by_md5, by_id=by_id)


def _save(store: Store, beatmap_id: int, data: bytes, source: str, origin: str, expected_md5: str | None) -> bool:
    md5 = hashlib.md5(data).hexdigest()
    rel = Path("osu_files") / f"{md5}.osu"
    path = store.raw.raw_dir / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    match = (md5 == expected_md5) if expected_md5 else None
    with store.engine.begin() as c:
        if c.execute(select(m.beatmap_files.c.beatmap_id).where(m.beatmap_files.c.beatmap_id == beatmap_id)).first():
            return False
        c.execute(m.beatmap_files.insert().values(
            beatmap_id=beatmap_id, md5=md5, rel_path=rel.as_posix(), bytes=len(data), source=source,
            origin=origin[:1024], checksum_match=match, imported_at=utcnow(),
        ))
    return True


def _iter_path(path: Path) -> Iterator[tuple[str, bytes]]:
    """(nome, conteúdo) de cada .osu numa pasta ou arquivo, em streaming."""
    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file() and p.suffix.lower() == ".osu":
                yield str(p), p.read_bytes()
        return
    name = path.name.lower()
    if name.endswith((".tar", ".tar.bz2", ".tbz2", ".tar.gz", ".tgz", ".tar.xz")):
        with tarfile.open(path, mode="r|*") as tar:  # streaming: não extrai tudo para disco
            for member in tar:
                if member.isfile() and member.name.lower().endswith(".osu"):
                    fh = tar.extractfile(member)
                    if fh is not None:
                        yield member.name, fh.read()
        return
    if name.endswith((".zip", ".osz")):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir() and info.filename.lower().endswith(".osu"):
                    yield info.filename, zf.read(info)
        return
    raise ValueError(f"formato não suportado: {path}")


def import_from_path(store: Store, path: Path, wanted: Wanted, source: str) -> ImportStats:
    stats = ImportStats()
    remaining = dict(wanted.by_id)
    for name, data in _iter_path(path):
        stats.scanned += 1
        if not remaining:
            break  # já tem tudo: não precisa de ler o resto do arquivo
        if not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER):
            continue
        md5 = hashlib.md5(data).hexdigest()
        bid = wanted.by_md5.get(md5)
        if bid is not None and bid in remaining:
            if _save(store, bid, data, source, f"{path}:{name}", remaining[bid]):
                stats.matched_md5 += 1
            del remaining[bid]
            continue
        # Último recurso: dumps nomeiam os ficheiros "<beatmap_id>.osu".
        mt = _ID_NAME.match(Path(name).name)
        if mt and int(mt.group(1)) in remaining:
            bid = int(mt.group(1))
            if _save(store, bid, data, source, f"{path}:{name}", remaining[bid]):
                stats.matched_id_only += 1
                log.warning("beatmap %s importado por nome: MD5 difere do checksum da API", bid)
            del remaining[bid]
    stats.not_found = sorted(remaining)
    return stats


class OsuWebFetcher:
    """Fallback opcional: https://osu.ppy.sh/osu/{id}. Rota fora da API documentada."""

    def __init__(self, *, user_agent: str, min_interval: float = 1.1, base_url: str = "https://osu.ppy.sh",
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] | None = None) -> None:
        kw = {"sleep": sleep} if sleep else {}
        self.http = HttpClient(base_url, MinIntervalLimiter(min_interval, **kw), user_agent,
                               name="osu_web", transport=transport, **kw)

    def close(self) -> None:
        self.http.close()

    def fetch_missing(self, store: Store, wanted: Wanted, limit: int | None = None) -> dict:
        ok, empty, failed, mismatch = 0, [], [], []
        for n, (bid, checksum) in enumerate(sorted(wanted.by_id.items())):
            if limit is not None and n >= limit:
                break
            try:
                res = self.http.request("GET", f"/osu/{bid}", authenticated=False)
            except ApiError as exc:
                failed.append({"beatmap_id": bid, "error": str(exc)})
                continue
            data = res.body
            if not data.lstrip(b"\xef\xbb\xbf").startswith(OSU_HEADER):
                empty.append(bid)  # mapa indisponível: resposta vazia
                continue
            if _save(store, bid, data, "osu_web", f"/osu/{bid}", checksum):
                ok += 1
                if checksum and hashlib.md5(data).hexdigest() != checksum:
                    mismatch.append(bid)
        return {"downloaded": ok, "unavailable": empty, "failed": failed, "checksum_mismatch": mismatch,
                "requests": self.http.stats.requests}


def files_report(store: Store, user_id: int | None = None) -> dict:
    s, f = m.scores, m.beatmap_files
    with store.engine.connect() as c:
        q = select(s.c.beatmap_id).where(s.c.beatmap_id.isnot(None)).distinct()
        if user_id is not None:
            q = q.where(s.c.user_id == user_id)
        ids = {r[0] for r in c.execute(q)}
        rows = c.execute(select(f.c.beatmap_id, f.c.source, f.c.checksum_match)).all()
        status = dict(c.execute(select(m.beatmaps.c.beatmap_id, m.beatmaps.c.status)
                                .where(m.beatmaps.c.beatmap_id.in_(ids))).all()) if ids else {}
    have = {r.beatmap_id: r for r in rows if r.beatmap_id in ids}
    missing_by_status: dict[str, int] = {}
    for b in ids - set(have):
        k = status.get(b) or "desconhecido"
        missing_by_status[k] = missing_by_status.get(k, 0) + 1
    by_source: dict[str, int] = {}
    for r in have.values():
        by_source[r.source] = by_source.get(r.source, 0) + 1
    return {
        "beatmaps_referenced": len(ids),
        "with_file": len(have),
        "missing": len(ids) - len(have),
        # ranked/approved/loved devem estar no dump do data.ppy.sh; os restantes não.
        "missing_by_status": missing_by_status,
        "by_source": by_source,
        "checksum_mismatch": sorted(b for b, r in have.items() if r.checksum_match is False),
    }
