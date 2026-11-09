"""Complete label snapshots, destructive operations and stale-writer fencing."""

from unittest.mock import patch

from bson import Binary
from mongoengine.connection import get_db

from app.models.file import File, Source, Translation, Tip
from app.models.language import Language
from app.models.target import Target
from app.services import label_snapshot as service
from app.services.label_lock import LabelBusyError, label_job_task
from tests import MoeAPITestCase


class LabelSnapshotTestCase(MoeAPITestCase):
    def test_queued_job_cannot_load_fresh_labels_after_clear(self):
        @label_job_task("project")
        def job(project_id):
            File.objects(id=self.image.pk).first().create_source("旧队列的原文")

        service.clear_labels(self.project, self.actor, "source", delete_labels=True)
        self.assertIn("跳过", job(str(self.project.pk), label_revision=0))
        self.assertEqual(0, Source.objects(file=self.image.pk).count())
        job(str(self.project.pk), label_revision=self.project.label_revision)
        self.assertEqual(1, Source.objects(file=self.image.pk).count())

    def test_running_job_cannot_refresh_objects_to_bypass_revision(self):
        @label_job_task("project")
        def job(project_id):
            service.clear_labels(self.project, self.actor, "source", delete_labels=True)
            File.objects(id=self.image.pk).first().create_source("不应重新出现")

        self.assertIn("跳过", job(str(self.project.pk), label_revision=0))
        self.assertEqual(0, Source.objects(file=self.image.pk).count())

    def setUp(self):
        super().setUp()
        self.project = self.create_project(
            "snapshot",
            source_language=Language.by_code("ja"),
            target_languages=Language.by_code("zh-CN"),
        )
        self.actor = self.get_creator(self.project)
        self.target = self.project.targets().first()
        self.other_target = Target.create(self.project, Language.by_code("en"))
        self.image = self.project.create_file("page.png")
        self.image.update(save_name="page.png")
        self.source = self.image.create_source(
            "原文", x=0.25, y=0.5, vertices=[[0.1, 0.2], [0.3, 0.4]]
        )
        self.writer = self.create_user("translator")
        self.proofreader = self.create_user("proofreader")
        self.translation = self.source.create_translation(
            "译文", self.target, self.writer
        )
        self.translation.update(proofread_content="修订", proofreader=self.proofreader)
        self.translation.select(self.actor)
        self.tip = self.source.create_tip("批注", self.target, self.proofreader)
        self.other = self.source.create_translation(
            "English", self.other_target, self.writer
        )
        self.other.update(proofread_content="Revised", proofreader=self.proofreader)

    def test_snapshot_round_trip_all_languages_and_metadata(self):
        before = service.label_data(self.project)
        with patch.object(service, "CHUNK_BYTES", 128):
            snapshot = service.create_snapshot(self.project, self.actor, "存档")
        self.assertEqual(before, service.load_snapshot(self.project, snapshot))
        self.assertGreater(
            get_db().project_label_snapshot_chunk.count_documents(
                {"snapshot": snapshot}
            ),
            3,
        )
        self.translation.update(content="另一译文")
        service.restore_snapshot(self.project, self.actor, snapshot)
        self.assertEqual(before, service.label_data(self.project))
        self.assertEqual(
            2, get_db().project_label_snapshot.count_documents({"ready": True})
        )

    def test_tampered_snapshot_refused_without_label_changes(self):
        snapshot = service.create_snapshot(self.project, self.actor)
        get_db().project_label_snapshot_chunk.update_one(
            {"snapshot": snapshot}, {"$set": {"data": Binary(b"broken")}}
        )
        before = service.label_data(self.project)
        with self.assertRaises(service.LabelOperationError):
            service.restore_snapshot(self.project, self.actor, snapshot)
        self.assertEqual(before, service.label_data(self.project))

    def test_replaced_file_refuses_restore(self):
        snapshot = service.create_snapshot(self.project, self.actor)
        self.image.update(save_name="replacement.png")
        with self.assertRaises(service.LabelOperationError):
            service.restore_snapshot(self.project, self.actor, snapshot)

    def test_new_file_labels_cleared_but_file_preserved(self):
        snapshot = service.create_snapshot(self.project, self.actor)
        added = self.project.create_file("new.png")
        added.create_source("新标签")
        service.restore_snapshot(self.project, self.actor, snapshot)
        self.assertEqual(0, Source.objects(file=added).count())
        self.assertEqual(1, File.objects(id=added.pk).count())

    def test_clear_proofread_keeps_selection_and_optional_tip(self):
        service.clear_labels(self.project, self.actor, "proofread")
        self.translation.reload()
        self.assertEqual("", self.translation.proofread_content)
        self.assertTrue(self.translation.selected)
        self.assertEqual(1, Tip.objects.count())
        service.clear_labels(self.project, self.actor, "proofread", clear_tips=True)
        self.assertEqual(0, Tip.objects.count())
        self.translation.reload()
        self.assertTrue(self.translation.selected)

    def test_clear_translation_cascades_all_authors_languages(self):
        service.clear_labels(self.project, self.actor, "translation")
        self.assertEqual(0, Translation.objects.count())
        self.source.reload()
        self.assertEqual("原文", self.source.content)
        self.assertEqual(1, Tip.objects.count())
        self.project.reload()
        self.assertEqual(0, self.project.translated_source_count)
        self.assertEqual(0, self.project.checked_source_count)

    def test_delete_translation_labels_refused_if_source_nonempty(self):
        with self.assertRaises(service.LabelOperationError):
            service.clear_labels(
                self.project, self.actor, "translation", delete_labels=True
            )
        self.assertEqual(0, get_db().project_label_snapshot.count_documents({}))
        self.source.update(content="")
        service.clear_labels(
            self.project, self.actor, "translation", delete_labels=True
        )
        self.assertEqual(0, Source.objects.count())
        self.assertEqual(0, Translation.objects.count())
        self.assertEqual(0, Tip.objects.count())

    def test_clear_source_preserves_image_marker_or_deletes_it(self):
        service.clear_labels(self.project, self.actor, "source")
        self.source.reload()
        self.assertEqual("", self.source.content)
        self.assertEqual(0.25, self.source.x)
        self.assertFalse(self.source.blank)
        self.assertEqual(0, Translation.objects.count())
        service.clear_labels(self.project, self.actor, "source", delete_labels=True)
        self.assertEqual(0, Source.objects.count())

    def test_clear_text_source_becomes_blank(self):
        text = self.project.create_file("note.txt")
        text_source = text.create_source("text")
        service.clear_labels(self.project, self.actor, "source")
        text_source.reload()
        self.assertTrue(text_source.blank)
        self.project.reload()
        self.assertEqual(1, self.project.source_count)

    def test_merge_creates_proofreader_translation_and_only_current_language(self):
        other_before = self.other.reload().to_mongo().to_dict()
        other_before.pop("label_epoch", None)
        preview = service.merge_preview(self.project, self.target)
        snapshot = service.merge_labels(
            self.project, self.target, self.actor, preview["token"], {}, {}
        )
        merged = Translation.objects(
            source=self.source, target=self.target, user=self.proofreader
        ).first()
        self.assertEqual("修订", merged.content)
        self.assertTrue(merged.selected)
        self.assertEqual(self.actor.pk, merged.selector.pk)
        self.assertFalse(merged.mt)
        self.assertEqual(
            0, Translation.objects(target=self.target, proofread_content__ne="").count()
        )
        other_after = self.other.reload().to_mongo().to_dict()
        other_after.pop("label_epoch", None)
        self.assertEqual(other_before, other_after)
        self.assertEqual(
            "merge",
            get_db().project_label_snapshot.find_one({"_id": snapshot})["reason"],
        )
        self.assertEqual(1, Tip.objects.count())

    def test_merge_overwrites_existing_proofreader_translation(self):
        own = self.source.create_translation(
            "旧译", self.target, self.proofreader, mt=True
        )
        preview = service.merge_preview(self.project, self.target)
        service.merge_labels(
            self.project, self.target, self.actor, preview["token"], {}, {}
        )
        own.reload()
        self.assertEqual("修订", own.content)
        self.assertFalse(own.mt)
        self.assertTrue(own.selected)

    def test_content_conflict_must_be_resolved_before_snapshot(self):
        another = self.source.create_translation("另一个译文", self.target, self.actor)
        another.update(proofread_content="另一修订", proofreader=self.proofreader)
        preview = service.merge_preview(self.project, self.target)
        self.assertEqual(2, len(preview["candidates"][0]["versions"]))
        with self.assertRaises(service.LabelOperationError):
            service.merge_labels(
                self.project, self.target, self.actor, preview["token"], {}, {}
            )
        self.assertEqual(0, get_db().project_label_snapshot.count_documents({}))
        candidate = preview["candidates"][0]
        service.merge_labels(
            self.project,
            self.target,
            self.actor,
            preview["token"],
            {candidate["key"]: str(another.pk)},
            {},
        )
        self.assertEqual(
            "另一修订",
            Translation.objects(
                source=self.source, target=self.target, user=self.proofreader
            )
            .first()
            .content,
        )

    def test_multiple_proofreaders_preserved_but_one_selected(self):
        another = self.source.create_translation("另译", self.target, self.actor)
        another.update(proofread_content="另一修订", proofreader=self.writer)
        preview = service.merge_preview(self.project, self.target)
        self.assertEqual(1, len(preview["selection_conflicts"]))
        with self.assertRaises(service.LabelOperationError):
            service.merge_labels(
                self.project, self.target, self.actor, preview["token"], {}, {}
            )
        service.merge_labels(
            self.project,
            self.target,
            self.actor,
            preview["token"],
            {},
            {str(self.source.pk): str(self.writer.pk)},
        )
        selected = Translation.objects(
            source=self.source, target=self.target, selected=True
        )
        self.assertEqual(1, selected.count())
        self.assertEqual(self.writer.pk, selected.first().user.pk)
        self.assertEqual(
            "修订",
            Translation.objects(
                source=self.source, target=self.target, user=self.proofreader
            )
            .first()
            .content,
        )

    def test_preview_change_refused_without_snapshot(self):
        preview = service.merge_preview(self.project, self.target)
        self.tip.update(content="新增批注")
        with self.assertRaises(service.LabelOperationError):
            service.merge_labels(
                self.project, self.target, self.actor, preview["token"], {}, {}
            )
        self.assertEqual(0, get_db().project_label_snapshot.count_documents({}))

    def test_operation_failure_rolls_back_all_label_data(self):
        before = service.label_data(self.project)
        original = service.rebuild_project_statistics
        calls = 0

        def fail_once(project):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected failure")
            return original(project)

        with patch.object(service, "rebuild_project_statistics", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                service.clear_labels(
                    self.project, self.actor, "source", delete_labels=True
                )
        self.assertEqual(before, service.label_data(self.project))
        self.project.reload()
        self.assertIsNone(self.project.label_recovery)

    def test_failed_rollback_keeps_journal_and_recovery_restores(self):
        before = service.label_data(self.project)
        with patch.object(
            service,
            "rebuild_project_statistics",
            side_effect=RuntimeError("injected failure"),
        ):
            with self.assertRaises(RuntimeError):
                service.clear_labels(
                    self.project, self.actor, "source", delete_labels=True
                )
        self.project.reload()
        self.assertIsNotNone(self.project.label_recovery)
        with self.assertRaises(LabelBusyError):
            self.source.update(content="迟到写入")
        service.recover_operation(self.project)
        self.assertEqual(before, service.label_data(self.project))

    def test_old_model_and_old_http_revision_cannot_write_back(self):
        service.clear_labels(self.project, self.actor, "proofread")
        with self.assertRaises(LabelBusyError):
            self.translation.update(content="迟到的旧任务")
        with self.assertRaises(LabelBusyError):
            self.translation.modify(content="迟到的旧任务")
        with self.assertRaises(LabelBusyError):
            self.image.create_source("迟到的旧标签")
        response = self.put(
            f"/v1/sources/{self.source.pk}",
            token=self.actor.generate_token(),
            json={"content": "迟到的旧页面"},
            headers={"X-Label-Revision": "0"},
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("1", response.headers["X-Label-Revision"])
        self.source.reload()
        self.source.update(content="重新加载后允许写入")
        inserted = Source.objects.insert(
            Source(file=self.image.reload(), content="批量新增")
        )
        self.assertEqual(1, inserted.label_epoch)

    def test_unauthorized_cannot_restore_or_clear(self):
        outsider = self.create_user("outsider")
        response = self.post(
            f"/v1/projects/{self.project.pk}/label-clear",
            token=outsider.generate_token(),
            json={"field": "source"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(103, response.json["code"])
        self.assertEqual(0, get_db().project_label_snapshot.count_documents({}))
