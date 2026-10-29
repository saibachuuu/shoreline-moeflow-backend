"""存储池模块的测试。

重点覆盖三类容易写错的地方：

1. **凭据不外泄**（`to_api` / 日志）；
2. **`sub_path` 与删除的保护**（已有项目迁入时不许改、不许删）；
3. **状态机顺序**（切指针必须在删源之前；搬运期间文件有变更必须中止）。
"""

from unittest.mock import patch

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("storage_pool")

import datetime

from bson import ObjectId

from app.models.project import Project
from app.modules.storage_pool import tasks as tasks_module
from app.modules.storage_pool import transfer as transfer_service
from app.modules.storage_pool.constants import (
    TransferDirection,
    TransferObjectStatus,
    TransferStatus,
)
from app.modules.storage_pool.models import (
    StoragePoolLock,
    StoragePoolSetting,
    StoragePoolSource,
    StorageTransfer,
    StorageTransferObject,
    normalize_sub_path,
)
from tests import MoeAPITestCase, MoeTestCase


class FakeBackend:
    """替身后端：只在内存里记 key，绝不联网。"""

    def __init__(self):
        self.objects = {}
        self.client = None  # 让 S3Transfer 走「同 endpoint」分支
        self.deleted = []

    def copy_object(self, source_key, target_key):
        self.objects[target_key] = {"size": 1024, "etag": "etag-" + target_key}

    def head_object(self, key):
        meta = self.objects.get(key)
        return {"key": key, **meta} if meta else None

    def upload(self, path, filename, file, headers=None, progress_callback=None):
        self.objects[path + filename] = {"size": 1024, "etag": "uploaded"}

    def download(self, path, filename, /, *, local_path=None):
        """流式搬运时把内容写到本地临时文件。"""
        from io import BytesIO

        payload = b"fake-object-bytes"
        if local_path:
            with open(local_path, "wb") as file:
                file.write(payload)
            return None
        return BytesIO(payload)

    def delete(self, path, filename):
        names = filename if isinstance(filename, list) else [filename]
        for name in names:
            self.deleted.append(path + name)
            self.objects.pop(path + name, None)


class SubPathTestCase(MoeTestCase):
    def test_normalize(self):
        cases = {
            "storage/": "storage/",
            "storage": "storage/",
            "/storage/": "storage/",
            "pool-a/2026": "pool-a/2026/",
            "": "",
            "/": "",
            None: "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, normalize_sub_path(raw))


class SourceApiShapeTestCase(MoeAPITestCase):
    def _admin_token(self):
        admin = self.create_user("pool-admin")
        admin.update(set__admin=True)
        admin.reload()
        return admin.generate_token()

    def _make_source(self, **kwargs):
        data = dict(
            name="pool-a",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="super-secret-value",
        )
        data.update(kwargs)
        return StoragePoolSource(**data).save()

    def test_secret_is_never_exposed(self):
        source = self._make_source()
        payload = source.to_api()
        self.assertNotIn("secret_access_key", payload)
        self.assertNotIn("super-secret-value", str(payload))

    def test_access_key_is_masked(self):
        source = self._make_source()
        self.assertEqual("AKIA...1234", source.to_api()["access_key_id"])

    def test_short_access_key_is_fully_masked(self):
        source = self._make_source(access_key_id="short")
        self.assertEqual("*****", source.to_api()["access_key_id"])


class SubPathLockTestCase(MoeAPITestCase):
    def _admin_token(self):
        admin = self.create_user("pool-admin-2")
        admin.update(set__admin=True)
        admin.reload()
        return admin.generate_token()

    def test_sub_path_change_is_rejected_when_source_has_data(self):
        source = StoragePoolSource(
            name="locked",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            has_data=True,
        ).save()
        response = self.put(
            f"/v1/admin/storage-pool/sources/{source.id}",
            json={"sub_path": "elsewhere/"},
            token=self._admin_token(),
        )
        self.assertEqual(409, response.status_code)
        source.reload()
        self.assertEqual("storage/", source.normalized_sub_path())

    def test_same_sub_path_is_allowed_even_with_data(self):
        source = StoragePoolSource(
            name="locked2",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            has_data=True,
        ).save()
        # 等价写法（多一个尾斜杠）不算修改
        response = self.put(
            f"/v1/admin/storage-pool/sources/{source.id}",
            json={"sub_path": "storage//", "name": "locked2-renamed"},
            token=self._admin_token(),
        )
        self.assertEqual(200, response.status_code)

    def test_source_with_migrated_project_cannot_be_deleted(self):
        source = StoragePoolSource(
            name="in-use",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        project = self.create_project("in-use-project")
        Project.objects(id=project.id).update(storage_source_id=source.id)

        response = self.delete(
            f"/v1/admin/storage-pool/sources/{source.id}",
            token=self._admin_token(),
        )
        self.assertEqual(409, response.status_code)
        self.assertIsNotNone(StoragePoolSource.objects(id=source.id).first())


class TransferStateMachineTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.source = StoragePoolSource(
            name="pool-sm",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            max_objects_per_run=100,
        ).save()
        self.project = self.create_project("pool-state")
        image = self.project.create_file("page.png")
        image.update(save_name="page.png")
        self.fake = FakeBackend()

    def _patch_backends(self):
        """把「源/目的后端」整体换成内存假件。

        不要依赖全局 ``oss`` 单例：它的类型取决于前面跑过哪些测试
        （本地存储 → 无 client；OSS → boto3 client），跨用例跑会让这里
        走不同的搬运分支，测试就变成顺序相关的。
        """
        self.src_backend = FakeBackend()
        return patch.object(
            transfer_service,
            "_backends_for",
            side_effect=lambda transfer: (self.src_backend, self.fake),
        )

    def _run(self, transfer):
        with self._patch_backends():
            return transfer_service.run(str(transfer.id))

    def test_happy_path_switches_pointer_before_deleting_source(self):
        with self._patch_backends():
            transfer = transfer_service.prepare(
                self.project, self.source, TransferDirection.IN
            )
            # 一张图 = 原图 + 重采样图，两个对象。
            self.assertEqual(2, transfer.total_objects)
            result = transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status, result)
        self.project.reload()
        self.assertEqual(self.source.id, self.project.storage_source_id)
        self.assertIsNotNone(self.project.storage_source_switched_time)
        self.source.reload()
        self.assertTrue(self.source.has_data)

        row = StorageTransferObject.objects(transfer=transfer).first()
        self.assertEqual(TransferObjectStatus.SOURCE_DELETED, row.status)

    def test_edit_time_change_aborts_instead_of_switching(self):
        with self._patch_backends():
            transfer = transfer_service.prepare(
                self.project, self.source, TransferDirection.IN
            )
            # 模拟「搬运开始时项目是另一个状态」：快照与当前 edit_time 不一致。
            import datetime

            transfer.update(set__snapshot_edit_time=datetime.datetime(2000, 1, 1))
            result = transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.ABORTED, transfer.status, result)
        self.project.reload()
        self.assertIsNone(self.project.storage_source_id, "中止时绝不能切指针")

    def test_restore_direction_moves_pointer_back(self):
        Project.objects(id=self.project.id).update(storage_source_id=self.source.id)
        self.project.reload()

        with self._patch_backends():
            transfer = transfer_service.prepare(
                self.project, self.source, TransferDirection.OUT
            )
            transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status)
        self.project.reload()
        self.assertIsNone(self.project.storage_source_id)

    def test_prepare_is_idempotent_while_active(self):
        with self._patch_backends():
            first = transfer_service.prepare(self.project, self.source)
            second = transfer_service.prepare(self.project, self.source)
        self.assertEqual(first.id, second.id)

    def test_batch_limit_keeps_transfer_running(self):
        for index in range(3):
            image = self.project.create_file(f"page{index}.png")
            image.update(save_name=f"page{index}.png")
        self.source.update(max_objects_per_run=2)

        with (
            self._patch_backends(),
            patch.object(transfer_service, "_continue_later") as continue_later,
        ):
            transfer = transfer_service.prepare(self.project, self.source)
            # 4 张图 ×（原图 + 重采样图）= 8 个对象。
            self.assertEqual(8, transfer.total_objects)
            result = transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.COPYING, transfer.status, result)
        self.assertIn("还剩", result)
        # 本轮只搬了 2 个
        self.assertEqual(
            2,
            StorageTransferObject.objects(
                transfer=transfer, status=TransferObjectStatus.COPIED
            ).count(),
        )
        # 关键：必须自己安排下一轮。没人续跑的话项目会永远卡在 COPYING，
        # 而它此刻读的仍是旧桶 —— 这是阶段 3 留下的真实缺陷。
        continue_later.assert_called_once_with(transfer.id)

        # 手动续跑能把剩下的搬完
        with self._patch_backends(), patch.object(transfer_service, "_continue_later"):
            for _ in range(4):
                transfer_service.run(str(transfer.id))
        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status)


class LockTestCase(MoeTestCase):
    def test_lease_lock_is_exclusive_and_releasable(self):
        project_id = ObjectId()
        self.assertTrue(StoragePoolLock.acquire(project_id, owner="a"))
        self.assertFalse(StoragePoolLock.acquire(project_id, owner="b"))
        StoragePoolLock.release(project_id)
        self.assertTrue(StoragePoolLock.acquire(project_id, owner="b"))


class EnumerateTestCase(MoeTestCase):
    def test_images_resamples_and_outputs_are_migrated(self):
        project = self.create_project("pool-enum")
        image = project.create_file("a.png")
        image.update(save_name="a.png", type=2)
        text = project.create_file("b.txt")
        text.update(save_name="b.txt", type=3)

        keys = transfer_service.enumerate_objects(project)
        # 原图 + 重采样图；文本不搬，也不应出现在清单里。
        self.assertEqual(["files/a.png", "files/resample-a.webp"], keys)

    def test_resample_kind_is_recorded_for_accounting(self):
        from app.modules.storage_pool.constants import StorageObjectKind

        project = self.create_project("pool-enum-kind")
        image = project.create_file("a.png")
        image.update(save_name="a.png", type=2)
        text = project.create_file("b.txt")
        text.update(save_name="b.txt", type=3)

        kinds = transfer_service.enumerate_object_kinds(project)
        self.assertEqual(StorageObjectKind.ORIGINAL, kinds["files/a.png"])
        self.assertEqual(StorageObjectKind.RESAMPLE, kinds["files/resample-a.webp"])
        self.assertNotIn("files/b.txt", kinds)


class SettingDefaultTestCase(MoeTestCase):
    """开关默认值：装完不搬 —— 第一次必须由人手动触发并盯着。"""

    def test_defaults_are_scan_off_run_on(self):
        StoragePoolSetting.objects.delete()
        setting = StoragePoolSetting.get()
        self.assertFalse(setting.scan_enabled, "默认不能自动搬数据")
        self.assertTrue(setting.run_enabled, "默认不阻止已排队任务")
        self.assertIsNone(setting.last_scan_time)

    def test_get_is_idempotent(self):
        StoragePoolSetting.objects.delete()
        first = StoragePoolSetting.get()
        second = StoragePoolSetting.get()
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, StoragePoolSetting.objects.count())


class ScanGateTestCase(MoeTestCase):
    """巡查开关：关着不产生任何迁移任务；手动触发不受限制。"""

    def setUp(self):
        super().setUp()
        StoragePoolSetting.objects.delete()
        self.source = StoragePoolSource(
            name="gate-source",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            inactive_days=30,
        ).save()
        self.project = self.create_project("gate-project")
        # 让它够冷：edit_time 远早于 cutoff
        Project.objects(id=self.project.id).update(
            set__edit_time=datetime.datetime.utcnow() - datetime.timedelta(days=200)
        )

    def _run_scan(self, **kwargs):
        with patch.object(tasks_module.storage_pool_run, "delay") as delay:
            result = tasks_module.storage_pool_scan(**kwargs)
        return result, delay

    def test_disabled_scan_creates_nothing(self):
        StoragePoolSetting.get().update(set__scan_enabled=False)
        result, delay = self._run_scan()
        self.assertIn("已停用", result)
        delay.assert_not_called()
        self.assertEqual(0, StorageTransfer.objects.count())
        # 被开关跳过时不覆盖「上次真实巡查结果」
        self.assertIsNone(StoragePoolSetting.get().last_scan_time)

    def test_enabled_scan_queues_and_records(self):
        StoragePoolSetting.get().update(set__scan_enabled=True)
        result, delay = self._run_scan()
        self.assertIn("已排队", result)
        self.assertEqual(1, StorageTransfer.objects.count())
        delay.assert_called_once()
        setting = StoragePoolSetting.get()
        self.assertIsNotNone(setting.last_scan_time)
        self.assertEqual(1, setting.last_scan_queued)

    def test_force_ignores_switch(self):
        StoragePoolSetting.get().update(set__scan_enabled=False)
        result, delay = self._run_scan(force=True)
        self.assertIn("已排队", result)
        self.assertEqual(1, StorageTransfer.objects.count())
        delay.assert_called_once()

    def test_switch_read_failure_fails_closed(self):
        # 读开关炸了必须按「关」处理：迁移会搬数据并删源，看不清就不动手。
        with patch.object(
            StoragePoolSetting, "get", side_effect=RuntimeError("mongo down")
        ):
            result, delay = self._run_scan()
        self.assertIn("失败", result)
        delay.assert_not_called()
        self.assertEqual(0, StorageTransfer.objects.count())

    def test_busy_project_with_presence_is_skipped(self):
        """有在线心跳的用户正在操作时，巡查放弃将该项目放入迁移队列。"""
        from app.models.presence import ProjectPresence

        StoragePoolSetting.get().update(set__scan_enabled=True)
        now = datetime.datetime.utcnow()
        user = self.create_user("busy-user")
        ProjectPresence(
            project=self.project,
            team=self.project.team,
            user=user,
            action="editing_text",
            last_heartbeat=now,
            expire_at=now + datetime.timedelta(seconds=45),
        ).save()

        result, delay = self._run_scan()
        delay.assert_not_called()
        self.assertEqual(0, StorageTransfer.objects.count())

    def test_busy_project_with_recent_edit_time_is_skipped(self):
        """项目在最近 15 分钟内有编辑动作（如正在保存项目信息或编辑文本），巡查放弃将该项目放入迁移队列。"""
        StoragePoolSetting.get().update(set__scan_enabled=True)
        now = datetime.datetime.utcnow()
        Project.objects(id=self.project.id).update(
            set__edit_time=now - datetime.timedelta(minutes=5)
        )

        result, delay = self._run_scan(force=True)
        delay.assert_not_called()
        self.assertEqual(0, StorageTransfer.objects.count())

    def test_busy_project_with_presence_buffer_is_skipped(self):
        """心跳即便刚过期片刻（处于5分钟安全缓冲期内），巡查依然将其作为活跃保护放弃迁移。"""
        from app.models.presence import ProjectPresence

        StoragePoolSetting.get().update(set__scan_enabled=True)
        now = datetime.datetime.utcnow()
        user = self.create_user("busy-user")
        ProjectPresence(
            project=self.project,
            team=self.project.team,
            user=user,
            action="editing_text",
            last_heartbeat=now - datetime.timedelta(minutes=2),
            expire_at=now - datetime.timedelta(minutes=1),
        ).save()

        result, delay = self._run_scan()
        delay.assert_not_called()
        self.assertEqual(0, StorageTransfer.objects.count())


class RunGateTestCase(MoeTestCase):
    """紧急停止：关掉后 worker 不领取新批次，任务留在待执行。"""

    def setUp(self):
        super().setUp()
        StoragePoolSetting.objects.delete()
        self.source = StoragePoolSource(
            name="run-gate",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        self.project = self.create_project("run-gate-project")
        self.transfer = transfer_service.prepare(self.project, self.source)

    def test_emergency_stop_keeps_transfer_pending(self):
        StoragePoolSetting.get().update(set__run_enabled=False)
        with patch.object(transfer_service, "run") as run:
            result = tasks_module.storage_pool_run(str(self.transfer.id))
        self.assertIn("暂停", result)
        run.assert_not_called()
        self.transfer.reload()
        self.assertEqual(TransferStatus.PENDING, self.transfer.status)

    def test_resume_runs_again(self):
        StoragePoolSetting.get().update(set__run_enabled=True)
        with patch.object(transfer_service, "run", return_value="成功") as run:
            tasks_module.storage_pool_run(str(self.transfer.id))
        run.assert_called_once()


class SettingsApiTestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        StoragePoolSetting.objects.delete()
        admin = self.create_user("settings-admin")
        admin.update(set__admin=True)
        admin.reload()
        self.admin = admin
        self.token = admin.generate_token()

    def test_get_returns_defaults_and_next_run(self):
        response = self.get("/v1/admin/storage-pool/settings", token=self.token)
        self.assertErrorEqual(response)
        payload = response.json
        self.assertFalse(payload["scan_enabled"])
        self.assertTrue(payload["run_enabled"])
        self.assertIn("next_run_at", payload)

    def test_put_only_changes_provided_field(self):
        response = self.put(
            "/v1/admin/storage-pool/settings",
            json={"scan_enabled": True},
            token=self.token,
        )
        self.assertErrorEqual(response)
        setting = StoragePoolSetting.get()
        self.assertTrue(setting.scan_enabled)
        self.assertTrue(setting.run_enabled, "没传的字段不能被顺手改掉")
        self.assertEqual(str(self.admin.id), str(setting.updated_by))

    def test_non_admin_is_rejected(self):
        from app.exceptions.base import NoPermissionError

        user = self.create_user("settings-plain")
        response = self.get(
            "/v1/admin/storage-pool/settings", token=user.generate_token()
        )
        self.assertErrorEqual(response, NoPermissionError)

    def test_manual_scan_endpoint_works_when_disabled(self):
        StoragePoolSource(
            name="api-scan",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        StoragePoolSetting.get().update(set__scan_enabled=False)
        with patch.object(tasks_module.storage_pool_scan, "delay") as delay:
            response = self.post("/v1/admin/storage-pool/scan", token=self.token)
        self.assertErrorEqual(response)
        delay.assert_called_once_with(force=True)


class BudgetTestCase(MoeTestCase):
    """单轮工作量上限：整项目取整、旧先新后、超大项目不饿死。"""

    def setUp(self):
        super().setUp()
        StoragePoolSetting.objects.delete()
        StoragePoolSetting.get().update(
            set__scan_enabled=True,
            set__run_max_projects=1,
            set__run_max_bytes=10 * 1024**3,
        )
        self.source = StoragePoolSource(
            name="budget-source",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            inactive_days=30,
        ).save()

    def _cold_project(self, name, file_size_kb, days):
        project = self.create_project(name)
        Project.objects(id=project.id).update(
            set__edit_time=datetime.datetime.utcnow() - datetime.timedelta(days=days),
            set__file_size=file_size_kb,
        )
        return project

    def _scan(self):
        with patch.object(tasks_module.storage_pool_run, "delay") as delay:
            tasks_module.storage_pool_scan()
        return delay

    def test_scan_starts_at_most_max_projects(self):
        self._cold_project("budget-a", 1024, 200)
        self._cold_project("budget-b", 1024, 300)
        delay = self._scan()
        self.assertEqual(1, StorageTransfer.objects.count())
        self.assertEqual(1, delay.call_count)

    def test_scan_picks_the_oldest_first(self):
        cold = self._cold_project("older", 1024, 300)
        self._cold_project("newer", 1024, 200)
        self._scan()
        transfer = StorageTransfer.objects.first()
        self.assertEqual(str(cold.id), str(transfer.project.id))

    def test_single_project_over_budget_still_starts(self):
        # 预算 10 MB，项目 10 GB：允许它独占一轮，否则永远排在队尾。
        StoragePoolSetting.get().update(set__run_max_bytes=10 * 1024**2)
        self._cold_project("huge", 10 * 1024 * 1024, 300)
        self._scan()
        self.assertEqual(1, StorageTransfer.objects.count())

    def test_budget_caps_how_many_projects_start(self):
        StoragePoolSetting.get().update(
            set__run_max_projects=10, set__run_max_bytes=2 * 1024**3
        )
        # 每个 800 MB，2 GB 预算只装得下两个整项目（第三个会超）
        for index in range(4):
            self._cold_project(f"size-{index}", 800 * 1024, 300 - index)
        self._scan()
        self.assertEqual(2, StorageTransfer.objects.count())


class BreakdownTestCase(MoeTestCase):
    """搬的时候按类别记账：原图与导出产物分开。"""

    def setUp(self):
        super().setUp()
        self.source = StoragePoolSource(
            name="breakdown-source",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
            max_objects_per_run=100,
        ).save()
        self.project = self.create_project("pool-breakdown")
        image = self.project.create_file("page.png")
        image.update(save_name="page.png")
        text = self.project.create_file("note.txt")
        text.update(save_name="note.txt", type=3)

        from app.constants.output import OutputStatus
        from app.models.output import Output

        target = self.project.targets().first()
        self.assertIsNotNone(target, "项目默认目标语言应存在")
        Output(
            project=self.project,
            target=target,
            file_name="result.zip",
            status=OutputStatus.SUCCEEDED,
        ).save()

    def test_breakdown_separates_images_and_outputs(self):
        fake = FakeBackend()
        with (
            patch.object(
                transfer_service,
                "_backends_for",
                side_effect=lambda transfer: (FakeBackend(), fake),
            ),
            patch.object(transfer_service, "_continue_later"),
        ):
            transfer = transfer_service.prepare(self.project, self.source)
            self.assertEqual(3, transfer.total_objects)
            transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status)
        breakdown = transfer.breakdown
        self.assertIsNotNone(breakdown)
        self.assertEqual(3, breakdown.object_count)
        # FakeBackend 每个对象固定 1024 字节；原图 + 重采样图算 origin，
        # 文本不搬，导出产物单独算 output。
        self.assertEqual(2048, breakdown.origin_bytes)
        self.assertEqual(1024, breakdown.output_bytes)


class UsageApiTestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        admin = self.create_user("usage-admin")
        admin.update(set__admin=True)
        admin.reload()
        self.token = admin.generate_token()

    def test_usage_sums_project_sizes(self):
        project = self.create_project("usage-project")
        Project.objects(id=project.id).update(
            set__file_size=2048, set__derived_bytes=100, set__output_bytes=200
        )
        response = self.get("/v1/admin/storage-pool/usage", token=self.token)
        self.assertErrorEqual(response)
        payload = response.json
        self.assertEqual(1, payload["total"]["projects"])
        self.assertEqual(2048 * 1024, payload["total"]["bytes"])
        self.assertEqual(100, payload["total"]["derived_bytes"])
        self.assertEqual(200, payload["total"]["output_bytes"])
        self.assertEqual(1, payload["pending"]["projects"])
        self.assertEqual(0, payload["migrated_projects"])
        self.assertEqual(1, len(payload["top_projects"]))

    def test_usage_requires_admin(self):
        from app.exceptions.base import NoPermissionError

        user = self.create_user("usage-plain")
        response = self.get("/v1/admin/storage-pool/usage", token=user.generate_token())
        self.assertErrorEqual(response, NoPermissionError)


class StoragePoolFixRegressionTestCase(MoeAPITestCase):
    """本轮修复的回归测试：禁用的来源、位置字段锁定、TEXT 路由、ABORTED 重试。"""

    def _admin_token(self):
        admin = self.create_user("fix-admin")
        admin.update(set__admin=True)
        admin.reload()
        return admin.generate_token()

    def _make_source(self, **kwargs):
        data = dict(
            name="fix-pool",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        )
        data.update(kwargs)
        return StoragePoolSource(**data).save()

    def test_migrate_to_disabled_source_is_rejected(self):
        source = self._make_source(enabled=False)
        project = self.create_project("fix-disabled")
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/migrate",
            json={"source_id": str(source.id)},
            token=self._admin_token(),
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            0,
            StorageTransfer.objects(
                project=project, status__in=TransferStatus.ACTIVE
            ).count(),
        )

    def test_location_fields_locked_once_source_has_data(self):
        source = self._make_source(has_data=True)
        for field, value in [
            ("endpoint", "https://other.example.com"),
            ("bucket", "other-bucket"),
            ("region", "us-east-1"),
            ("addressing_style", "virtual"),
        ]:
            response = self.put(
                f"/v1/admin/storage-pool/sources/{source.id}",
                json={field: value},
                token=self._admin_token(),
            )
            self.assertEqual(409, response.status_code, field)
        source.reload()
        self.assertEqual("https://acc.r2.cloudflarestorage.com", source.endpoint)
        self.assertEqual("cold", source.bucket)

    def test_aborted_retry_rebuilds_manifest(self):
        from app.modules.storage_pool import api as api_module

        source = self._make_source()
        project = self.create_project("fix-retry")
        image = project.create_file("page.png")
        image.update(save_name="page.png")
        transfer = transfer_service.prepare(project, source, TransferDirection.IN)
        # 一张图 = 原图 + 重采样图。
        self.assertEqual(2, transfer.total_objects)
        transfer.update(set__status=TransferStatus.ABORTED)
        # 中止后新增一个文件：旧逻辑重试时清单还是旧数，切指针必再次 ABORT。
        extra = project.create_file("page2.png")
        extra.update(save_name="page2.png")
        with patch.object(api_module, "_dispatch"):
            response = self.post(
                f"/v1/admin/storage-pool/transfers/{transfer.id}/retry",
                token=self._admin_token(),
            )
        self.assertErrorEqual(response)
        transfer.reload()
        self.assertEqual(TransferStatus.PENDING, transfer.status)
        # 两张图 × 2 个对象。
        self.assertEqual(4, transfer.total_objects)
        self.assertEqual(
            4,
            StorageTransferObject.objects(transfer=transfer).count(),
        )

    def test_text_files_are_not_migrated(self):
        from app import oss
        from app.constants.file import FileType

        project = self.create_project("fix-text")
        image = project.create_file("page.png")
        image.update(save_name="page.png")
        text = project.create_file("note.txt")
        text.update(save_name="note.txt", type=FileType.TEXT)
        text.reload()

        # 文本永不迁移：读取固定走默认存储，即使项目以后指向存储池。
        self.assertIs(oss, text.storage_backend)
        # 清单里只有原图和重采样图，没有文本。
        kinds = transfer_service.enumerate_object_kinds(project)
        self.assertIn("files/page.png", kinds)
        self.assertIn("files/resample-page.webp", kinds)
        self.assertNotIn("files/note.txt", kinds)

    def test_transfer_list_filters_by_project(self):
        source = self._make_source()
        project_a = self.create_project("fix-filter-a")
        project_b = self.create_project("fix-filter-b")
        image = project_a.create_file("a.png")
        image.update(save_name="a.png")
        transfer_service.prepare(project_a, source, TransferDirection.IN)
        transfer_service.prepare(project_b, source, TransferDirection.IN)

        response = self.get(
            f"/v1/admin/storage-pool/transfers?project_id={project_a.id}",
            token=self._admin_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual(1, len(response.json))
        self.assertEqual(str(project_a.id), response.json[0]["project"]["id"])

        # 格式不对的 id 给空列表，不 500。
        response = self.get(
            "/v1/admin/storage-pool/transfers?project_id=nope",
            token=self._admin_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual([], response.json)

    def test_project_api_exposes_storage_location(self):
        source = self._make_source()
        project = self.create_project("fix-expose")
        Project.objects(id=project.id).update(storage_source_id=source.id)
        project.reload()
        payload = project.to_api()
        self.assertEqual(str(source.id), payload["storage_source_id"])
        self.assertFalse(payload["storage_pool_locked"])


class MoveAndLockTestCase(MoeAPITestCase):
    """统一搬迁（move）与存储锁定。

    move 一个接口包圆三种去向：默认→源、源→默认、源A→源B；
    锁定后自动巡查跳过、手动三个接口全拦。
    """

    def setUp(self):
        super().setUp()
        # 按住入队：测试里 broker 不可用时 _dispatch 会退化为同步真跑，
        # 假 backend 还没套上，任务会直接 FAILED。
        from app.modules.storage_pool import api as api_module

        patcher = patch.object(api_module, "_dispatch")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _admin_token(self):
        admin = self.create_user("move-admin")
        admin.update(set__admin=True)
        admin.reload()
        return admin.generate_token()

    def _make_source(self, name="pool-a", **kwargs):
        data = dict(
            name=name,
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            sub_path="storage/",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="super-secret-value",
        )
        data.update(kwargs)
        return StoragePoolSource(**data).save()

    def _fresh_project(self, name):
        project = self.create_project(name)
        image = project.create_file("page.png")
        image.update(save_name="page.png")
        return project

    def _patch_move(self, src_backend, dst_backend):
        """搬运后端整体换成内存假件。

        同一对 backend 在多轮 run 之间保持不变：第二、三轮的校验
        读的是第一轮写进去的同一份内存，换新的空件会校验失败。
        """
        return patch.object(
            transfer_service,
            "_backends_for",
            side_effect=lambda transfer: (src_backend, dst_backend),
        )

    def _run_all(self, transfer):
        with (
            self._patch_move(FakeBackend(), FakeBackend()),
            patch.object(transfer_service, "_continue_later"),
        ):
            transfer_service.run(str(transfer.id))
            transfer_service.run(str(transfer.id))
            transfer_service.run(str(transfer.id))

    def test_move_default_to_source_and_back(self):
        token = self._admin_token()
        source = self._make_source()
        project = self._fresh_project("move-roundtrip")

        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={"target_source_id": str(source.id)},
            token=token,
        )
        self.assertErrorEqual(response)
        self._run_all(StorageTransfer.objects(project=project).first())
        project.reload()
        self.assertEqual(source.id, project.storage_source_id)

        # 目标就是当前位置：拒绝，不建空任务。
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={"target_source_id": str(source.id)},
            token=token,
        )
        self.assertEqual(409, response.status_code)

        # 空目标 = 搬回默认存储。
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={"target_source_id": ""},
            token=token,
        )
        self.assertErrorEqual(response)
        self._run_all(
            StorageTransfer.objects(project=project).order_by("-create_time").first()
        )
        project.reload()
        self.assertIsNone(project.storage_source_id)

        # 已在默认还要回默认：拒绝。
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={},
            token=token,
        )
        self.assertEqual(409, response.status_code)

    def test_move_source_to_source_switches_pointer(self):
        token = self._admin_token()
        source_a = self._make_source("pool-a")
        source_b = self._make_source("pool-b")
        project = self._fresh_project("move-direct")
        Project.objects(id=project.id).update(storage_source_id=source_a.id)
        project.reload()
        source_a.update(set__has_data=True)

        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={"target_source_id": str(source_b.id)},
            token=token,
        )
        self.assertErrorEqual(response)
        transfer = StorageTransfer.objects(project=project).first()
        self.assertEqual(TransferDirection.MOVE, transfer.direction)
        self.assertEqual(source_a.id, transfer.source.id)
        self.assertEqual(source_b.id, transfer.target_source.id)

        src_backend, dst_backend = FakeBackend(), FakeBackend()
        with (
            self._patch_move(src_backend, dst_backend),
            patch.object(transfer_service, "_continue_later"),
        ):
            transfer_service.run(str(transfer.id))
            transfer_service.run(str(transfer.id))
            transfer_service.run(str(transfer.id))
        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status)
        project.reload()
        self.assertEqual(source_b.id, project.storage_source_id)
        # 两边 has_data 都重算：A 搬空解锁、B 进项目标记。
        source_a.reload()
        source_b.reload()
        self.assertFalse(source_a.has_data)
        self.assertTrue(source_b.has_data)

    def test_lock_blocks_manual_and_scan(self):
        token = self._admin_token()
        source = self._make_source()
        project = self._fresh_project("move-locked")

        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/lock",
            json={"locked": True},
            token=token,
        )
        self.assertErrorEqual(response)
        project.reload()
        self.assertTrue(project.storage_pool_locked)
        self.assertTrue(project.to_api()["storage_pool_locked"])

        for url, body in [
            (
                f"/v1/admin/storage-pool/projects/{project.id}/migrate",
                {"source_id": str(source.id)},
            ),
            (
                f"/v1/admin/storage-pool/projects/{project.id}/move",
                {"target_source_id": str(source.id)},
            ),
            (
                f"/v1/admin/storage-pool/projects/{project.id}/restore",
                {},
            ),
        ]:
            response = self.post(url, json=body, token=token)
            self.assertEqual(409, response.status_code, url)
        self.assertEqual(0, StorageTransfer.objects(project=project).count())

        # 自动巡查也跳过锁定的项目。
        StoragePoolSetting.get().update(set__scan_enabled=True)
        Project.objects(id=project.id).update(
            set__edit_time=datetime.datetime.utcnow() - datetime.timedelta(days=200)
        )
        with patch.object(tasks_module.storage_pool_run, "delay"):
            tasks_module.storage_pool_scan()
        self.assertEqual(0, StorageTransfer.objects(project=project).count())

        # 解锁后恢复正常。
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/lock",
            json={"locked": False},
            token=token,
        )
        self.assertErrorEqual(response)
        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/move",
            json={"target_source_id": str(source.id)},
            token=token,
        )
        self.assertErrorEqual(response)
        self.assertEqual(1, StorageTransfer.objects(project=project).count())

    def test_lock_refused_while_transfer_active(self):
        token = self._admin_token()
        source = self._make_source()
        project = self._fresh_project("move-lock-busy")
        transfer_service.prepare(project, source, TransferDirection.IN)

        response = self.post(
            f"/v1/admin/storage-pool/projects/{project.id}/lock",
            json={"locked": True},
            token=token,
        )
        self.assertEqual(409, response.status_code)
        project.reload()
        self.assertFalse(project.storage_pool_locked)


class SkippedObjectTestCase(MoeTestCase):
    """重采样图还没生成时，搬运应跳过而不是判失败。"""

    def setUp(self):
        super().setUp()
        StoragePoolSetting.objects.delete()
        self.source = StoragePoolSource(
            name="skip-source",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        self.project = self.create_project("skip-project")
        image = self.project.create_file("page.png")
        image.update(save_name="page.png")

    def test_missing_resample_is_skipped_not_failed(self):
        from oss2.exceptions import NoSuchKey

        class MissingResampleBackend(FakeBackend):
            def download(self, path, filename, /, *, local_path=None):
                if filename.startswith("resample-"):
                    raise NoSuchKey(status=404, headers={}, body={}, details={})
                return super().download(path, filename, local_path=local_path)

        with (
            patch.object(
                transfer_service,
                "_backends_for",
                side_effect=lambda transfer: (MissingResampleBackend(), FakeBackend()),
            ),
            patch.object(transfer_service, "_continue_later"),
        ):
            transfer = transfer_service.prepare(self.project, self.source)
            result = transfer_service.run(str(transfer.id))

        transfer.reload()
        self.assertEqual(TransferStatus.DONE, transfer.status, result)
        skipped = StorageTransferObject.objects(
            transfer=transfer, status=TransferObjectStatus.SKIPPED
        )
        self.assertEqual(1, skipped.count())
        self.assertIn("resample-", skipped.first().src_key)
        original = StorageTransferObject.objects(
            transfer=transfer, kind="original"
        ).first()
        self.assertEqual(TransferObjectStatus.SOURCE_DELETED, original.status)


class StateGuardTestCase(MoeTestCase):
    """迁移进行中时，完结/清空要被守卫拒绝。"""

    def test_active_transfer_blocks_complete_and_clear(self):
        from app.modules.storage_pool import _guard_project_state
        from app.modules.storage_pool.api import TransferInProgressError

        source = StoragePoolSource(
            name="guard-source",
            endpoint="https://acc.r2.cloudflarestorage.com",
            bucket="cold",
            access_key_id="AKIAEXAMPLE1234",
            secret_access_key="secret",
        ).save()
        project = self.create_project("guard-project")
        transfer_service.prepare(project, source, TransferDirection.IN)

        for action in ("complete", "clear"):
            with self.subTest(action=action):
                with self.assertRaises(TransferInProgressError):
                    _guard_project_state(project, action)

        # 取消迁移后放行。
        StorageTransfer.objects(project=project).update(
            set__status=TransferStatus.ABORTED
        )
        _guard_project_state(project, "complete")
        _guard_project_state(project, "clear")
