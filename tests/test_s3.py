"""Espelho para S3 (osuml sync-s3) — sem rede: usa um cliente falso em vez do boto3."""

from __future__ import annotations

from osuml.storage.s3 import _md5_hex, _needs_upload, sync_directory


class _FakeClientError(Exception):
    def __init__(self, response):
        super().__init__(response)
        self.response = response


class FakeS3Client:
    """`existing`: key -> ETag (sem aspas). Um ETag com "-" simula um upload multipart."""

    def __init__(self, existing: dict[str, str] | None = None):
        self.existing = dict(existing or {})
        self.uploaded: list[str] = []

    def head_object(self, Bucket, Key):
        if Key not in self.existing:
            raise _FakeClientError({"Error": {"Code": "404"}})
        return {"ETag": f'"{self.existing[Key]}"'}

    def upload_file(self, Filename, Bucket, Key):
        from pathlib import Path

        self.uploaded.append(Key)
        self.existing[Key] = _md5_hex(Path(Filename))


class _ExplodingClient:
    """Rebenta se `dry_run` alguma vez lhe chamar algo — dry-run não pode tocar na rede."""

    def head_object(self, *a, **kw):
        raise AssertionError("dry-run não devia chamar head_object")

    def upload_file(self, *a, **kw):
        raise AssertionError("dry-run não devia chamar upload_file")


def test_needs_upload_true_when_key_missing():
    assert _needs_upload(FakeS3Client(), "b", "k", "abc123") is True


def test_needs_upload_false_when_md5_matches():
    client = FakeS3Client({"k": "abc123"})
    assert _needs_upload(client, "b", "k", "abc123") is False


def test_needs_upload_true_when_md5_differs():
    client = FakeS3Client({"k": "abc123"})
    assert _needs_upload(client, "b", "k", "different") is True


def test_needs_upload_true_when_etag_is_multipart():
    client = FakeS3Client({"k": "abc123-2"})
    assert _needs_upload(client, "b", "k", "abc123-2") is True


def test_sync_directory_uploads_new_then_skips_unchanged(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    (d / "a.osu").write_bytes(b"hello")
    (d / "sub").mkdir()
    (d / "sub" / "b.osu").write_bytes(b"world")

    client = FakeS3Client()
    stats1 = sync_directory(client, "bucket", d, "data/raw")
    assert stats1.uploaded == 2 and stats1.skipped == 0 and not stats1.failed
    assert set(client.uploaded) == {"data/raw/a.osu", "data/raw/sub/b.osu"}

    stats2 = sync_directory(client, "bucket", d, "data/raw")
    assert stats2.uploaded == 0 and stats2.skipped == 2


def test_sync_directory_dry_run_never_touches_client(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    (d / "a.osu").write_bytes(b"hello")

    stats = sync_directory(_ExplodingClient(), "bucket", d, "data/raw", dry_run=True)
    assert stats.uploaded == 1 and stats.skipped == 0


def test_sync_directory_missing_dir_returns_empty_stats(tmp_path):
    stats = sync_directory(FakeS3Client(), "bucket", tmp_path / "nao-existe", "data/raw")
    assert stats.uploaded == 0 and stats.skipped == 0 and not stats.failed
