"""S3-compatible object storage; SDK I/O stays off the event loop."""

import asyncio
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sca.core.errors import NotFoundError
from sca.storage.local import validate_key


class S3ObjectStore:
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )
        self._ready = False
        self._lock = threading.Lock()

    def _ensure_bucket(self) -> None:
        with self._lock:
            if self._ready:
                return
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                options = {}
                if self.region != "us-east-1":
                    options["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
                try:
                    self.client.create_bucket(Bucket=self.bucket, **options)
                except ClientError as create_exc:
                    if create_exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                        raise
            self._ready = True

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        validate_key(key)

        def write() -> None:
            self._ensure_bucket()
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

        await asyncio.to_thread(write)
        return f"s3://{self.bucket}/{key}"

    async def get(self, key: str) -> bytes:
        validate_key(key)

        def read() -> bytes:
            self._ensure_bucket()
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
                    raise NotFoundError("Object not found") from None
                raise
            with response["Body"] as body:
                return body.read()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        validate_key(key)

        def remove() -> None:
            self._ensure_bucket()
            self.client.delete_object(Bucket=self.bucket, Key=key)

        await asyncio.to_thread(remove)

    async def list(self, prefix: str = "") -> list[str]:
        if prefix:
            validate_key(prefix)

        def listing() -> list[str]:
            self._ensure_bucket()
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            )
            return sorted(obj["Key"] for page in pages for obj in page.get("Contents", []))

        return await asyncio.to_thread(listing)
