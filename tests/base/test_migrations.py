import datetime
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId
from mongoengine.connection import get_db

from app.constants.file import FileType, ThumbnailStatus
from app.migrations.runner import (
    LOCK_ID,
    MIGRATION_LOCK,
    MigrationError,
    _checksum,
    pending,
    rollback,
    run_pending,
    status,
)
from app.models.file import File
from tests import MoeTestCase


class MigrationRunnerTestCase(MoeTestCase):
    def test_run_status_and_rollback_are_idempotent(self):
        db = get_db()
        # This is the production pre-migration shape: compressed field names,
        # a missing root ``f`` field, and no thumbnail lifecycle fields.
        legacy_image_id = db.file.insert_one(
            {
                "n": "legacy-image.png",
                "t": FileType.IMAGE,
                "p": ObjectId(),
                "a": [],
                "sn": "legacy-image.png",
                "dn": "",
                "sa": "",
                "md": "legacy-md5",
                "fs": 1,
                "fn": 0,
                "fo": 0,
                "fc": 0,
                "sc": 0,
                "tsc": 0,
                "csc": 0,
                "et": datetime.datetime.utcnow(),
                "v": 1,
                "ac": True,
                "ss": 0,
                "ps": 0,
                "pt": 0,
                "op": 0,
                "ft": 0,
                "sm": False,
            }
        ).inserted_id
        legacy_text_id = db.file.insert_one({"t": 3, "n": "legacy-text"}).inserted_id

        self.assertEqual(
            ["0000", "0001", "0002", "0003"],
            [migration.version for migration in pending(db)],
        )
        self.assertEqual(
            ["0000", "0001", "0002", "0003"],
            [migration.version for migration in run_pending(db)],
        )
        self.assertEqual([], run_pending(db))
        self.assertIn("file_list_v2", db.file.index_information())
        self.assertIn("file_admin_edit_time_v1", db.file.index_information())
        self.assertEqual(
            {"th": 0, "the": ""},
            {
                key: db.file.find_one({"_id": legacy_image_id})[key]
                for key in ("th", "the")
            },
        )
        self.assertNotIn("th", db.file.find_one({"_id": legacy_text_id}))
        self.assertTrue(all(item["applied"] for item in status(db)))

        # Prove the new backend can read and serialize a document migrated
        # from the real legacy shape, not just that MongoDB accepted the update.
        image = File.objects(id=legacy_image_id).first()
        self.assertIsNotNone(image)
        self.assertIsNone(image.parent)
        self.assertEqual(ThumbnailStatus.UNKNOWN, image.thumbnail_status)
        self.assertEqual("", image.thumbnail_error)
        payload = image.to_api()
        self.assertEqual(str(legacy_image_id), payload["id"])
        self.assertEqual(ThumbnailStatus.UNKNOWN, payload["thumbnail_status"])
        self.assertEqual("", payload["thumbnail_error"])

        self.assertEqual(
            ["0001", "0002", "0003"],
            [migration.version for migration in rollback(db, "0000")],
        )
        self.assertNotIn("file_list_v2", db.file.index_information())
        self.assertNotIn("file_admin_edit_time_v1", db.file.index_information())
        self.assertNotIn("th", db.file.find_one({"_id": legacy_image_id}))
        self.assertNotIn("the", db.file.find_one({"_id": legacy_image_id}))
        self.assertEqual(
            ["0001", "0002", "0003"],
            [migration.version for migration in pending(db)],
        )

    def test_reformatting_a_migration_does_not_trip_the_checksum(self):
        """校验和必须无视格式化改动。

        校验和曾经直接哈希模块源码，于是一次 ``ruff format`` 或改个注释就会让
        已应用的迁移看起来被篡改，``status`` 抛错、init 容器起不来。改为哈希
        AST 后，只有真正改变行为的编辑才会触发。
        """
        module = importlib.import_module(
            "app.migrations.versions.m0002_thumbnail_status"
        )
        original = _checksum(module)

        reformatted = SimpleNamespace()
        source = inspect.getsource(module)
        # 重排空白、加注释、改字符串引号 —— 全都不改变语义。
        cosmetic = "# an added comment\n" + source.replace(
            "def up(db):", "def  up( db ):"
        ).replace('"""', "'''", 2)
        with patch("app.migrations.runner.inspect.getsource", return_value=cosmetic):
            self.assertEqual(original, _checksum(reformatted))

        # 而真正改变行为的编辑仍然必须被发现。
        behavioural = source.replace('{"$set"', '{"$unset"')
        self.assertNotEqual(behavioural, source)
        with patch("app.migrations.runner.inspect.getsource", return_value=behavioural):
            self.assertNotEqual(original, _checksum(reformatted))

    def test_a_held_lock_fails_loudly_instead_of_reporting_success(self):
        """抢不到锁必须报错退出，不能当作"没事可做"。

        ``manage.py migrate`` 是整个 compose 栈的
        ``service_completed_successfully`` 门禁。如果这里正常返回，被 OOM 杀掉
        的上一次迁移留下的租约会让下一次启动直接放行，gunicorn 和两个 celery
        worker 就会跑在只迁移了一半的数据库上。
        """
        db = get_db()
        db[MIGRATION_LOCK].insert_one(
            {
                "_id": LOCK_ID,
                "holder": "someone-else",
                "expires_at": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
            }
        )

        with self.assertRaisesRegex(MigrationError, "migration lock"):
            run_pending(db)

        # 迁移确实没有被执行，门禁才有意义。
        self.assertEqual(
            ["0000", "0001", "0002", "0003"],
            [migration.version for migration in pending(db)],
        )

    def test_rollback_rejects_a_version_that_does_not_exist(self):
        """--to 必须是已知版本。

        版本号按字符串比较：``--to 0`` 会满足 "0000" > "0" 而把基线一起回滚，
        ``--to 1`` 则一个都不匹配却静默退出 0。两种都是危险的误操作。
        """
        db = get_db()
        run_pending(db)

        for bad_target in ("0", "1", "9999", ""):
            with self.subTest(target=bad_target):
                with self.assertRaisesRegex(MigrationError, "Unknown target version"):
                    rollback(db, bad_target)

        # 合法版本仍然正常工作。
        self.assertEqual(
            ["0002", "0003"],
            [migration.version for migration in rollback(db, "0001")],
        )
