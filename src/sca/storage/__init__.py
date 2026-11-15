"""Select the object store without coupling callers to its implementation."""

from pathlib import Path

from sca.core.interfaces import ObjectStore
from sca.storage.local import LocalObjectStore
from sca.storage.s3 import S3ObjectStore


def make_object_store(settings: object) -> ObjectStore:
    backend = getattr(settings, "object_store", "local")
    if backend == "local":
        objects_dir = getattr(settings, "objects_dir", None)
        root = (
            objects_dir()
            if objects_dir
            else Path(getattr(settings, "data_dir", ".data")) / "objects"
        )
        return LocalObjectStore(root)
    if backend == "s3":
        return S3ObjectStore(
            getattr(settings, "s3_bucket", "sca"),
            endpoint_url=getattr(settings, "s3_endpoint_url", None),
            access_key=getattr(settings, "s3_access_key", None),
            secret_key=getattr(settings, "s3_secret_key", None),
            region=getattr(settings, "s3_region", "us-east-1"),
        )
    raise ValueError(f"Unknown object store: {backend}")
