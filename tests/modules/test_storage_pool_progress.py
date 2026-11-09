"""Queued cancellation, stable pagination, truthful progress and metric repair."""

from unittest.mock import patch
from mongoengine.connection import get_db

from tests.modules import requires_module

requires_module("storage_pool")

from app.modules.storage_pool import transfer as service
from app.modules.storage_pool.constants import (
    StorageObjectKind,
    TransferObjectStatus,
    TransferStatus,
)
from app.modules.storage_pool.models import (
    StoragePoolSource,
    StorageTransfer,
    StorageTransferObject,
)
from tests import MoeAPITestCase
from tests.modules.test_storage_pool import FakeBackend


class TransferProgressTestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self.create_project("progress")
        self.source = StoragePoolSource(
            name="test",
            endpoint="https://storage.invalid",
            bucket="test",
            access_key_id="test",
            secret_access_key="test",
        ).save()
        admin = self.create_user("admin")
        admin.update(admin=True)
        self.token = admin.reload().generate_token()

    def make_transfer(self):
        return StorageTransfer(project=self.project, source=self.source).save()

    def test_cancel_wins_before_worker_and_delete_cascades_manifest(self):
        transfer = self.make_transfer()
        StorageTransferObject(
            transfer=transfer, src_key="files/a.png", dst_key="files/a.png"
        ).save()
        response = self.post(
            f"/v1/admin/storage-pool/transfers/{transfer.pk}/cancel", token=self.token
        )
        self.assertErrorEqual(response)
        self.assertEqual(TransferStatus.CANCELLED, transfer.reload().status)
        with patch.object(service, "_backends_for") as backends:
            service.run(str(transfer.pk))
        backends.assert_not_called()
        response = self.delete(
            f"/v1/admin/storage-pool/transfers/{transfer.pk}", token=self.token
        )
        self.assertErrorEqual(response)
        self.assertEqual(0, StorageTransferObject.objects.count())
        self.assertEqual(0, StorageTransfer.objects.count())

    def test_running_or_switched_task_cannot_be_cancelled_or_deleted(self):
        transfer = self.make_transfer()
        for status in (
            TransferStatus.COPYING,
            TransferStatus.VERIFIED,
            TransferStatus.SWITCHED,
            TransferStatus.FAILED,
        ):
            transfer.update(status=status)
            cancel = self.post(
                f"/v1/admin/storage-pool/transfers/{transfer.pk}/cancel",
                token=self.token,
            )
            delete = self.delete(
                f"/v1/admin/storage-pool/transfers/{transfer.pk}", token=self.token
            )
            self.assertGreaterEqual(cancel.status_code, 400)
            self.assertGreaterEqual(delete.status_code, 400)
            self.assertEqual(status, transfer.reload().status)

    def test_warned_done_and_live_worker_record_kept(self):
        transfer = self.make_transfer()
        transfer.update(status=TransferStatus.DONE, last_error="残留对象")
        response = self.delete(
            f"/v1/admin/storage-pool/transfers/{transfer.pk}", token=self.token
        )
        self.assertGreaterEqual(response.status_code, 400)
        transfer.update(last_error="")
        get_db().storage_transfer_run_lock.insert_one(
            {"_id": transfer.pk, "owner": "live"}
        )
        response = self.delete(
            f"/v1/admin/storage-pool/transfers/{transfer.pk}", token=self.token
        )
        self.assertGreaterEqual(response.status_code, 400)

    def test_worker_lock_prevents_duplicate_continuation(self):
        transfer = self.make_transfer()
        get_db().storage_transfer_run_lock.insert_one(
            {"_id": transfer.pk, "owner": "live"}
        )
        with patch.object(service, "_run") as run:
            service.run(str(transfer.pk))
        run.assert_not_called()

    def test_list_paginates_stably_and_refreshes_intermediate_counts(self):
        transfers = [self.make_transfer() for _ in range(5)]
        current = transfers[2]
        current.update(total_objects=2, status=TransferStatus.COPYING)
        StorageTransferObject(
            transfer=current,
            src_key="a",
            dst_key="a",
            status=TransferObjectStatus.COPIED,
            size=123,
        ).save()
        StorageTransferObject(
            transfer=current,
            src_key="b",
            dst_key="b",
            status=TransferObjectStatus.SKIPPED,
            kind=StorageObjectKind.RESAMPLE,
        ).save()
        response = self.get(
            "/v1/admin/storage-pool/transfers?page=2&limit=2", token=self.token
        )
        self.assertErrorEqual(response)
        self.assertEqual("5", response.headers["X-Pagination-Count"])
        self.assertEqual(
            [str(transfers[2].pk), str(transfers[1].pk)],
            [row["id"] for row in response.json],
        )
        row = response.json[0]
        self.assertEqual(2, row["processed_objects"])
        self.assertEqual(1, row["skipped_objects"])
        self.assertEqual(123, row["copied_bytes"])
        self.assertLess(row["progress_percent"], 100)
        self.assertEqual(
            100, StorageTransfer(status=TransferStatus.DONE).progress_percent
        )

    def test_required_origin_missing_fails_but_optional_resample_can_skip(self):
        image = self.project.create_file("page.png")
        image.update(save_name="page.png")
        transfer = service.prepare(self.project, self.source)
        with (
            patch.object(
                service, "_backends_for", return_value=(FakeBackend(), FakeBackend())
            ),
            patch.object(
                service.S3Transfer, "transfer", side_effect=FileNotFoundError("missing")
            ),
        ):
            service.run(str(transfer.pk))
        self.assertEqual(TransferStatus.FAILED, transfer.reload().status)
        self.assertIsNone(self.project.reload().storage_source_id)
        self.assertEqual(
            1,
            StorageTransferObject.objects(
                transfer=transfer, status=TransferObjectStatus.FAILED
            ).count(),
        )

    def test_metric_repair_uses_verified_sizes_and_clears_missing_derivative(self):
        image = self.project.create_file("page.png")
        image.update(
            save_name="page.png", file_size=999, resample_bytes=888, cover_bytes=512
        )
        transfer = service.prepare(self.project, self.source)
        original = StorageTransferObject.objects(
            transfer=transfer, kind=StorageObjectKind.ORIGINAL
        ).first()
        resample = StorageTransferObject.objects(
            transfer=transfer, kind=StorageObjectKind.RESAMPLE
        ).first()
        original.update(status=TransferObjectStatus.VERIFIED, size=2048)
        resample.update(status=TransferObjectStatus.SKIPPED)
        service._refresh_project_metrics(transfer, self.project)
        image.reload()
        self.assertEqual(2, image.file_size)
        self.assertEqual(0, image.resample_bytes)
        self.assertEqual(512, self.project.reload().cover_bytes)
        self.assertEqual(round((2048 + 512) / 1024), self.project.file_size)
