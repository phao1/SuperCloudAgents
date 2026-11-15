import io

from moto import mock_aws

from sca.api.files import put_file
from sca.storage.s3 import S3ObjectStore


async def test_s3_upload_uses_streaming_transfer():
    data = b"csv-data\n" * 1000000
    with mock_aws():
        store = S3ObjectStore("uploads", access_key="test", secret_key="test")
        uri = await put_file(store, "tenants/one/files/example", io.BytesIO(data))
        assert uri == "s3://uploads/tenants/one/files/example"
        assert await store.get("tenants/one/files/example") == data
