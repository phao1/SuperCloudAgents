"""Bounded multipart ingestion and streaming adapters for the shipped stores."""

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, BinaryIO
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.http import parse_options_header
from werkzeug.sansio.multipart import Data, Epilogue, File, MultipartDecoder, NeedData

from sca.api.authz import authorize
from sca.config import get_settings
from sca.core.interfaces import ObjectStore
from sca.core.models import Principal
from sca.storage.local import LocalObjectStore
from sca.storage.s3 import S3ObjectStore

router = APIRouter(prefix="/v1/files")


async def put_file(objects: ObjectStore, key: str, source: BinaryIO) -> str:
    def write() -> str:
        source.seek(0)
        if isinstance(objects, LocalObjectStore):
            path = objects._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
            return path.as_uri()
        if isinstance(objects, S3ObjectStore):
            from boto3.s3.transfer import TransferConfig

            objects._ensure_bucket()
            objects.client.upload_fileobj(
                source,
                objects.bucket,
                key,
                Config=TransferConfig(max_concurrency=1, use_threads=False),
            )
            return f"s3://{objects.bucket}/{key}"
        raise RuntimeError("Object store needs a streaming upload adapter")

    return await asyncio.to_thread(write)


@router.post("", status_code=201)
async def upload(
    request: Request,
    user: Annotated[Principal, Depends(authorize("upload"))],
) -> dict:
    media, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get("boundary", "")
    if media != "multipart/form-data" or not boundary or len(boundary) > 200:
        raise HTTPException(422, "Expected multipart/form-data")
    limit = get_settings().max_upload_mb * 1024 * 1024
    size, parts, complete = 0, 0, False
    name = ""
    digest = hashlib.sha256()
    parser = MultipartDecoder(boundary.encode(), max_form_memory_size=2 * 1024 * 1024, max_parts=1)
    with tempfile.TemporaryFile() as source:

        def consume(chunk: bytes | None) -> None:
            nonlocal size, parts, complete, name
            parser.receive_data(chunk)
            while True:
                event = parser.next_event()
                if isinstance(event, NeedData):
                    return
                if isinstance(event, Epilogue):
                    complete = True
                    return
                if isinstance(event, File):
                    parts += 1
                    if parts != 1 or event.name != "file":
                        raise HTTPException(422, "Expected exactly one file field")
                    name = event.filename.replace("\\", "/").rsplit("/", 1)[-1]
                    if not name or name in {".", ".."} or len(name) > 255 or "\x00" in name:
                        raise HTTPException(422, "Invalid filename")
                elif isinstance(event, Data):
                    if not name:
                        raise HTTPException(422, "Expected file field")
                    size += len(event.data)
                    if size > limit:
                        raise HTTPException(413, "File exceeds upload limit")
                    digest.update(event.data)
                    source.write(event.data)

        received = 0
        try:
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit + 65536:
                    raise HTTPException(413, "Upload exceeds limit")
                for offset in range(0, len(chunk), 65536):
                    await asyncio.to_thread(consume, chunk[offset : offset + 65536])
            await asyncio.to_thread(consume, None)
        except RequestEntityTooLarge as exc:
            raise HTTPException(413, "Multipart upload exceeds limits") from exc
        except ValueError as exc:
            raise HTTPException(422, "Malformed multipart upload") from exc
        if not complete or not name:
            raise HTTPException(422, "Incomplete multipart upload")
        file_id = uuid4()
        key = f"tenants/{user.tenant_id}/files/{file_id}"
        objects = request.app.state.object_store
        await put_file(objects, key, source)
        try:
            return await request.app.state.store.for_tenant(user.tenant_id).create_file(
                file_id,
                name,
                size,
                digest.hexdigest(),
            )
        except BaseException:
            await objects.delete(key)
            raise


@router.get("/{file_id}")
async def metadata(
    file_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read"))],
) -> dict:
    return await request.app.state.store.for_tenant(user.tenant_id).get_file(file_id)
