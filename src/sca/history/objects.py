"""Stream published history objects through the configured storage adapters."""

import gzip
import io
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from zipfile import ZipFile

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from sca.core.errors import NotFoundError
from sca.history.bundle import (
    BLOCK_SIZE,
    BundleWriter,
    DigestWriter,
    HistoryError,
    Member,
    check_digest,
)
from sca.history.privacy import copy_text
from sca.storage.local import LocalObjectStore
from sca.storage.s3 import S3ObjectStore

Storage = LocalObjectStore | S3ObjectStore
_TEXT_EXTENSIONS = {".log", ".txt", ".md", ".json", ".jsonl", ".csv", ".diff", ".patch", ".svg"}
_CREDENTIAL_FILES = {"auth.json", "credentials", ".npmrc", ".netrc", "id_rsa", "id_ed25519"}


def _s3_object(
    store: S3ObjectStore, key: str, *, metadata_only: bool = False, allow_missing_bucket: bool = False,
) -> dict:
    request = store.client.head_object if metadata_only else store.client.get_object
    try:
        return request(Bucket=store.bucket, Key=key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"NoSuchKey", "404", "NotFound"} or (allow_missing_bucket and code == "NoSuchBucket"):
            raise NotFoundError("History object not found") from None
        raise


@contextmanager
def read_object(
    store: Storage, key: str, *, allow_missing_bucket: bool = False,
) -> Iterator[tuple[BinaryIO, int]]:
    if isinstance(store, LocalObjectStore):
        try:
            source = store._path(key).open("rb")
        except FileNotFoundError:
            raise NotFoundError("History object not found") from None
        with source:
            yield source, os.fstat(source.fileno()).st_size
        return

    response = _s3_object(store, key, allow_missing_bucket=allow_missing_bucket)
    with io.BufferedReader(response["Body"]) as source:
        yield source, response["ContentLength"]


def copy_artifact(
    store: Storage, key: str, name: str, media_type: str, size: int, bundle: BundleWriter, member: str,
) -> None:
    filename = PurePosixPath(name.replace("\\", "/")).name.lower()
    if filename in _CREDENTIAL_FILES or filename == ".env" or filename.startswith(".env."):
        if isinstance(store, LocalObjectStore):
            if not store._path(key).is_file():
                raise NotFoundError("History object not found")
        else:
            _s3_object(store, key, metadata_only=True)
        with bundle.open(member) as target:
            target.write(b"[redacted credential file]\n")
        return

    extension = PurePosixPath(filename).suffix
    with read_object(store, key) as (source, length):
        if length != size:
            raise HistoryError("A published artifact changed during export. Retry the export.")
        with bundle.open(member) as target:
            if extension == ".gz" and PurePosixPath(filename[:-3]).suffix in _TEXT_EXTENSIONS:
                with (
                    gzip.GzipFile(fileobj=source) as compressed,
                    gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as output,
                ):
                    copy_text(compressed, output)
            elif (media_type.startswith("text/") or media_type.split(";")[0].endswith(("json", "+xml"))
                  or extension in _TEXT_EXTENSIONS or _looks_like_text(source.peek(4096)[:4096])):
                copy_text(source, target, patch=extension in {".diff", ".patch"})
            else:
                shutil.copyfileobj(source, target, BLOCK_SIZE)


def _looks_like_text(sample: bytes) -> bool:
    return b"\x00" not in sample and all(byte >= 32 or byte in b"\t\n\r" for byte in sample)


def object_uri(store: Storage, key: str) -> str:
    if isinstance(store, LocalObjectStore):
        return store._path(key).as_uri()
    return f"s3://{store.bucket}/{key}"


def write_object(
    store: Storage, key: str, zip_file: ZipFile, name: str, member: Member, media_type: str,
) -> bool:
    """Publish verified bytes. A previous attempt's object can be reused."""
    try:
        with read_object(store, key, allow_missing_bucket=True) as (existing, _):
            check_digest(existing, member)
            return False
    except NotFoundError:
        pass

    if isinstance(store, LocalObjectStore):
        path = store._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                _copy_verified(zip_file, name, member, output)
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
    else:
        store._ensure_bucket()
        with tempfile.TemporaryFile() as output:
            _copy_verified(zip_file, name, member, output)
            output.seek(0)
            store.client.upload_fileobj(
                output, store.bucket, key, ExtraArgs={"ContentType": media_type},
                Config=TransferConfig(use_threads=False, max_concurrency=1),
            )
    return True


def _copy_verified(zip_file: ZipFile, name: str, member: Member, output: BinaryIO) -> None:
    writer = DigestWriter(output)
    with zip_file.open(name) as source:
        shutil.copyfileobj(source, writer, BLOCK_SIZE)
    if writer.member() != member:
        raise HistoryError("History object checksum mismatch")
