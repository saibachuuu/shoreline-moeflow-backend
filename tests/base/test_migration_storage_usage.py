"""m0009：项目占用回填与 output 旧键兜底。

这条迁移替代了原本打算做的「一次性脚本 + 定时校准」：老数据的修正只在
部署时跑一次，之后只靠增量记账。
"""

from bson import ObjectId
from mongoengine.connection import get_db

from app.migrations.versions import m0009_storage_usage_backfill as migration
from tests import MoeTestCase


class StorageUsageBackfillTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.db = get_db()

    def test_backfills_only_zero_projects(self):
        zero = ObjectId()
        kept = ObjectId()
        self.db.project.insert_many(
            [
                {"_id": zero, "fs": 0, "n": "zero"},
                {"_id": kept, "fs": 999, "n": "kept"},
            ]
        )
        self.db.file.insert_many(
            [
                {"_id": ObjectId(), "p": zero, "fs": 100},
                {"_id": ObjectId(), "p": zero, "fs": 50},
                {"_id": ObjectId(), "p": kept, "fs": 123},
            ]
        )

        migration.up(self.db)

        self.assertEqual(150, self.db.project.find_one({"_id": zero})["fs"])
        self.assertEqual(
            999, self.db.project.find_one({"_id": kept})["fs"], "已有值一个都不碰"
        )
        self.assertTrue(migration.verify(self.db))

    def test_missing_file_size_is_filled(self):
        project = ObjectId()
        self.db.project.insert_one({"_id": project, "n": "missing"})
        self.db.file.insert_one({"_id": ObjectId(), "p": project, "fs": 7})

        migration.up(self.db)

        self.assertEqual(7, self.db.project.find_one({"_id": project})["fs"])

    def test_legacy_fn_key_is_renamed_without_clobbering(self):
        self.db.output.insert_many(
            [
                {"_id": ObjectId(), "fn": "legacy.zip"},
                # 目标键已存在：不能覆盖真实文件名
                {"_id": ObjectId(), "fn": "old.zip", "file_name": "keep.zip"},
            ]
        )

        migration.up(self.db)

        self.assertEqual(0, self.db.output.count_documents({"fn": {"$exists": True}}))
        names = sorted(document["file_name"] for document in self.db.output.find({}))
        self.assertEqual(["keep.zip", "legacy.zip"], names)

    def test_up_is_idempotent(self):
        project = ObjectId()
        self.db.project.insert_one({"_id": project, "fs": 0})
        self.db.file.insert_one({"_id": ObjectId(), "p": project, "fs": 42})

        migration.up(self.db)
        migration.up(self.db)

        self.assertEqual(42, self.db.project.find_one({"_id": project})["fs"])

    def test_verify_detects_missing_backfill(self):
        project = ObjectId()
        self.db.project.insert_one({"_id": project, "fs": 0})
        self.db.file.insert_one({"_id": ObjectId(), "p": project, "fs": 42})

        self.assertFalse(migration.verify(self.db))

    def test_empty_database_is_fine(self):
        migration.up(self.db)
        self.assertTrue(migration.verify(self.db))
