from pathlib import PureWindowsPath
from types import SimpleNamespace
from uuid import uuid4

import pytest
from moto.server import ThreadedMotoServer

from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.interfaces import ObjectStore
from sca.storage import local as local_store
from sca.storage import make_object_store
from sca.storage.local import LocalObjectStore
from sca.storage.s3 import S3ObjectStore


@pytest.fixture(scope="module")
def s3_endpoint():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


@pytest.fixture(params=["local", "s3"])
def objects(request, tmp_path, s3_endpoint):
    if request.param == "local":
        return LocalObjectStore(tmp_path)
    return S3ObjectStore(
        "contract-" + uuid4().hex,
        endpoint_url=s3_endpoint,
        access_key="testing",
        secret_key="testing",
    )


@pytest.mark.parametrize("payload", [b"hello", bytes(range(256)), b""])
async def test_round_trip(objects, payload):
    assert isinstance(objects, ObjectStore)
    uri = await objects.put("nested/data", payload)
    expected = (
        f"s3://{objects.bucket}/nested/data"
        if isinstance(objects, S3ObjectStore)
        else (objects.root / "nested/data").as_uri()
    )
    assert uri == expected
    assert await objects.get("nested/data") == payload
    await objects.put("nested/data", b"replacement", "text/plain")
    assert await objects.get("nested/data") == b"replacement"


async def test_missing_list_delete(objects):
    with pytest.raises(NotFoundError):
        await objects.get("missing")
    assert await objects.list("") == []
    for key in ["tasks/one/a", "tasks/one/b", "tasks/two", "other"]:
        await objects.put(key, b"x")
    assert await objects.list("tasks/one/") == ["tasks/one/a", "tasks/one/b"]
    assert await objects.list("tasks/on") == ["tasks/one/a", "tasks/one/b"]
    await objects.delete("tasks/one/a")
    await objects.delete("tasks/one/a")
    with pytest.raises(NotFoundError):
        await objects.get("tasks/one/a")
    assert await objects.list("tasks/one/") == ["tasks/one/b"]


@pytest.mark.parametrize("key", ["", "/absolute", "../escape", "a/../b", "."])
async def test_invalid_keys(objects, key):
    for operation in (objects.get, objects.delete):
        with pytest.raises(ValueError):
            await operation(key)
    with pytest.raises(ValueError):
        await objects.put(key, b"bad")
    if key:
        with pytest.raises(ValueError):
            await objects.list(key)


def test_windows_object_keys_reject_anchors(monkeypatch):
    monkeypatch.setattr(local_store, "Path", PureWindowsPath)
    task_id = uuid4()
    for key in ("/absolute", r"\absolute", r"C:relative", r"C:\absolute", r"\\host\share\item"):
        with pytest.raises(ValueError):
            local_store.validate_key(key)
        with pytest.raises(ValueError):
            local_store.key_for(task_id, key)
    local_store.validate_key("nested/item")
    assert local_store.key_for(task_id, "nested/item") == f"tasks/{task_id}/nested/item"


async def test_local_symlink_escape(tmp_path):
    objects = LocalObjectStore(tmp_path / "objects")
    objects.root.mkdir()
    (objects.root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        await objects.put("link/escape", b"bad")


async def test_bucket_auto_creation(s3_endpoint):
    objects = S3ObjectStore(
        "auto-" + uuid4().hex,
        endpoint_url=s3_endpoint,
        access_key="x",
        secret_key="x",
        region="eu-west-1",
    )
    assert await objects.list() == []
    assert (
        objects.client.head_bucket(Bucket=objects.bucket)["ResponseMetadata"]["HTTPStatusCode"]
        == 200
    )


def test_factory(tmp_path, s3_endpoint):
    assert isinstance(make_object_store(Settings(data_dir=tmp_path)), LocalObjectStore)
    settings = SimpleNamespace(
        object_store="s3", s3_endpoint_url=s3_endpoint, s3_access_key="x", s3_secret_key="x"
    )
    store = make_object_store(settings)
    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "sca"
    settings.object_store = "invalid"
    with pytest.raises(ValueError):
        make_object_store(settings)
