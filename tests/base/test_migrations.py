import datetime
import hashlib
import importlib
import inspect
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from bson import DBRef, ObjectId
from mongoengine.connection import get_db

from app.constants.file import FileType, ThumbnailStatus
from app.migrations.runner import (
    LOCK_ID,
    MIGRATION_LOCK,
    IrreversibleMigration,
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
    def test_search_projection_backfill_writes_bounded_batches(self):
        from app.migrations.versions import m0006_search_projections

        class Collection:
            def __init__(self):
                self.batch_sizes = []

            def bulk_write(self, operations, ordered=False):
                self.batch_sizes.append(len(operations))

        collection = Collection()
        m0006_search_projections._write_batches(
            collection,
            ((ObjectId(), {"$set": {"ns": str(index)}}) for index in range(1001)),
        )
        self.assertEqual([500, 500, 1], collection.batch_sizes)

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
            ["0000", "0001", "0002", "0003", "0004", "0005", "0006"],
            [migration.version for migration in pending(db)],
        )
        self.assertEqual(
            ["0000", "0001", "0002", "0003", "0004", "0005", "0006"],
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
        self.assertEqual(
            1,
            db.identity_migration_report.count_documents({"_id": "identity-v1-summary"}),
        )

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

        with self.assertRaises(IrreversibleMigration):
            rollback(db, "0000")
        self.assertEqual(0, db.project.count_documents({"w": {"$exists": True}}))

    def test_identity_migration_backfills_relations_workers_and_is_reentrant(self):
        db = get_db()
        team_id = ObjectId()
        project_id = ObjectId()
        creator_id = ObjectId()
        translator_id = ObjectId()
        creator_role_id = ObjectId()
        coordinator_role_id = ObjectId()
        team_creator_role_id = ObjectId()
        team_member_role_id = ObjectId()

        db.user.insert_many(
            [
                {
                    "_id": creator_id,
                    "n": f"Creator-{creator_id}",
                    "e": f"creator-{creator_id}@example.com",
                },
                {
                    "_id": translator_id,
                    "n": f"Translator-{translator_id}",
                    "e": f"translator-{translator_id}@example.com",
                },
            ]
        )
        db.team.insert_one(
            {"_id": team_id, "n": f"migration-team-{team_id}", "m_uc": 99}
        )
        db.project.insert_one(
            {
                "_id": project_id,
                "n": "migration-project",
                "t": team_id,
                "st": 2,
                "w": '{"翻译": ["外部人员", "外部人员"], "unknown": ["丢失"]}',
            }
        )
        db.project_role.insert_many(
            [
                {"_id": creator_role_id, "m_o": "creator"},
                {"_id": coordinator_role_id, "m_o": "coordinator"},
            ]
        )
        db.team_role.insert_many(
            [
                {"_id": team_creator_role_id, "m_o": "creator"},
                {"_id": team_member_role_id, "m_o": "beginner"},
            ]
        )
        db.project_user_relation.insert_many(
            [
                {"u": creator_id, "g": project_id, "r": creator_role_id},
                {
                    "u": translator_id,
                    "g": project_id,
                    "r": coordinator_role_id,
                    "m_t": ["legacy-label"],
                },
            ]
        )
        db.team_user_relation.insert_many(
            [
                {"u": creator_id, "g": team_id, "r": team_creator_role_id},
                {"u": translator_id, "g": team_id, "r": team_member_role_id},
            ]
        )

        run_pending(db)

        creator_member = db.project_member.find_one(
            {"ik": f"{project_id}:u:{creator_id}"}
        )
        translator_member = db.project_member.find_one(
            {"ik": f"{project_id}:u:{translator_id}"}
        )
        self.assertEqual(["creator"], creator_member["tags"])
        self.assertEqual(["proofreader"], translator_member["tags"])
        self.assertNotIn("legacy-label", translator_member["tags"])
        external = db.project_member.find_one(
            {"project": project_id, "display_name": "外部人员"}
        )
        self.assertEqual(["translator"], external["tags"])
        self.assertEqual(3, db.project_member.count_documents({"project": project_id}))
        migrated_project = db.project.find_one({"_id": project_id})
        self.assertEqual("migration-project", migrated_project["ns"])
        creator_user = db.user.find_one({"_id": creator_id})
        self.assertEqual(creator_user["n"].casefold(), creator_user["ns"])
        external_member = db.project_member.find_one(
            {"project": project_id, "external_id": {"$exists": True}}
        )
        self.assertEqual(
            external_member["display_name"], external_member["dns"]
        )
        self.assertIn("project_team_set_status_edit_v1", db.project.index_information())
        self.assertIn("project_member_display_name_search_v1", db.project_member.index_information())
        self.assertEqual(
            "NORMAL", {0: "NORMAL", 1: "CLEARED", 5: "COMPLETED"}[db.project.find_one({"_id": project_id})["st"]]
        )
        self.assertEqual(creator_id, db.project.find_one({"_id": project_id})["ou"])
        self.assertNotIn("w", db.project.find_one({"_id": project_id}))
        self.assertEqual(
            "creator",
            db.team_member.find_one({"team": team_id, "user": creator_id})["base_tag"],
        )
        self.assertEqual(
            1,
            db.identity_migration_report.count_documents(
                {"_id": f"project:{project_id}"}
            ),
        )
        report = db.identity_migration_report.find_one({"_id": f"project:{project_id}"})
        self.assertTrue(any(item["code"] == "unknown_worker_tag" for item in report["issues"]))
        relation_report = next(
            item
            for item in db.identity_migration_report.find(
                {"scope": "project_relation", "legacy_tags": ["legacy-label"]}
            )
        )
        self.assertEqual("coordinator", relation_report["role"]["system_code"])
        self.assertTrue(
            any(item["code"] == "legacy_relation_tags" for item in relation_report["issues"])
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(1, summary["external_members"])
        self.assertEqual(2, summary["worker_values"])
        self.assertEqual(1, summary["owner_repaired_count"])
        self.assertNotIn("raw_workers", report)
        self.assertEqual(64, len(report["raw_workers_sha256"]))

        before = {
            "project_members": db.project_member.count_documents({"project": project_id}),
            "team_members": db.team_member.count_documents({"team": team_id}),
        }
        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)
        self.assertEqual(before["project_members"], db.project_member.count_documents({"project": project_id}))
        self.assertEqual(before["team_members"], db.team_member.count_documents({"team": team_id}))

    def test_identity_migration_reports_missing_owner_and_blocks_verification(self):
        db = get_db()
        # Since the owner fallback (team creator, then site creator) repairs
        # creator-less projects automatically, the ``owner_missing`` guard is
        # only reachable when no fallback owner exists at all -- i.e. the site
        # has no users.  Remove the seeded users to exercise that guard.
        db.user.delete_many({})
        project_id = ObjectId()
        db.project.insert_one({"_id": project_id, "n": "ownerless", "st": 0})

        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)

        report = db.identity_migration_report.find_one({"_id": f"project:{project_id}"})
        self.assertTrue(any(item["code"] == "owner_missing" for item in report["issues"]))
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(1, summary["owner_missing_count"])
        self.assertFalse(m0004_identity_members.verify(db))

    def test_identity_migration_writes_complete_external_artifact_bundle(self):
        db = get_db()
        from app.migrations.versions import m0004_identity_members

        team_id = ObjectId()
        project_id = ObjectId()
        user_id = ObjectId()
        creator_role_id = ObjectId()
        translator_role_id = ObjectId()
        team_creator_role_id = ObjectId()
        project_relation_id = ObjectId()
        team_relation_id = ObjectId()

        db.user.insert_one(
            {
                "_id": user_id,
                "n": "artifact-user",
                "e": "artifact-user@example.com",
                "p": "password-must-not-be-exported",
            }
        )
        db.team.insert_one({"_id": team_id, "n": "artifact-team"})
        db.project.insert_one(
            {
                "_id": project_id,
                "n": "artifact-project",
                "t": team_id,
                "st": 0,
                "w": json.dumps({"翻译": ["External Worker"]}),
            }
        )
        db.project_role.insert_many(
            [
                {"_id": creator_role_id, "m_o": "creator", "m_p": ["legacy-owner"]},
                {"_id": translator_role_id, "m_o": "translator", "m_p": ["legacy-translate"]},
            ]
        )
        db.team_role.insert_one({"_id": team_creator_role_id, "m_o": "creator"})
        db.project_user_relation.insert_many(
            [
                {
                    "_id": project_relation_id,
                    "u": user_id,
                    "g": project_id,
                    "r": creator_role_id,
                },
                {
                    "u": user_id,
                    "g": project_id,
                    "r": translator_role_id,
                    "m_t": ["legacy-annotation"],
                },
            ]
        )
        db.team_user_relation.insert_one(
            {
                "_id": team_relation_id,
                "u": user_id,
                "g": team_id,
                "r": team_creator_role_id,
            }
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            with patch.dict(
                os.environ,
                {
                    "IDENTITY_MIGRATION_ARTIFACT_DIR": artifact_root,
                    "IDENTITY_MIGRATION_BATCH_ID": "test-artifact-batch",
                },
                clear=False,
            ):
                m0004_identity_members.up(db)
                project_member_count = db.project_member.count_documents({})
                team_member_count = db.team_member.count_documents({})
                m0004_identity_members.up(db)

            bundle = os.path.join(artifact_root, "test-artifact-batch")
            manifest_path = os.path.join(bundle, "manifest.json")
            checksums_path = os.path.join(bundle, "checksums.json")
            self.assertTrue(os.path.isfile(manifest_path))
            self.assertTrue(os.path.isfile(checksums_path))

            with open(manifest_path, encoding="utf-8") as stream:
                manifest = json.load(stream)
            with open(checksums_path, encoding="utf-8") as stream:
                checksums = json.load(stream)

            self.assertEqual("test-artifact-batch", manifest["batch_id"])
            self.assertEqual(
                set(m0004_identity_members.ARTIFACT_FILES),
                set(manifest["files"]),
            )
            self.assertEqual(
                "test-artifact-batch", manifest["summary"]["batch_id"]
            )
            self.assertEqual(project_member_count, db.project_member.count_documents({}))
            self.assertEqual(team_member_count, db.team_member.count_documents({}))

            for name in (*m0004_identity_members.ARTIFACT_FILES, "manifest.json"):
                path = os.path.join(bundle, name)
                self.assertTrue(os.path.isfile(path), name)
                with open(path, "rb") as stream:
                    self.assertEqual(
                        checksums["files"][name],
                        hashlib.sha256(stream.read()).hexdigest(),
                    )

            rows = {}
            for name in m0004_identity_members.ARTIFACT_FILES:
                path = os.path.join(bundle, name)
                with open(path, encoding="utf-8") as stream:
                    rows[name] = [json.loads(line) for line in stream if line.strip()]
                self.assertTrue(
                    all(row["batch_id"] == "test-artifact-batch" for row in rows[name])
                )

            project_rows = rows["project-members.jsonl"]
            team_rows = rows["team-members.jsonl"]
            self.assertTrue(all("before" in row and "after" in row for row in project_rows))
            self.assertTrue(all("before" in row and "after" in row for row in team_rows))
            self.assertTrue(
                any(
                    project_relation_id.__str__() in row["source_ids"]
                    for row in project_rows
                )
            )
            self.assertTrue(
                any(
                    team_relation_id.__str__() in row["source_ids"]
                    for row in team_rows
                )
            )
            self.assertTrue(rows["role-issues.jsonl"])
            self.assertTrue(rows["permission-diff.jsonl"])
            self.assertTrue(rows["workers-audit.jsonl"])

            serialized_parts = []
            for name in (*m0004_identity_members.ARTIFACT_FILES, "manifest.json"):
                with open(os.path.join(bundle, name), encoding="utf-8") as stream:
                    serialized_parts.append(stream.read())
            serialized = "".join(serialized_parts).lower()
            self.assertNotIn("@example.com", serialized)
            self.assertNotIn("password-must-not-be-exported", serialized)
            self.assertNotIn("token", serialized)

    def test_identity_migration_normalizes_planned_statuses_and_cleans_legacy_fields(self):
        db = get_db()
        planned_finish_id = ObjectId()
        planned_delete_id = ObjectId()
        completed_id = ObjectId()
        cleared_id = ObjectId()
        db.project.insert_many(
            [
                {
                    "_id": planned_finish_id,
                    "n": "planned-finish",
                    "st": 2,
                    "pft": datetime.datetime.utcnow(),
                    "ft": datetime.datetime.utcnow(),
                    "w": json.dumps({"翻译": ["Translator"]}),
                },
                {
                    "_id": planned_delete_id,
                    "n": "planned-delete",
                    "st": 3,
                    "pdt": datetime.datetime.utcnow(),
                    "ft": datetime.datetime.utcnow(),
                },
                {
                    "_id": completed_id,
                    "n": "completed",
                    "st": 5,
                    "ctm": datetime.datetime.utcnow(),
                    "ft": datetime.datetime.utcnow(),
                },
                {
                    "_id": cleared_id,
                    "n": "cleared",
                    "st": 1,
                    "ft": datetime.datetime.utcnow(),
                },
            ]
        )

        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)

        self.assertEqual(0, db.project.find_one({"_id": planned_finish_id})["st"])
        self.assertEqual(0, db.project.find_one({"_id": planned_delete_id})["st"])
        self.assertEqual(5, db.project.find_one({"_id": completed_id})["st"])
        self.assertEqual(1, db.project.find_one({"_id": cleared_id})["st"])
        for project in db.project.find({}):
            for field in ("w", "pft", "pdt", "ft"):
                self.assertNotIn(field, project)

        finish_report = db.identity_migration_report.find_one(
            {"_id": f"project:{planned_finish_id}"}
        )
        delete_report = db.identity_migration_report.find_one(
            {"_id": f"project:{planned_delete_id}"}
        )
        self.assertTrue(
            any(item["code"] == "legacy_finish_plan" for item in finish_report["issues"])
        )
        self.assertTrue(
            any(item["code"] == "legacy_delete_plan" for item in delete_report["issues"])
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(2, summary["status_plan_count"])
        self.assertEqual(1, summary["workers_projects"])

    def test_identity_migration_reports_owner_conflicts_and_invalid_owner(self):
        db = get_db()
        creator_one = ObjectId()
        creator_two = ObjectId()
        invalid_owner = ObjectId()
        conflict_project = ObjectId()
        invalid_project = ObjectId()
        conflict_role = ObjectId()
        db.user.insert_many(
            [
                {"_id": creator_one, "n": "creator-one", "e": "creator-one@test.com"},
                {"_id": creator_two, "n": "creator-two", "e": "creator-two@test.com"},
            ]
        )
        db.project.insert_many(
            [
                {"_id": conflict_project, "n": "owner-conflict", "st": 0},
                {"_id": invalid_project, "n": "owner-invalid", "st": 0, "ou": invalid_owner},
            ]
        )
        db.project_role.insert_one({"_id": conflict_role, "m_o": "creator"})
        db.project_user_relation.insert_many(
            [
                {"u": creator_one, "g": conflict_project, "r": conflict_role},
                {"u": creator_two, "g": conflict_project, "r": conflict_role},
            ]
        )

        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)

        conflict_report = db.identity_migration_report.find_one(
            {"_id": f"project:{conflict_project}"}
        )
        invalid_report = db.identity_migration_report.find_one(
            {"_id": f"project:{invalid_project}"}
        )
        self.assertTrue(
            any(item["code"] == "owner_candidate_conflict" for item in conflict_report["issues"])
        )
        self.assertTrue(
            any(item["code"] == "owner_invalid_user" for item in invalid_report["issues"])
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(1, summary["owner_conflict_count"])
        self.assertEqual(1, summary["owner_invalid_count"])
        self.assertFalse(m0004_identity_members.verify(db))

    def test_identity_migration_merges_duplicate_relations_without_legacy_tags(self):
        db = get_db()
        team_id = ObjectId()
        project_id = ObjectId()
        user_id = ObjectId()
        project_translator_role = ObjectId()
        project_proofreader_role = ObjectId()
        team_admin_role = ObjectId()
        team_member_role = ObjectId()
        project_relation_one = ObjectId()
        project_relation_two = ObjectId()
        team_relation_one = ObjectId()
        team_relation_two = ObjectId()
        db.user.insert_one({"_id": user_id, "n": "duplicate-user"})
        db.team.insert_one({"_id": team_id, "n": "duplicate-team"})
        db.project.insert_one(
            {"_id": project_id, "n": "duplicate-project", "t": team_id, "st": 0}
        )
        db.project_role.insert_many(
            [
                {"_id": project_translator_role, "m_o": "translator"},
                {"_id": project_proofreader_role, "m_o": "proofreader"},
            ]
        )
        db.team_role.insert_many(
            [
                {"_id": team_admin_role, "m_o": "admin"},
                {"_id": team_member_role, "m_o": "member"},
            ]
        )
        db.project_user_relation.insert_many(
            [
                {
                    "_id": project_relation_one,
                    "u": user_id,
                    "g": project_id,
                    "r": project_translator_role,
                    "m_t": ["old-project-annotation"],
                },
                {
                    "_id": project_relation_two,
                    "u": user_id,
                    "g": project_id,
                    "r": project_proofreader_role,
                    "m_t": ["old-project-annotation-two"],
                },
            ]
        )
        db.team_user_relation.insert_many(
            [
                {
                    "_id": team_relation_one,
                    "u": user_id,
                    "g": team_id,
                    "r": team_member_role,
                    "m_t": ["old-team-annotation"],
                },
                {
                    "_id": team_relation_two,
                    "u": user_id,
                    "g": team_id,
                    "r": team_admin_role,
                    "m_t": ["old-team-annotation-two"],
                },
            ]
        )

        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)

        project_member = db.project_member.find_one(
            {"project": project_id, "user": user_id}
        )
        team_member = db.team_member.find_one({"team": team_id})
        self.assertEqual(["proofreader", "translator"], project_member["tags"])
        self.assertNotIn("old-project-annotation", project_member["tags"])
        self.assertEqual("admin", team_member["base_tag"])
        self.assertEqual([], team_member["tags"])
        self.assertNotIn("relation_ids", team_member)
        # The project has no creator relation and its team has no creator
        # either, so the owner fallback binds the site creator and writes a
        # creator member row.
        self.assertEqual(2, db.project_member.count_documents({"project": project_id}))
        self.assertIsNotNone(
            db.project_member.find_one({"project": project_id, "tags": ["creator"]})
        )
        self.assertEqual(1, db.team_member.count_documents({"team": team_id}))
        self.assertEqual(
            2,
            db.identity_migration_report.count_documents(
                {"scope": "project_relation", "legacy_tags": {"$exists": True}}
            ),
        )
        self.assertEqual(
            2,
            db.identity_migration_report.count_documents(
                {"scope": "team_relation", "legacy_tags": {"$exists": True}}
            ),
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(1, summary["project_relation_duplicate_count"])
        self.assertEqual(1, summary["team_relation_duplicate_count"])

    def test_identity_migration_audits_workers_shapes_values_and_duplicate_names(self):
        db = get_db()
        project_ids = [ObjectId() for _ in range(5)]
        workers_values = [
            "[\"root-array\"]",
            "{\"translator\":",
            json.dumps({"translator": "not-an-array"}),
            json.dumps(
                {
                    "translator": [None, "", 7, " Alice ", "alice"],
                    "unknown": ["ignored"],
                }
            ),
            json.dumps({"翻译": ["Bob", " bob "], "校对": ["BOB"]}),
        ]
        db.project.insert_many(
            [
                {"_id": project_id, "n": f"workers-{index}", "st": 0, "w": workers}
                for index, (project_id, workers) in enumerate(zip(project_ids, workers_values))
            ]
        )

        from app.migrations.versions import m0004_identity_members

        m0004_identity_members.up(db)

        expected_issue_codes = [
            "workers_root_not_object",
            "workers_parse_error",
            "worker_names_not_array",
            "invalid_worker_name",
        ]
        for project_id, code in zip(project_ids, expected_issue_codes):
            report = db.identity_migration_report.find_one(
                {"_id": f"project:{project_id}"}
            )
            self.assertTrue(any(item["code"] == code for item in report["issues"]))
            self.assertNotIn("raw_workers", report)
            self.assertEqual(64, len(report["raw_workers_sha256"]))

        invalid_values_report = db.identity_migration_report.find_one(
            {"_id": f"project:{project_ids[3]}"}
        )
        self.assertTrue(
            any(item["code"] == "unknown_worker_tag" for item in invalid_values_report["issues"])
        )
        duplicate_member = db.project_member.find_one(
            {"project": project_ids[4], "display_name": "Bob"}
        )
        self.assertEqual(["proofreader", "translator"], duplicate_member["tags"])
        # Each workers-only project has no creator, so the owner fallback adds
        # the site creator as an extra creator member.
        self.assertEqual(2, db.project_member.count_documents({"project": project_ids[4]}))
        self.assertEqual(
            2,
            db.project_member.count_documents({"project": project_ids[3]}),
        )
        for project in db.project.find({"_id": {"$in": project_ids}}):
            self.assertNotIn("w", project)
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(5, summary["workers_projects"])
        self.assertEqual(5, summary["worker_values"])

    def test_identity_migration_projects_all_join_process_states_and_repairs_counts(self):
        db = get_db()
        from app.migrations.versions import m0004_identity_members

        def object_id(index):
            return ObjectId(f"64{index:022x}")

        team_id = object_id(100)
        project_id = object_id(101)
        creator_id = object_id(1)
        invitation_pending_id = object_id(10)
        invitation_allowed_id = object_id(11)
        invitation_denied_id = object_id(12)
        application_pending_id = object_id(20)
        application_allowed_id = object_id(21)
        application_denied_id = object_id(22)
        direct_id = object_id(30)
        team_invitation_id = object_id(40)
        team_application_id = object_id(41)
        user_ids = [
            creator_id,
            object_id(2),
            object_id(3),
            object_id(4),
            object_id(5),
            object_id(6),
            object_id(7),
            object_id(8),
            object_id(9),
            object_id(13),
        ]
        db.user.insert_many(
            [
                {
                    "_id": user_id,
                    "n": f"join-state-user-{index}",
                    "e": f"join-state-user-{index}@example.com",
                }
                for index, user_id in enumerate(user_ids)
            ]
        )
        db.team.insert_one({"_id": team_id, "n": "join-state-team", "m_uc": 999})
        db.project.insert_one(
            {
                "_id": project_id,
                "n": "join-state-project",
                "t": team_id,
                "st": 0,
                "m_uc": 999,
            }
        )
        creator_role_id = object_id(200)
        translator_role_id = object_id(201)
        team_member_role_id = object_id(202)
        db.project_role.insert_many(
            [
                {"_id": creator_role_id, "m_o": "creator", "m_p": []},
                {"_id": translator_role_id, "m_o": "translator", "m_p": []},
            ]
        )
        db.team_role.insert_one({"_id": team_member_role_id, "m_o": "member", "m_p": []})
        db.project_user_relation.insert_many(
            [
                {"_id": object_id(300), "u": creator_id, "g": project_id, "r": creator_role_id},
                {"_id": direct_id, "u": user_ids[6], "g": project_id, "r": translator_role_id},
            ]
        )
        db.team_user_relation.insert_one(
            {"_id": object_id(301), "u": creator_id, "g": team_id, "r": team_member_role_id}
        )
        event_time = datetime.datetime(2024, 1, 1)
        db.invitation.insert_many(
            [
                {
                    "_id": invitation_pending_id,
                    "u": user_ids[1],
                    "o": creator_id,
                    "g": project_id,
                    "r": translator_role_id,
                    "s": 1,
                    "c": event_time,
                },
                {
                    "_id": invitation_allowed_id,
                    "u": user_ids[2],
                    "o": creator_id,
                    "g": project_id,
                    "r": translator_role_id,
                    "s": 2,
                    "c": event_time + datetime.timedelta(days=1),
                },
                {
                    "_id": invitation_denied_id,
                    "u": user_ids[3],
                    "o": creator_id,
                    "g": project_id,
                    "r": translator_role_id,
                    "s": 3,
                    "c": event_time + datetime.timedelta(days=2),
                },
                {
                    "_id": team_invitation_id,
                    "u": user_ids[7],
                    "o": creator_id,
                    "g": team_id,
                    "r": team_member_role_id,
                    "s": 2,
                    "c": event_time,
                },
            ]
        )
        db.application.insert_many(
            [
                {"_id": application_pending_id, "u": user_ids[4], "g": project_id, "s": 1},
                {
                    "_id": application_allowed_id,
                    "u": user_ids[5],
                    "g": project_id,
                    "s": 2,
                    # Some historical Application documents have no create
                    # timestamp but do retain an edit timestamp.
                    "m_e": event_time + datetime.timedelta(days=3),
                },
                {"_id": application_denied_id, "u": user_ids[9], "g": project_id, "s": 3},
                {"_id": team_application_id, "u": user_ids[8], "g": team_id, "s": 1},
            ]
        )

        m0004_identity_members.up(db)

        members = {
            member["user"]: member
            for member in db.project_member.find({"project": project_id})
        }
        self.assertEqual(
            {creator_id, user_ids[1], user_ids[2], user_ids[3], user_ids[5], user_ids[6], user_ids[9]},
            set(members),
        )
        self.assertEqual("invited", members[user_ids[1]]["status"])
        self.assertEqual("active", members[user_ids[2]]["status"])
        self.assertEqual("removed", members[user_ids[3]]["status"])
        self.assertNotIn(user_ids[4], members)
        self.assertEqual("active", members[user_ids[5]]["status"])
        self.assertEqual(
            event_time + datetime.timedelta(days=3),
            members[user_ids[5]]["create_time"],
        )
        self.assertEqual("active", members[user_ids[6]]["status"])
        self.assertEqual("removed", members[user_ids[9]]["status"])
        self.assertEqual(["creator"], members[creator_id]["tags"])
        self.assertEqual(["translator"], members[user_ids[6]]["tags"])
        self.assertEqual(4, db.project.find_one({"_id": project_id})["m_uc"])
        self.assertEqual(4, db.project_member.count_documents({"project": project_id, "status": "active"}))
        self.assertEqual(1, db.team_member.count_documents({"team": team_id, "status": "active"}))
        self.assertEqual(1, db.team.find_one({"_id": team_id})["m_uc"])
        self.assertEqual(creator_id, db.project.find_one({"_id": project_id})["ou"])

        def report(scope, source_id):
            return db.identity_migration_report.find_one({"_id": f"{scope}:{source_id}"})

        self.assertEqual("project_member_invited", report("invitation", invitation_pending_id)["projection"])
        self.assertEqual("project_member_active", report("invitation", invitation_allowed_id)["projection"])
        self.assertEqual("project_member_removed", report("invitation", invitation_denied_id)["projection"])
        self.assertEqual("pending_application_no_member", report("application", application_pending_id)["projection"])
        self.assertEqual("project_member_active", report("application", application_allowed_id)["projection"])
        self.assertEqual("project_member_removed", report("application", application_denied_id)["projection"])
        self.assertEqual("team_scope_no_project_member", report("invitation", team_invitation_id)["projection"])
        self.assertEqual("team_scope_no_project_member", report("application", team_application_id)["projection"])

        summary = db.identity_migration_report.find_one({"_id": "identity-v1-summary"})["summary"]
        self.assertEqual(4, summary["invitation_count"])
        self.assertEqual(4, summary["application_count"])
        self.assertEqual(1, summary["pending_invitation_count"])
        self.assertEqual(2, summary["allowed_invitation_count"])
        self.assertEqual(1, summary["denied_invitation_count"])
        self.assertEqual(2, summary["pending_application_count"])
        self.assertEqual(1, summary["allowed_application_count"])
        self.assertEqual(1, summary["denied_application_count"])
        self.assertEqual(5, summary["project_members_from_join_process"])
        self.assertTrue(m0004_identity_members.verify(db))

    def test_identity_migration_binds_missing_owner_to_team_creator(self):
        """Projects without any creator relation must not block the migration:
        the owner is bound to the project team's creator (with a creator member
        row) so verification passes.  Production ``application``/``invitation``
        documents store GenericReferenceField values as ``{"_cls": ...,
        "_ref": DBRef}`` (and text ``{"$id": ...}`` objects after JSON
        round-trips) instead of bare ObjectIds, so those shapes are exercised
        here too."""
        db = get_db()
        project_id = ObjectId()
        team_id = ObjectId()
        creator_id = ObjectId()
        invited_user = ObjectId()
        applicant_user = ObjectId()
        invitation_id = ObjectId()
        application_id = ObjectId()
        team_creator_role_id = ObjectId()
        db.user.insert_many(
            [
                {"_id": creator_id, "n": "team-creator", "e": "creator@example.com"},
                {"_id": invited_user, "n": "invited-user", "e": "invited@example.com"},
                {"_id": applicant_user, "n": "applicant-user", "e": "applicant@example.com"},
            ]
        )
        db.team.insert_one({"_id": team_id, "n": "generic-ref-team", "m_uc": 1})
        db.team_role.insert_one({"_id": team_creator_role_id, "m_o": "creator"})
        # The team has a creator relation, but the project itself has NONE --
        # the exact shape that used to trip ``owner_missing``.
        db.team_user_relation.insert_one(
            {"u": creator_id, "g": team_id, "r": team_creator_role_id}
        )
        db.project.insert_one(
            {
                "_id": project_id,
                "n": "generic-ref-project",
                "t": team_id,
                "st": 2,
                "w": "{}",
            }
        )
        db.invitation.insert_one(
            {
                "_id": invitation_id,
                "g": {"_cls": "Project", "_ref": DBRef("project", project_id)},
                "u": {"_cls": "User", "_ref": DBRef("user", invited_user)},
                "s": 2,
            }
        )
        db.application.insert_one(
            {
                "_id": application_id,
                "g": {
                    "_cls": "Project",
                    "_ref": DBRef("project", project_id),
                },
                "u": {
                    "_cls": "User",
                    "_ref": DBRef("user", applicant_user),
                },
                "s": 1,
            }
        )

        run_pending(db)
        self.assertEqual([], run_pending(db))

        project = db.project.find_one({"_id": project_id})
        self.assertEqual(creator_id, project["ou"])
        creator_member = db.project_member.find_one(
            {"ik": f"{project_id}:u:{creator_id}"}
        )
        self.assertEqual(["creator"], sorted(creator_member["tags"]))
        invitation_report = db.identity_migration_report.find_one(
            {"_id": f"invitation:{invitation_id}"}
        )
        self.assertEqual("project_member_active", invitation_report["projection"])
        application_report = db.identity_migration_report.find_one(
            {"_id": f"application:{application_id}"}
        )
        self.assertEqual("pending_application_no_member", application_report["projection"])
        project_report = db.identity_migration_report.find_one(
            {"_id": f"project:{project_id}"}
        )
        self.assertTrue(
            any(
                item["code"] == "owner_bound_to_team_creator"
                for item in project_report["issues"]
            )
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(0, summary["owner_issue_count"])
        self.assertEqual(1, summary["owner_repaired_count"])
        self.assertEqual(1, summary["allowed_invitation_count"])
        self.assertEqual(1, summary["pending_application_count"])
        from app.migrations.versions import m0004_identity_members

        self.assertTrue(m0004_identity_members.verify(db))

    def test_identity_migration_binds_missing_owner_to_site_creator(self):
        """When neither the project nor its team has a creator relation, the
        earliest user (site creator) becomes the owner so verification can
        still pass."""
        db = get_db()
        site_creator_id = ObjectId("000000000000000000000001")
        later_user_id = ObjectId()
        project_id = ObjectId()
        team_id = ObjectId()
        db.user.insert_many(
            [
                {"_id": site_creator_id, "n": "site-creator", "e": "site@example.com"},
                {"_id": later_user_id, "n": "later-user", "e": "later@example.com"},
            ]
        )
        db.team.insert_one({"_id": team_id, "n": "creator-less-team", "m_uc": 0})
        db.project.insert_one(
            {
                "_id": project_id,
                "n": "creator-less-project",
                "t": team_id,
                "st": 2,
                "w": "{}",
            }
        )

        run_pending(db)
        self.assertEqual([], run_pending(db))

        project = db.project.find_one({"_id": project_id})
        self.assertEqual(site_creator_id, project["ou"])
        member = db.project_member.find_one(
            {"ik": f"{project_id}:u:{site_creator_id}"}
        )
        self.assertEqual(["creator"], sorted(member["tags"]))
        project_report = db.identity_migration_report.find_one(
            {"_id": f"project:{project_id}"}
        )
        self.assertTrue(
            any(
                item["code"] == "owner_bound_to_site_creator"
                for item in project_report["issues"]
            )
        )
        summary = db.identity_migration_report.find_one(
            {"_id": "identity-v1-summary"}
        )["summary"]
        self.assertEqual(0, summary["owner_issue_count"])
        self.assertEqual(1, summary["owner_repaired_count"])
        from app.migrations.versions import m0004_identity_members

        self.assertTrue(m0004_identity_members.verify(db))

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

    def test_old_source_checksum_records_remain_verifiable(self):
        db = get_db()
        module = importlib.import_module(
            "app.migrations.versions.m0002_thumbnail_status"
        )
        db.migration_record.insert_one(
            {
                "v": "0002",
                "c": hashlib.sha256(inspect.getsource(module).encode("utf-8")).hexdigest(),
            }
        )
        self.assertTrue(all(item["version"] for item in status(db)))

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
            ["0000", "0001", "0002", "0003", "0004", "0005", "0006"],
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
        with self.assertRaises(IrreversibleMigration):
            rollback(db, "0001")

    def test_legacy_checksum_mismatch_warns_but_remains_accepted(self):
        """0004 之前的记录不是按规范 checksum 落库的。

        无法复现的旧记录仍然要放行（否则历史环境升级会被误判为篡改），但必须
        通过日志暴露出来，而不是静默吞掉——这正是本次修复补上的告警。
        """
        from app.migrations.runner import _checksum_matches, discover

        module = discover()[0].module
        self.assertLess(module.VERSION, "0004")
        with patch("app.migrations.runner.logger") as logger_mock:
            accepted = _checksum_matches(module, "legacy-record-checksum", module.VERSION)
        self.assertTrue(accepted)
        logger_mock.warning.assert_called_once()

    def test_lock_lease_loss_raises_instead_of_letting_two_runners_continue(self):
        """租约丢失后继续跑等于和接管的 runner 并发执行同一批 up()。"""
        from app.migrations.runner import LOCK_LEASE, MigrationError, _acquire_lock, _renew_lock

        db = get_db()
        self.assertTrue(_acquire_lock(db, "lease-holder-a", LOCK_LEASE))
        # 另一个进程的续约条件匹配不到 holder（已被接管），必须抛错。
        with self.assertRaises(MigrationError):
            _renew_lock(db, "lease-holder-b", LOCK_LEASE)
        db[MIGRATION_LOCK].delete_one({"_id": LOCK_ID})

    def test_m0004_preserves_invalid_subject_documents_instead_of_deleting(self):
        """不可逆迁移不该物理删除可能是唯一线索的残缺文档。"""
        from app.migrations.versions import m0004_identity_members as m0004

        db = get_db()
        scratch = "identity_migration_scratch"
        db.drop_collection(scratch)
        db[scratch].insert_one(
            {"external_id": "orphan", "display_name": "无项目文档", "status": "active"}
        )
        reports = {}
        summary = {
            "identity_member_reported_invalid_count": 0,
            "identity_member_repaired_count": 0,
        }
        m0004._merge_existing_documents(db, reports, summary, scratch)
        self.assertEqual(1, summary["identity_member_reported_invalid_count"])
        self.assertEqual(0, summary["identity_member_repaired_count"])
        self.assertEqual(1, db[scratch].count_documents({"external_id": "orphan"}))
        self.assertTrue(
            any(
                issue.get("code") == "invalid_identity_member"
                for report in reports.values()
                for issue in report.get("issues", [])
            )
        )
        db.drop_collection(scratch)

    def test_m0004_verify_detects_overlong_display_names_and_count_mismatch(self):
        """verify 必须挡住超过运行时上限的 display_name 和与 active 数不一致的 m_uc。"""
        from app.migrations.versions import m0004_identity_members as m0004

        db = get_db()
        run_pending(db)
        self.assertTrue(m0004.verify(db))

        # Runtime-created data must also satisfy the strengthened verify.
        project = self.create_project("verify-probe-project")
        self.assertTrue(m0004.verify(db))

        member = db.project_member.find_one({"status": "active"})
        self.assertIsNotNone(member)
        original_name = member["display_name"]
        db.project_member.update_one(
            {"_id": member["_id"]}, {"$set": {"display_name": "x" * 200}}
        )
        self.assertFalse(m0004.verify(db))
        db.project_member.update_one(
            {"_id": member["_id"]}, {"$set": {"display_name": original_name}}
        )

        original_count = project.reload().user_count
        db.project.update_one(
            {"_id": project.id}, {"$set": {"m_uc": original_count + 1}}
        )
        self.assertFalse(m0004.verify(db))
        db.project.update_one(
            {"_id": project.id}, {"$set": {"m_uc": original_count}}
        )
        self.assertTrue(m0004.verify(db))
