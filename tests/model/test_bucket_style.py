"""储存语义层（BucketStyle / 能力位 / 关键对象操作）的单元测试。

这一层是阶段 1 的地基：`OSS_BUCKET_STYLE` 的历史语义是反的（"S3" 指阿里云、
"R2" 指标准 S3），归一出错会让存量部署整体 404，所以别名映射与告警都要钉住。
"""

from unittest import TestCase
from unittest.mock import Mock, patch

from app.constants.storage import (
    BucketStyle,
    StorageType,
    capabilities_for,
    parse_bucket_style,
    report_bucket_style_issues,
    requires_local_thumbnail,
    storage_capabilities,
)
from app.services.oss import OSS


class ParseBucketStyleTestCase(TestCase):
    def test_standard_s3_aliases_collapse_to_s3(self):
        # 这些都是标准 S3 兼容协议，行为必须完全一致。
        for raw in [
            "S3",
            "s3",
            "R2",
            "r2",
            "R2 ",
            "MINIO",
            "garage",
            "AWS",
            "S3_COMPATIBLE",
        ]:
            with self.subTest(raw=raw):
                result = parse_bucket_style(raw)
                self.assertEqual(result.style, BucketStyle.S3)
                self.assertIsNone(result.issue)

    def test_aliyun_aliases_collapse_to_ali(self):
        for raw in ["ALI", "ali", "ALIYUN", "aliyun_oss", "OSS"]:
            with self.subTest(raw=raw):
                result = parse_bucket_style(raw)
                self.assertEqual(result.style, BucketStyle.ALI)
                self.assertIsNone(result.issue)

    def test_empty_defaults_to_standard_s3(self):
        for raw in [None, "", "   "]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_bucket_style(raw).style, BucketStyle.S3)

    def test_unknown_value_falls_back_to_s3_with_warning(self):
        result = parse_bucket_style("S4")
        self.assertEqual(result.style, BucketStyle.S3)
        self.assertEqual(result.issue, "unknown")

    def test_aliyun_endpoint_with_s3_style_is_flagged(self):
        # 存量把 "S3" 当阿里云用的部署：必须能报警，否则签名全错。
        for raw in [None, "", "S3"]:
            with self.subTest(raw=raw):
                result = parse_bucket_style(
                    raw, "https://oss-cn-shanghai.aliyuncs.com/"
                )
                self.assertEqual(result.style, BucketStyle.S3)
                self.assertEqual(result.issue, "aliyun_endpoint_as_s3")

    def test_aliyun_endpoint_with_ali_style_is_fine(self):
        result = parse_bucket_style("ALI", "https://oss-cn-shanghai.aliyuncs.com/")
        self.assertEqual(result.style, BucketStyle.ALI)
        self.assertIsNone(result.issue)

    def test_s3_endpoint_with_s3_style_is_fine(self):
        result = parse_bucket_style("R2", "https://acc.r2.cloudflarestorage.com")
        self.assertIsNone(result.issue)


class CapabilitiesTestCase(TestCase):
    def test_standard_s3_has_no_vendor_features(self):
        caps = capabilities_for(BucketStyle.S3)
        self.assertFalse(caps.server_side_image_processing)
        self.assertFalse(caps.cdn_url_auth)
        self.assertTrue(caps.local_thumbnail_generation)

    def test_aliyun_has_server_side_image_processing(self):
        caps = capabilities_for(BucketStyle.ALI)
        self.assertTrue(caps.server_side_image_processing)
        self.assertTrue(caps.cdn_url_auth)
        self.assertFalse(caps.local_thumbnail_generation)

    def test_unknown_style_is_treated_as_standard_s3(self):
        self.assertEqual(capabilities_for("whatever"), capabilities_for(BucketStyle.S3))

    def test_requires_local_thumbnail_by_storage_type(self):
        # 本地存储永远自己生成
        self.assertTrue(
            requires_local_thumbnail({"STORAGE_TYPE": StorageType.LOCAL_STORAGE})
        )
        # 标准 S3 也自己生成
        self.assertTrue(
            requires_local_thumbnail(
                {
                    "STORAGE_TYPE": StorageType.OSS,
                    "OSS_BUCKET_STYLE": "R2",
                    "OSS_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
                }
            )
        )
        # 阿里云交给服务端图片处理
        self.assertFalse(
            requires_local_thumbnail(
                {
                    "STORAGE_TYPE": StorageType.OSS,
                    "OSS_BUCKET_STYLE": "ALI",
                    "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com/",
                }
            )
        )

    def test_storage_capabilities_reads_config(self):
        caps = storage_capabilities(
            {
                "STORAGE_TYPE": StorageType.OSS,
                "OSS_BUCKET_STYLE": "aliyun",
                "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com/",
            }
        )
        self.assertTrue(caps.server_side_image_processing)


class ReportIssuesTestCase(TestCase):
    def test_misconfigured_aliyun_endpoint_logs_error(self):
        # 发版硬要求：存量「S3 当阿里云用」的部署启动时必须能看到 ERROR。
        result = parse_bucket_style("S3", "https://oss-cn-shanghai.aliyuncs.com/")
        with self.assertLogs("app.constants.storage", level="ERROR") as captured:
            report_bucket_style_issues(result)
        self.assertIn("OSS_BUCKET_STYLE=ALI", captured.output[0])

    def test_unknown_style_logs_warning(self):
        result = parse_bucket_style("S4")
        with self.assertLogs("app.constants.storage", level="WARNING") as captured:
            report_bucket_style_issues(result)
        self.assertIn("不是已知取值", captured.output[0])

    def test_healthy_config_logs_nothing(self):
        result = parse_bucket_style("R2", "https://acc.r2.cloudflarestorage.com")
        with patch("app.constants.storage.logger") as logger:
            report_bucket_style_issues(result)
        logger.error.assert_not_called()
        logger.warning.assert_not_called()


class KeyPrefixTestCase(TestCase):
    def make_config(self, **overrides):
        config = {
            "STORAGE_TYPE": StorageType.OSS,
            "OSS_BUCKET_STYLE": "R2",
            "OSS_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
            "OSS_ACCESS_KEY_ID": "key-id",
            "OSS_ACCESS_KEY_SECRET": "key-secret",
            "OSS_BUCKET_NAME": "moeflow",
            "STORAGE_DOMAIN": "https://cdn.example.test/",
            "OSS_VIA_CDN": False,
            "CDN_URL_KEY_A": "",
        }
        config.update(overrides)
        return config

    @patch("app.services.oss.boto3.client")
    def test_default_prefix_matches_history(self, client_factory):
        client_factory.return_value = Mock()
        oss = OSS(self.make_config())
        self.assertEqual(oss.bucket_style, BucketStyle.S3)
        self.assertEqual(oss.oss_key_prefix, "storage/")
        self.assertEqual(oss._to_object_key("files/a.png"), "storage/files/a.png")
        self.assertEqual(oss._from_object_key("storage/files/a.png"), "files/a.png")

    @patch("app.services.oss.boto3.client")
    def test_prefix_is_configurable(self, client_factory):
        client_factory.return_value = Mock()
        oss = OSS(self.make_config(OSS_KEY_PREFIX="tenant-a/"))
        self.assertEqual(oss._to_object_key("files/a.png"), "tenant-a/files/a.png")
        # 前缀之外的 key 原样返回，不做误伤
        self.assertEqual(oss._from_object_key("other/a.png"), "other/a.png")

    @patch("app.services.oss.boto3.client")
    def test_r2_alias_is_zero_behaviour_change(self, client_factory):
        client_factory.return_value = Mock()
        oss = OSS(self.make_config(OSS_BUCKET_STYLE="R2"))
        self.assertEqual(oss.bucket_style, BucketStyle.S3)

    @patch("app.services.oss.oss2.Bucket")
    @patch("app.services.oss.oss2.Auth")
    def test_aliyun_branch_has_no_key_prefix(self, auth_factory, bucket_factory):
        oss = OSS(
            self.make_config(
                OSS_BUCKET_STYLE="ALI",
                OSS_KEY_PREFIX="should-be-ignored/",
                OSS_ENDPOINT="https://oss-cn-shanghai.aliyuncs.com/",
            )
        )
        self.assertEqual(oss.bucket_style, BucketStyle.ALI)
        # 阿里云分支历史上就是无前缀的，这里不能被 OSS_KEY_PREFIX 带偏。
        self.assertEqual(oss.oss_key_prefix, "")
        self.assertEqual(oss._to_object_key("files/a.png"), "files/a.png")


class ObjectOperationsTestCase(TestCase):
    """list_objects / head_object / copy_object 都在「相对 key」空间工作。"""

    def make_oss(self):
        config = {
            "STORAGE_TYPE": StorageType.OSS,
            "OSS_BUCKET_STYLE": "R2",
            "OSS_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
            "OSS_ACCESS_KEY_ID": "key-id",
            "OSS_ACCESS_KEY_SECRET": "key-secret",
            "OSS_BUCKET_NAME": "moeflow",
            "STORAGE_DOMAIN": "https://cdn.example.test/",
            "OSS_VIA_CDN": False,
            "CDN_URL_KEY_A": "",
        }
        with patch("app.services.oss.boto3.client") as client_factory:
            client = Mock()
            client_factory.return_value = client
            return OSS(config), client

    def test_list_objects_strips_prefix_and_returns_cursor(self):
        oss, client = self.make_oss()
        client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "storage/files/a.png", "Size": 10, "ETag": '"etag-a"'},
                {"Key": "storage/files/b.png", "Size": 20, "ETag": '"etag-b"'},
            ],
            "IsTruncated": True,
        }

        page = oss.list_objects("files/", max_keys=2)

        client.list_objects_v2.assert_called_once_with(
            Bucket="moeflow", Prefix="storage/files/", MaxKeys=2
        )
        self.assertEqual(
            [o["key"] for o in page["objects"]], ["files/a.png", "files/b.png"]
        )
        self.assertEqual(page["objects"][0]["etag"], "etag-a")  # 引号已剥掉
        # 游标同样在相对 key 空间，直接回传给下一页即可
        self.assertEqual(page["next"], "files/b.png")

        client.list_objects_v2.return_value = {
            "Contents": [{"Key": "storage/files/c.png", "Size": 1}],
            "IsTruncated": False,
        }
        oss.list_objects("files/", start_after=page["next"], max_keys=2)
        self.assertEqual(
            client.list_objects_v2.call_args.kwargs["StartAfter"],
            "storage/files/b.png",
        )

    def test_head_object_returns_none_when_missing(self):
        from botocore.exceptions import ClientError

        oss, client = self.make_oss()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        self.assertIsNone(oss.head_object("files/missing.png"))

    def test_head_object_normalises_metadata(self):
        oss, client = self.make_oss()
        client.head_object.return_value = {"ContentLength": 42, "ETag": '"abc"'}
        meta = oss.head_object("files/a.png")
        client.head_object.assert_called_once_with(
            Bucket="moeflow", Key="storage/files/a.png"
        )
        self.assertEqual(meta["size"], 42)
        self.assertEqual(meta["etag"], "abc")

    def test_copy_object_is_server_side_within_bucket(self):
        oss, client = self.make_oss()
        oss.copy_object("files/a.png", "files/b.png")
        client.copy_object.assert_called_once_with(
            Bucket="moeflow",
            CopySource={"Bucket": "moeflow", "Key": "storage/files/a.png"},
            Key="storage/files/b.png",
        )
