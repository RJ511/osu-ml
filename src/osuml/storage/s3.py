"""Espelha `data/raw/` e `data/processed/` (e o dump oficial, se encontrado) para o bucket S3
`osu-ml-skill` (`eu-west-1` por omissão, ver `Settings.s3_bucket`/`s3_region`).

Só corre quando pedido explicitamente (`osuml sync-s3`) — **nunca** automático a seguir a um
collect/export/import, para não fazer pedidos de rede sem necessidade clara.

Credenciais **nunca** são lidas nem geridas por este módulo: o boto3 resolve-as sozinho a partir do
ambiente (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_PROFILE`, em `.env` ou variáveis de
ambiente reais) — nunca hardcoded, nunca em logs, nunca passadas explicitamente por este código.

Idempotente para ficheiros pequenos: salta os que já têm o mesmo MD5 no `ETag` do objeto em S3. Um
upload multipart (o `boto3.upload_file` usa-o automaticamente acima de ~8 MB, o que inclui o dump
`.tar.bz2`) tem um `ETag` que não é o MD5 direto — nesse caso o ficheiro é sempre reenviado, não há
deteção fiável de "já está lá" sem replicar o esquema de ETag multipart do S3. `--dry-run` nunca
contacta o S3 (nem para verificar o que já existe) — só lista o que seria tentado.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    uploaded: int = 0
    skipped: int = 0
    bytes_uploaded: int = 0
    failed: list[str] = field(default_factory=list)


def _md5_hex(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _needs_upload(client, bucket: str, key: str, local_md5: str) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey"):
            return True
        raise
    etag = head["ETag"].strip('"')
    return "-" in etag or etag != local_md5  # "-" = multipart, ETag não é MD5: reenvia por segurança


def _upload_one(client, bucket: str, key: str, path: Path, stats: SyncStats, dry_run: bool) -> None:
    if dry_run:
        stats.uploaded += 1
        return
    try:
        if not _needs_upload(client, bucket, key, _md5_hex(path)):
            stats.skipped += 1
            return
        client.upload_file(str(path), bucket, key)
        stats.uploaded += 1
        stats.bytes_uploaded += path.stat().st_size
    except Exception as exc:
        stats.failed.append(f"{key}: {exc}")


def sync_directory(client, bucket: str, local_dir: Path, key_prefix: str, dry_run: bool = False) -> SyncStats:
    stats = SyncStats()
    if not local_dir.exists():
        return stats
    for path in sorted(local_dir.rglob("*")):
        if path.is_file():
            key = f"{key_prefix}/{path.relative_to(local_dir).as_posix()}"
            _upload_one(client, bucket, key, path, stats, dry_run)
    return stats


def sync_all(raw_dir: Path, processed_dir: Path, dump_path: Path | None, bucket: str, region: str,
             dry_run: bool = False) -> dict:
    import boto3

    client = boto3.client("s3", region_name=region)
    out = {
        "bucket": bucket, "region": region, "dry_run": dry_run,
        "raw": vars(sync_directory(client, bucket, raw_dir, "data/raw", dry_run)),
        "processed": vars(sync_directory(client, bucket, processed_dir, "data/processed", dry_run)),
    }
    dump_stats = SyncStats()
    if dump_path and dump_path.is_file():
        _upload_one(client, bucket, f"dumps/{dump_path.name}", dump_path, dump_stats, dry_run)
    out["dump"] = vars(dump_stats)
    return out


def upload_pack(zip_path: Path, bucket: str, region: str, key: str | None = None, *, share_hours: float = 0,
                dry_run: bool = False, client=None) -> dict:
    """Envia UM ficheiro (o pacote de dados do recomendador) para o bucket privado, cifrado no servidor (AES256), e confirma o tamanho.

    Só corre quando pedido (`osuml recommend upload`). Credenciais: as do boto3 (ambiente/perfil), nunca lidas nem guardadas aqui.
    `share_hours > 0` devolve também um link temporário de descarga (URL pré-assinado); esse link dá acesso ao ficheiro a quem o tiver."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"não existe: {zip_path}")
    key = key or f"recommend/{zip_path.name}"
    out: dict = {"bucket": bucket, "region": region, "key": key, "bytes": zip_path.stat().st_size, "dry_run": dry_run}
    if dry_run:
        return out
    if client is None:
        import boto3

        if boto3.session.Session().get_credentials() is None:
            raise RuntimeError("sem credenciais AWS: configura AWS_PROFILE (aws configure) ou AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY no ambiente")
        client = boto3.client("s3", region_name=region)
    try:  # o bucket bloqueia acesso público? (precisa de s3:GetBucketPublicAccessBlock; se não houver permissão, fica "não verificado")
        cfg = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        out["public_access_blocked"] = all(cfg.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
    except Exception:
        out["public_access_blocked"] = None
    client.upload_file(str(zip_path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    head = client.head_object(Bucket=bucket, Key=key)
    out["uploaded_bytes"] = int(head["ContentLength"])
    if out["uploaded_bytes"] != out["bytes"]:
        raise RuntimeError(f"tamanho no S3 ({out['uploaded_bytes']}) difere do local ({out['bytes']})")
    if share_hours > 0:
        out["temporary_url"] = client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(share_hours * 3600))
        out["temporary_url_hours"] = share_hours
    return out
