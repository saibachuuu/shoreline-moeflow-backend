from io import BytesIO
from unittest import TestCase
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from oss2.exceptions import NoSuchKey

from app.constants.storage import StorageType
from app.services.oss import OSS


class R2OSSAdapterTestCase(TestCase):
    def make_config(self, *, via_cdn=False):
        return {
            "STORAGE_TYPE": StorageType.OSS,
            "OSS_BUCKET_STYLE": "R2",
            "OSS_ENDPOINT": "https://account.r2.cloudflarestorage.com",
            "OSS_ACCESS_KEY_ID": "key-id",
            "OSS_ACCESS_KEY_SECRET": "key-secret",
            "OSS_BUCKET_NAME": "moeflow",
            "STORAGE_DOMAIN": "https://cdn.example.test/",
            "OSS_VIA_CDN": via_cdn,
            "CDN_URL_KEY_A": "",
        }

    @patch("app.services.oss.boto3.client")
    def test_r2_upload_maps_key_and_headers(self, client_factory):
        client = Mock()
        client_factory.return_value = client
        oss = OSS(self.make_config())

        oss.upload(
            "files/",
            "page 1.png",
            "image-body",
            headers={"Content-Type": "image/png", "Cache-Control": "public"},
        )

        client_factory.assert_called_once_with(
            "s3",
            endpoint_url="https://account.r2.cloudflarestorage.com",
            aws_access_key_id="key-id",
            aws_secret_access_key="key-secret",
        )
        client.put_object.assert_called_once_with(
            Bucket="moeflow",
            Key="storage/files/page 1.png",
            Body=b"image-body",
            ContentType="image/png",
            CacheControl="public",
        )

    @patch("app.services.oss.boto3.client")
    def test_r2_download_delete_and_presigned_url(self, client_factory):
        client = Mock()
        client_factory.return_value = client
        client.get_object.return_value = {"Body": BytesIO(b"content")}
        client.generate_presigned_url.return_value = (
            "https://signed.example.test/object"
        )
        oss = OSS(self.make_config())

        self.assertEqual(oss.download("files/", "page.png").read(), b"content")
        client.get_object.assert_called_once_with(
            Bucket="moeflow", Key="storage/files/page.png"
        )

        oss.delete("files/", ["first.png", "second.png"])
        client.delete_objects.assert_called_once_with(
            Bucket="moeflow",
            Delete={
                "Objects": [
                    {"Key": "storage/files/first.png"},
                    {"Key": "storage/files/second.png"},
                ]
            },
        )

        self.assertEqual(
            oss.sign_url("files/", "page.png", download=True),
            "https://signed.example.test/object",
        )
        client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={
                "Bucket": "moeflow",
                "Key": "storage/files/page.png",
                "ResponseContentDisposition": "attachment",
            },
            ExpiresIn=604800,
        )

    @patch("app.services.oss.boto3.client")
    def test_r2_cdn_url_and_missing_object(self, client_factory):
        client = Mock()
        client_factory.return_value = client
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject"
        )
        oss = OSS(self.make_config())

        with self.assertRaises(NoSuchKey):
            oss.download("files/", "missing.png")

        cdn_oss = OSS(self.make_config(via_cdn=True))
        self.assertEqual(
            cdn_oss.sign_url("files/", "page 1.png"),
            "https://cdn.example.test/files/page%201.png",
        )
