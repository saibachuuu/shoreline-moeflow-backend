"""查重服务层的测试。

这是本模块**最关键**的一组测试：三态语义。
若「查询失败」被写成「未查到」，用户会以为没有撞车——这是最危险的失败模式。
"""

from unittest import mock

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("ziteng_partner")

from app.modules.ziteng_partner import config, service
from app.modules.ziteng_partner.client import PartnerApiError, PartnerWork
from app.modules.ziteng_partner.models import (
    VERDICT_CLEAR,
    VERDICT_FAILED,
    VERDICT_SUSPECTED,
    CheckStatus,
    ZitengCheck,
    ZitengQuery,
    ZitengWork,
)
from tests import MoeTestCase


def work(**overrides) -> PartnerWork:
    data = {
        "id": "zt-1",
        "reference": "1",
        "display_title": "【1】作品",
        "original_title": "作品",
        "author": "",
        "circle": "",
        "state": "published",
        "stage": "已上传",
    }
    data.update(overrides)
    return PartnerWork(**data)


class ServiceTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        config.reset_cache()
        # 清空模块自有集合，保证用例之间互不影响
        for model in (ZitengCheck, ZitengQuery, ZitengWork):
            model.drop_collection()
        self.project = self.create_project("テスト作品")

    def tearDown(self):
        config.reset_cache()
        super().tearDown()


class EnsureCheckTest(ServiceTestCase):
    def test_creates_check_with_extracted_keyword(self):
        check = service.ensure_check(str(self.project.id), "【385】[社团] 作品名 [DL版]")
        self.assertEqual("作品名", check.keyword)
        self.assertEqual(int(CheckStatus.QUEUED), check.status)

    def test_reuses_existing_check(self):
        first = service.ensure_check(str(self.project.id), "作品A")
        second = service.ensure_check(str(self.project.id), "作品A")
        self.assertEqual(str(first.id), str(second.id))
        self.assertEqual(1, ZitengCheck.objects.count())

    def test_updates_keyword_on_rename(self):
        service.ensure_check(str(self.project.id), "旧名")
        check = service.ensure_check(str(self.project.id), "新名")
        self.assertEqual("新名", check.keyword)


class ThreeStateTest(ServiceTestCase):
    """三态：未查到 / 有疑似 / 查询失败。"""

    def test_failure_is_not_clear(self):
        """**核心断言**：查询失败绝不写 verdict=clear。"""
        check = service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service, "search", side_effect=PartnerApiError("boom")
        ):
            result = service.run_check(str(self.project.id))

        self.assertEqual(int(CheckStatus.FAILED), result.status)
        self.assertEqual(VERDICT_FAILED, result.verdict)
        self.assertNotEqual(VERDICT_CLEAR, result.verdict)
        self.assertIn("boom", result.error)

    def test_timeout_is_failure_not_clear(self):
        """超时同样必须归为失败。"""
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service, "search", side_effect=PartnerApiError("超时")
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual(VERDICT_FAILED, result.verdict)

    def test_no_results_is_clear(self):
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service, "search", return_value=mock.Mock(works=[], total=0, truncated=False)
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual(int(CheckStatus.SUCCEEDED), result.status)
        self.assertEqual(VERDICT_CLEAR, result.verdict)
        self.assertEqual(0, result.suspect_count)

    def test_match_is_suspected(self):
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service,
            "search",
            return_value=mock.Mock(
                works=[work(original_title=self.project.name)],
                total=1,
                truncated=False,
            ),
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual(VERDICT_SUSPECTED, result.verdict)
        self.assertEqual(1, result.suspect_count)
        self.assertEqual("zt-1", result.suspects[0]["id"])

    def test_unrelated_candidates_are_clear(self):
        """有候选但都不像 → clear（不是 suspected）。"""
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service,
            "search",
            return_value=mock.Mock(
                works=[work(original_title="完全に無関係な別の作品")],
                total=1,
                truncated=False,
            ),
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual(VERDICT_CLEAR, result.verdict)
        self.assertEqual(0, result.suspect_count)


class CacheTest(ServiceTestCase):
    """懒查询缓存：同一关键词在 TTL 内不重复请求。"""

    def test_second_call_uses_cache(self):
        service.ensure_check(str(self.project.id), self.project.name)
        payload = mock.Mock(works=[work()], total=1, truncated=False)

        with mock.patch.object(service, "search", return_value=payload) as patched:
            service.run_check(str(self.project.id))
            self.assertEqual(1, patched.call_count)

            # 第二次：应先命中缓存，不再发请求
            service.run_check(str(self.project.id))
            self.assertEqual(1, patched.call_count)

    def test_use_cache_false_forces_refetch(self):
        service.ensure_check(str(self.project.id), self.project.name)
        payload = mock.Mock(works=[work()], total=1, truncated=False)
        with mock.patch.object(service, "search", return_value=payload) as patched:
            service.run_check(str(self.project.id))
            service.run_check(str(self.project.id), use_cache=False)
            self.assertEqual(2, patched.call_count)

    def test_expired_cache_refetches(self):
        service.ensure_check(str(self.project.id), self.project.name)
        payload = mock.Mock(works=[work()], total=1, truncated=False)
        with mock.patch.object(service, "search", return_value=payload) as patched:
            service.run_check(str(self.project.id))
            # TTL 设为 0 → 立即过期
            with mock.patch.dict("os.environ", {"ZITENG_PARTNER_CACHE_TTL": "0"}):
                config.reset_cache()
                service.run_check(str(self.project.id))
            self.assertEqual(2, patched.call_count)

    def test_works_are_persisted(self):
        service.ensure_check(str(self.project.id), self.project.name)
        payload = mock.Mock(works=[work(id="zzz")], total=1, truncated=False)
        with mock.patch.object(service, "search", return_value=payload):
            service.run_check(str(self.project.id))
        self.assertEqual(1, ZitengWork.objects.count())
        self.assertEqual("zzz", ZitengWork.objects.first().work_id)


class UnsearchableTitleTest(ServiceTestCase):
    """标题不可检索时记 clear，并保留 keyword 供排查。"""

    def test_symbol_only_name_is_clear(self):
        service.ensure_check(str(self.project.id), "!!!")
        result = service.run_check(str(self.project.id))
        self.assertEqual(VERDICT_CLEAR, result.verdict)
        self.assertEqual(int(CheckStatus.SUCCEEDED), result.status)

    def test_no_request_made_for_unsearchable(self):
        service.ensure_check(str(self.project.id), "!!!")
        with mock.patch.object(service, "search") as patched:
            service.run_check(str(self.project.id))
        patched.assert_not_called()


class CheckModelTest(ServiceTestCase):
    """模型的原子领取与状态写入。"""

    def test_claim_is_atomic(self):
        check = service.ensure_check(str(self.project.id), "作品")
        self.assertTrue(check.claim())
        # 第二次领取必须失败（防止并发查询打爆对方 1 req/s 配额）
        again = ZitengCheck.objects(project_id=str(self.project.id)).first()
        self.assertFalse(again.claim())

    def test_set_failed_never_sets_clear(self):
        check = service.ensure_check(str(self.project.id), "作品")
        check.claim()
        check.set_failed("网络错误")
        reloaded = ZitengCheck.objects(project_id=str(self.project.id)).first()
        self.assertEqual(VERDICT_FAILED, reloaded.verdict)
        self.assertEqual(int(CheckStatus.FAILED), reloaded.status)

    def test_to_api_shape(self):
        check = service.ensure_check(str(self.project.id), "作品")
        check.set_result(VERDICT_CLEAR, [])
        api = check.to_api()
        for key in (
            "project_id",
            "keyword",
            "status",
            "verdict",
            "suspects",
            "suspect_count",
            "error",
        ):
            self.assertIn(key, api)


class SuspectPayloadTest(ServiceTestCase):
    """疑似结果必须带上对方状态，供前端区分在制/已发布。"""

    def test_payload_includes_state_and_stage(self):
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service,
            "search",
            return_value=mock.Mock(
                works=[
                    work(
                        id="w1",
                        original_title=self.project.name,
                        state="in_progress",
                        stage="嵌字中",
                    )
                ],
                total=1,
                truncated=False,
            ),
        ):
            result = service.run_check(str(self.project.id))
        suspect = result.suspects[0]
        self.assertEqual("in_progress", suspect["state"])
        self.assertEqual("嵌字中", suspect["stage"])
        self.assertIn("level", suspect)
        self.assertIn("score", suspect)

    def test_in_progress_first(self):
        service.ensure_check(str(self.project.id), self.project.name)
        with mock.patch.object(
            service,
            "search",
            return_value=mock.Mock(
                works=[
                    work(id="pub", original_title=self.project.name, state="published"),
                    work(id="prog", original_title=self.project.name, state="in_progress"),
                ],
                total=2,
                truncated=False,
            ),
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual("prog", result.suspects[0]["id"])

    def test_max_results_applied(self):
        service.ensure_check(str(self.project.id), self.project.name)
        works = [work(id=f"w{i}", original_title=self.project.name) for i in range(10)]
        with mock.patch.object(
            service,
            "search",
            return_value=mock.Mock(works=works, total=10, truncated=False),
        ):
            result = service.run_check(str(self.project.id))
        self.assertEqual(config.max_results(), result.suspect_count)