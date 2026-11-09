from unittest.mock import patch

from app.models.file import File, Source, Translation
from app.models.output import Output
from app.scripts.backfill_storage_usage import _probe_derived
from app.services.project_statistics import rebuild_project_statistics
from tests import MoeTestCase


class ProjectStatisticsTestCase(MoeTestCase):
    def test_absolute_totals_include_history_derived_and_outputs_not_folders(self):
        project = self.create_project("statistics")
        actor = self.get_creator(project)
        target = project.targets().first()
        folder = project.create_folder("folder")
        image = project.create_file("page.png", parent=folder)
        image.update(
            save_name="page.png", file_size=2, cover_bytes=11, resample_bytes=13
        )
        history = project.create_file("history.png", parent=folder)
        history.update(
            activated=False,
            save_name="history.png",
            file_size=3,
            cover_bytes=17,
            resample_bytes=19,
        )
        source = image.create_source("原文")
        old_source = Source(file=history, rank=1, content="历史原文").save()
        Translation(
            source=source, target=target, user=actor, content="译文", selected=True
        ).save()
        Translation(
            source=old_source,
            target=target,
            user=actor,
            content="历史译文",
            selected=True,
        ).save()
        Output(
            project=project,
            target=target,
            user=actor,
            file_name="output.zip",
            storage_bytes=23,
        ).save()
        folder.update(file_size=9999)
        project.update(file_size=99999, source_count=9999)

        totals = rebuild_project_statistics(project)
        self.assertEqual(5 * 1024 + 11 + 13 + 17 + 19 + 23, totals["total_bytes"])
        self.assertEqual(
            (1, 1, 1, 1), (totals["fc"], totals["sc"], totals["tsc"], totals["csc"])
        )
        self.assertEqual(2, folder.reload().file_size)
        self.assertEqual(1, target.reload().checked_source_count)
        self.assertEqual(totals, rebuild_project_statistics(project))

    def test_dry_run_does_not_rewrite_caches(self):
        project = self.create_project("dryrun")
        project.update(file_size=12345)
        totals = rebuild_project_statistics(project, apply=False)
        self.assertEqual(0, totals["total_bytes"])
        self.assertEqual(12345, project.reload().file_size)

    def test_resample_probe_uses_project_storage_and_only_falls_back_on_missing(self):
        project = self.create_project("probe")
        image = project.create_file("page.png")
        image.update(save_name="page.png")
        backend = object()
        config = {
            "OSS_FILE_PREFIX": "files/",
            "OSS_PROCESS_COVER_NAME": "cover",
            "OSS_PROCESS_RESAMPLE_NAME": "resample",
        }
        with patch(
            "app.scripts.backfill_storage_usage._head_size",
            side_effect=[(11, None), (13, None)],
        ) as probe:
            self.assertEqual(((11, 13), None), _probe_derived(image, config, backend))
            self.assertIs(backend, probe.call_args_list[1].args[0])
        with patch(
            "app.scripts.backfill_storage_usage._head_size",
            side_effect=[(11, None), (None, None), (17, None)],
        ):
            self.assertEqual(((11, 17), None), _probe_derived(image, config, backend))
        with patch(
            "app.scripts.backfill_storage_usage._head_size",
            side_effect=[(11, None), (None, "access denied")],
        ) as probe:
            self.assertEqual(
                (None, "access denied"), _probe_derived(image, config, backend)
            )
            self.assertEqual(2, probe.call_count)
