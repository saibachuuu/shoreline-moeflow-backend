"""存储池的加固测试（fork 追加）。

三处都是「读代码能看出、现有用例测不到」的地方：

1. 同 endpoint 跨桶时，服务端复制从**目标桶**取源对象；
2. 校验失败重试后，``copied_objects`` / ``breakdown`` 重复累加；
3. ``StoragePoolLock.release`` 无条件删锁，会删掉别人接管的租约。
"""

from types import SimpleNamespace
from unittest.mock import patch

from tests.modules import requires_module

requires_module("storage_pool")

import datetime

from bson import ObjectId

from app.modules.storage_pool import transfer as transfer_service
from app.modules.storage_pool.backend import S3Transfer
from app.modules.storage_pool.constants import (
    StorageObjectKind,
    TransferDirection,
    TransferObjectStatus,
    TransferStatus,
)
from app.modules.storage_pool.models import (
    StoragePoolLock,
    StoragePoolSource,
    StorageTransferObject,
)
from tests import MoeTestCase


class BucketClient:
    """模拟一个 S3 endpoint 下的桶客户端。

    同一个 endpoint 的所有桶共享一份存储：``copy_object`` 按 ``CopySource``
    指定的桶去取对象——这正是拿错桶就会露馅的地方。
    """

    def __init__(self, cluster, bucket, endpoint):
        self.cluster = cluster
        self.bucket = bucket
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.copy_calls = []

    def copy_object(self, Bucket, CopySource, Key):
        self.copy_calls.append(
            {"target_bucket": Bucket, "source": CopySource, "key": Key}
        )
        src_store = self.cluster.buckets.get(CopySource["Bucket"], {})
        if CopySource["Key"] not in src_store:
            raise RuntimeError(
                f"no such key in bucket {CopySource['Bucket']}: {CopySource['Key']}"
            )
        self.cluster.buckets.setdefault(Bucket, {})[Key] = dict(
            src_store[CopySource["Key"]]
        )
        return {}


class PooledBackend:
    """带 boto3 形状 client 的假后端，用来驱动 S3Transfer 的两个分支。"""

    def __init__(self, bucket, cluster, endpoint="https://same.example.com"):
        self.bucket_name = bucket
        self.oss_key_prefix = "storage/"
        self.cluster = cluster
        self.store = cluster.buckets.setdefault(bucket, {})
        self.client = BucketClient(cluster, bucket, endpoint)

    def _to_object_key(self, key):
        return self.oss_key_prefix + key

    def _key(self, key):
        return self._to_object_key(key)

    def copy_object(self, source_key, target_key):
        return self.client.copy_object(
            Bucket=self.bucket_name,
            CopySource={"Bucket": self.bucket_name, "Key": self._key(source_key)},
            Key=self._key(target_key),
        )

    def head_object(self, key):
        meta = self.store.get(self._key(key))
        if meta is None:
            return None
        return {"key": key, **meta}

    def upload(self, path, filename, file, headers=None, progress_callback=None):
        payload = file.read() if hasattr(file, "read") else file
        self.store[self._key(path + filename)] = {
            "size": len(payload),
            "etag": "uploaded-" + filename,
            "payload": payload,
        }

    def download(self, path, filename, /, *, local_path=None):
        from io import BytesIO

        meta = self.store.get(self._key(path + filename))
        if meta is None:
            raise RuntimeError("no such key: " + path + filename)
        payload = meta.get("payload", b"")
        if local_path:
            with open(local_path, "wb") as handle:
                handle.write(payload)
            return None
        return BytesIO(payload)


class ServerSideCopyTestCase(MoeTestCase):
    def test_same_endpoint_copies_from_source_bucket_not_target(self):
        """同 endpoint 跨桶：源对象在源桶，不能去目标桶里找同名 key。"""
        cluster = SimpleNamespace(buckets={})
        src = PooledBackend("default-bucket", cluster)
        dst = PooledBackend("cold-bucket", cluster)
        src.store["storage/files/a.png"] = {
            "size": 5,
            "etag": "SRC",
            "payload": b"AAAAA",
        }
        # 目标桶里放一个同名但不同内容的诱饵：从错桶复制就会搬它。
        dst.store["storage/files/a.png"] = {
            "size": 5,
            "etag": "DECOY",
            "payload": b"ZZZZZ",
        }

        S3Transfer(src, dst).transfer("files/a.png")

        self.assertEqual(
            "SRC",
            dst.head_object("files/a.png")["etag"],
            "必须搬源桶的对象，而不是目标桶里的同名诱饵",
        )
        self.assertEqual(1, len(dst.client.copy_calls))
        self.assertEqual(
            "default-bucket",
            dst.client.copy_calls[0]["source"]["Bucket"],
            "CopySource 必须指向源桶",
        )


class RetryAccountingTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.source = StoragePoolSource(
            name="pool-acc",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        self.project = self.create_project("pool-accounting")
        for name in ("a.png", "b.png"):
            item = self.project.create_file(name)
            item.update(save_name=name)

    def _run_with(self, backends):
        with patch.object(transfer_service, "_backends_for", side_effect=lambda t: backends):
            return transfer_service.run

    def test_verify_failure_then_retry_does_not_double_count(self):
        """校验失败 → 重试 → 计数不能超过对象总数。"""
        from tests.modules.test_storage_pool import FakeBackend

        src = FakeBackend()
        bad = FakeBackend()
        original_head = bad.head_object
        seen = {"n": 0}

        def lying_head(key):
            """前两次是搬运时的回读（正常），之后是校验（故意报错大小）。"""
            meta = original_head(key)
            seen["n"] += 1
            if meta and seen["n"] > 2:
                meta = dict(meta, size=999999)
            return meta

        bad.head_object = lying_head

        with patch.object(transfer_service, "_backends_for", side_effect=lambda t: (src, bad)):
            transfer = transfer_service.prepare(self.project, self.source, TransferDirection.IN)
            transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.FAILED, transfer.status, transfer.last_error)

        # 重试：后端恢复正常
        StorageTransferObject.objects(
            transfer=transfer, status=TransferObjectStatus.FAILED
        ).update(set__status=TransferObjectStatus.PENDING)
        transfer.update(status=TransferStatus.PENDING)

        with patch.object(transfer_service, "_backends_for", side_effect=lambda t: (src, FakeBackend())):
            with patch.object(transfer_service, "_continue_later"):
                transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status, transfer.last_error)
        self.assertEqual(2, transfer.copied_objects, "重试后计数不能超过对象总数")
        self.assertEqual(transfer.total_objects, transfer.copied_objects)
        self.assertEqual(
            2,
            StorageTransferObject.objects(
                transfer=transfer,
                status__in=[
                    TransferObjectStatus.VERIFIED,
                    TransferObjectStatus.SOURCE_DELETED,
                ],
            ).count(),
        )


class LockLeaseTestCase(MoeTestCase):
    def test_release_does_not_drop_a_lock_taken_over_by_another_owner(self):
        project_id = ObjectId()
        self.assertTrue(StoragePoolLock.acquire(project_id, owner="worker-a", ttl_seconds=1))
        # 租约过期，另一个 worker 接管
        StoragePoolLock.objects(pk=project_id).update(
            set__expire_at=datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        )
        self.assertTrue(StoragePoolLock.acquire(project_id, owner="worker-b"))

        # 迟到的释放不能把别人的锁删掉
        StoragePoolLock.release(project_id, owner="worker-a")
        held = StoragePoolLock.objects(pk=project_id).first()
        self.assertIsNotNone(held, "不能删掉其它 owner 持有的锁")
        self.assertEqual("worker-b", held.owner)


class EnumerateLegacyDocumentTestCase(MoeTestCase):
    def test_output_without_file_name_is_not_enumerated(self):
        """历史导出文档缺 ``file_name`` 时不能枚举出空文件名的 key。

        这种 key（``outputs/<id>/``）搬它必然 404，而一个老文档就能把
        整趟迁移判成失败。
        """
        from app.models.output import Output

        project = self.create_project("pool-legacy-output")
        Output._get_collection().insert_one(
            {
                "_id": ObjectId(),
                "p": project.id,
                "t": ObjectId(),
                "ty": 0,
                "s": 3,
            }
        )
        keys = transfer_service.enumerate_objects(project)
        self.assertEqual([], [key for key in keys if key.startswith("outputs/")])
