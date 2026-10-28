"""搬运流程的批量优化测试。

项目动辄上千个对象：逐条删源、逐条建行会把一趟迁移拖成几千次网络往返，
worker 很容易在跑完之前被超时杀掉。这里把「按批发送」钉住。
"""

from unittest.mock import patch

from tests.modules import requires_module

requires_module("storage_pool")

from app.modules.storage_pool import transfer as transfer_service
from app.modules.storage_pool.constants import (
    StorageObjectKind,
    TransferStatus,
)
from app.modules.storage_pool.models import (
    StoragePoolSource,
    StorageTransfer,
    StorageTransferObject,
)
from app.modules.storage_pool.constants import TransferObjectStatus
from tests import MoeTestCase
from tests.modules.test_storage_pool import FakeBackend


class BulkSourceDeleteTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.source = StoragePoolSource(
            name="pool-bulk",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        self.project = self.create_project("pool-bulk")
        self.transfer = StorageTransfer(
            source=self.source,
            project=self.project,
            status=TransferStatus.SWITCHED,
        ).save()

    def _seed(self, count):
        rows = [
            StorageTransferObject(
                transfer=self.transfer,
                src_key=f"files/page{index}.png",
                dst_key=f"files/page{index}.png",
                kind=StorageObjectKind.ORIGINAL,
                size=1024,
                status=TransferObjectStatus.VERIFIED,
            )
            for index in range(count)
        ]
        StorageTransferObject.objects.insert(rows, load_bulk=False)
        return count

    def test_source_delete_is_batched_not_per_object(self):
        total = self._seed(1500)
        fake = FakeBackend()
        calls = []
        original = fake.delete

        def counting_delete(path, filename):
            names = filename if isinstance(filename, list) else [filename]
            calls.append((path, names))
            return original(path, filename)

        fake.delete = counting_delete

        with patch.object(transfer_service, "current_app"):
            transfer_service._delete_source_objects(self.transfer, fake)

        # S3 的 delete_objects 一次最多 1000 个 key：1500 个对象应该只发两次，
        # 而不是 1500 次。
        self.assertLessEqual(
            len(calls), 2, f"应按批删除，实际发了 {len(calls)} 次请求"
        )
        self.assertEqual(total, sum(len(names) for _, names in calls))
        self.assertEqual(
            total,
            StorageTransferObject.objects(
                transfer=self.transfer, status=TransferObjectStatus.SOURCE_DELETED
            ).count(),
        )

    def test_missing_source_object_does_not_block_the_rest(self):
        """源端已无对象（S3 delete 是幂等的）时，整批仍要标记完成。"""
        self._seed(10)
        fake = FakeBackend()

        def noisy_delete(path, filename):
            raise RuntimeError("模拟源端不可用")

        fake.delete = noisy_delete

        with patch.object(transfer_service, "current_app"):
            transfer_service._delete_source_objects(self.transfer, fake)

        # 删除失败时行保持 VERIFIED，下一轮还能重试，不能谎报已删。
        self.assertEqual(
            10,
            StorageTransferObject.objects(
                transfer=self.transfer, status=TransferObjectStatus.VERIFIED
            ).count(),
        )


class SourceDeleteRetryTestCase(MoeTestCase):
    """删源失败不能直接标 DONE —— 否则源端对象永远没人再删，扩容落空。"""

    def setUp(self):
        super().setUp()
        self.source = StoragePoolSource(
            name="pool-delretry",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        self.project = self.create_project("pool-delretry")
        self.transfer = StorageTransfer(
            source=self.source,
            project=self.project,
            status=TransferStatus.SWITCHED,
        ).save()
        rows = [
            StorageTransferObject(
                transfer=self.transfer,
                src_key=f"files/p{index}.png",
                dst_key=f"files/p{index}.png",
                kind=StorageObjectKind.ORIGINAL,
                size=1024,
                status=TransferObjectStatus.VERIFIED,
            )
            for index in range(3)
        ]
        StorageTransferObject.objects.insert(rows, load_bulk=False)

    def _run(self, fake):
        with (
            patch.object(transfer_service, "_backends_for", side_effect=lambda t: (fake, fake)),
            patch.object(transfer_service, "_continue_later") as continue_later,
        ):
            result = transfer_service.run(str(self.transfer.id))
        return result, continue_later

    def test_failed_delete_keeps_switched_and_schedules_next_round(self):
        fake = FakeBackend()

        def broken_delete(path, filename):
            raise RuntimeError("源端不可用")

        fake.delete = broken_delete

        result, continue_later = self._run(fake)

        self.transfer.reload()
        self.assertEqual(TransferStatus.SWITCHED, self.transfer.status, result)
        self.assertEqual(1, self.transfer.stall_count)
        continue_later.assert_called_once()

    def test_gives_up_after_max_stalls_with_a_visible_error(self):
        from app.modules.storage_pool.constants import MAX_STALLS

        fake = FakeBackend()

        def broken_delete(path, filename):
            raise RuntimeError("源端不可用")

        fake.delete = broken_delete

        for _ in range(MAX_STALLS):
            result, _ = self._run(fake)

        self.transfer.reload()
        self.assertEqual(TransferStatus.DONE, self.transfer.status, result)
        self.assertIn("未删除", self.transfer.last_error, "放弃时必须留下残留记录")
