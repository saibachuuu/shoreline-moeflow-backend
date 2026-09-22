"""紫藤外组检索模块的测试。

覆盖 `docs/optional-modules.md` §6 的关键行为，重点是那些"错了会误导用户"的地方：
- 三态严格区分（未查到 / 有疑似 / 查询失败）
- **查询失败绝不降级为「未查到」**（最危险的失败模式）
- 默认只查已立项（不带 include_inactive）
- 429 重试 / 409 重查 / 503 归类为失败
- 翻页上限
- 标题提取与匹配优先级

所有测试都打桩了 HTTP 层，不发真实请求（对方配额是全局 1 req/s）。
"""

from unittest import TestCase
from unittest import mock

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("ziteng_partner")

from app.modules.ziteng_partner import client, config, matching, title
from app.modules.ziteng_partner.client import PartnerApiError, PartnerWork
from tests import MoeTestCase


def make_work(**overrides) -> PartnerWork:
    data = {
        "id": "zt-1",
        "reference": "1",
        "display_title": "【1】テスト作品",
        "original_title": "テスト作品",
        "author": "作者",
        "circle": "社团",
        "state": "published",
        "stage": "已上传",
    }
    data.update(overrides)
    return PartnerWork(**data)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text="", *, json_error=False):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self._json_error = json_error
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class TitleExtractionTest(TestCase):
    """标题提取：编号与版本标签剥离，系列括号保留。"""

    def test_strips_index_brackets_and_version_tags(self):
        self.assertEqual(
            "魔法少女敗北実現委員会 (ブルーアーカイブ)",
            title.extract_title(
                "【385】[ShiBoo! (Ixy)] 魔法少女敗北実現委員会 (ブルーアーカイブ) [DL版]"
            ),
        )

    def test_keeps_series_parentheses(self):
        """系列括号是重要区分信息，不能丢（§6.2.1 实测校准）。"""
        self.assertEqual(
            "作品名 (ブルーアーカイブ)", title.extract_title("【9】作品名 (ブルーアーカイブ)")
        )

    def test_keeps_volume_numbers(self):
        self.assertIn("第3巻", title.extract_title("第3巻 作品"))

    def test_plain_name_passes_through(self):
        self.assertEqual("六畳一間の魔法少女", title.extract_title("六畳一間の魔法少女"))

    def test_empty_input(self):
        self.assertEqual("", title.extract_title(""))
        self.assertEqual("", title.extract_title("   "))

    def test_is_searchable(self):
        self.assertTrue(title.is_searchable("作品"))
        self.assertFalse(title.is_searchable(""))
        self.assertFalse(title.is_searchable("   "))
        self.assertFalse(title.is_searchable("!!!___"))
        self.assertFalse(title.is_searchable("あ" * 241))


class ClientScopeTest(TestCase):
    """默认只查已立项：请求里不得出现 include_inactive。"""

    def setUp(self):
        config.reset_cache()
        client.reset_throttle()
        self._env = mock.patch.dict(
            "os.environ", {"ZITENG_PARTNER_API_KEY": "k"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        config.reset_cache()

    def _capture_params(self, payload):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured.update(params or {})
            return FakeResponse(200, payload)

        with mock.patch.object(client.requests, "get", side_effect=fake_get):
            with mock.patch.object(client, "_throttle") as throttle:
                throttle.wait = lambda: None
                throttle.mark = lambda: None
                client.search("作品")
        return captured

    def test_omits_include_inactive_by_default(self):
        params = self._capture_params(
            {"total": 0, "results": [], "next_page": None, "revision": "r"}
        )
        self.assertNotIn("include_inactive", params)
        self.assertEqual("作品", params["q"])

    def test_includes_include_inactive_when_enabled(self):
        with mock.patch.dict("os.environ", {"ZITENG_PARTNER_INCLUDE_INACTIVE": "true"}):
            config.reset_cache()
            params = self._capture_params(
                {"total": 0, "results": [], "next_page": None, "revision": "r"}
            )
        self.assertEqual("true", params["include_inactive"])


class ClientPagingTest(TestCase):
    """翻页、revision 与上限。"""

    def setUp(self):
        config.reset_cache()
        client.reset_throttle()
        self._env = mock.patch.dict(
            "os.environ", {"ZITENG_PARTNER_API_KEY": "k"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        config.reset_cache()

    def _run(self, pages, **env):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(dict(params or {}))
            page = (params or {}).get("page", 1)
            return FakeResponse(200, pages[page - 1])

        with mock.patch.dict("os.environ", env, clear=False):
            config.reset_cache()
            with mock.patch.object(client.requests, "get", side_effect=fake_get):
                with mock.patch.object(client, "_throttle") as throttle:
                    throttle.wait = lambda: None
                    throttle.mark = lambda: None
                    result = client.search("作品")
        return result, calls

    def test_single_page(self):
        result, calls = self._run(
            [
                {
                    "total": 1,
                    "results": [
                        {
                            "id": "a",
                            "reference": "1",
                            "display_title": "d",
                            "original_title": "o",
                            "author": "",
                            "circle": "",
                            "state": "published",
                            "stage": "已上传",
                        }
                    ],
                    "next_page": None,
                    "revision": "rev1",
                }
            ]
        )
        self.assertEqual(1, len(result.works))
        self.assertEqual(1, result.total)
        self.assertFalse(result.truncated)
        self.assertEqual(1, len(calls))

    def test_follows_next_page_with_revision(self):
        pages = [
            {"total": 2, "results": [], "next_page": 2, "revision": "revX"},
            {"total": 2, "results": [], "next_page": None, "revision": "revX"},
        ]
        _result, calls = self._run(pages)
        self.assertEqual(2, len(calls))
        # 第二页必须携带第一页的 revision（锁定同一索引版本）
        self.assertEqual("revX", calls[1].get("revision"))

    def test_respects_max_pages_and_marks_truncated(self):
        pages = [
            {"total": 100, "results": [], "next_page": i + 1, "revision": "r"}
            for i in range(1, 6)
        ]
        with mock.patch.dict("os.environ", {"ZITENG_PARTNER_MAX_PAGES": "2"}):
            config.reset_cache()
            calls = []

            def fake_get(url, params=None, headers=None, timeout=None):
                calls.append(dict(params or {}))
                return FakeResponse(200, pages[(params or {}).get("page", 1) - 1])

            with mock.patch.object(client.requests, "get", side_effect=fake_get):
                with mock.patch.object(client, "_throttle") as throttle:
                    throttle.wait = lambda: None
                    throttle.mark = lambda: None
                    result = client.search("作品")
        self.assertEqual(2, len(calls))
        self.assertTrue(result.truncated)


class ClientErrorTest(TestCase):
    """错误处理：每次都必须是 PartnerApiError，绝不静默返回空结果。"""

    def setUp(self):
        config.reset_cache()
        client.reset_throttle()
        self._env = mock.patch.dict(
            "os.environ", {"ZITENG_PARTNER_API_KEY": "k"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        config.reset_cache()

    def _search_with(self, side_effect):
        with mock.patch.object(client.requests, "get", side_effect=side_effect):
            with mock.patch.object(client, "_throttle") as throttle:
                throttle.wait = lambda: None
                throttle.mark = lambda: None
                with mock.patch.object(client.time, "sleep", lambda _s: None):
                    return client.search("作品")

    def test_429_retries_then_raises(self):
        response = FakeResponse(429, headers={"Retry-After": "1"})
        with self.assertRaises(PartnerApiError):
            self._search_with(lambda *a, **k: response)

    def test_429_then_success(self):
        responses = [
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(200, {"total": 0, "results": [], "next_page": None}),
        ]
        result = self._search_with(lambda *a, **k: responses.pop(0))
        self.assertEqual(0, result.total)

    def test_503_raises(self):
        with self.assertRaises(PartnerApiError):
            self._search_with(lambda *a, **k: FakeResponse(503))

    def test_400_raises_without_retry(self):
        with self.assertRaises(PartnerApiError):
            self._search_with(lambda *a, **k: FakeResponse(400, text="invalid_query"))

    def test_network_error_raises(self):
        import requests

        with self.assertRaises(PartnerApiError):
            self._search_with(requests.ConnectionError("boom"))

    def test_non_json_raises(self):
        with self.assertRaises(PartnerApiError):
            self._search_with(lambda *a, **k: FakeResponse(200, json_error=True))

    def test_409_recovers_by_restarting_from_page_one(self):
        """409 catalog_changed：丢弃本轮分页，从第一页重查。"""
        calls = []
        responses = [
            FakeResponse(409),
            FakeResponse(
                200,
                {
                    "total": 1,
                    "results": [
                        {
                            "id": "x",
                            "reference": "1",
                            "display_title": "d",
                            "original_title": "o",
                            "author": "",
                            "circle": "",
                            "state": "published",
                            "stage": "s",
                        }
                    ],
                    "next_page": None,
                    "revision": "r2",
                },
            ),
        ]

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(dict(params or {}))
            return responses.pop(0)

        with mock.patch.object(client.requests, "get", side_effect=fake_get):
            with mock.patch.object(client, "_throttle") as throttle:
                throttle.wait = lambda: None
                throttle.mark = lambda: None
                result = client.search("作品")
        self.assertEqual(1, len(result.works))
        # 两次请求都是第一页
        self.assertEqual([1, 1], [call["page"] for call in calls])

    def test_empty_query_rejected(self):
        with self.assertRaises(PartnerApiError):
            client.search("")

    def test_overlong_query_rejected(self):
        with self.assertRaises(PartnerApiError):
            client.search("あ" * 241)


class MatchingTest(TestCase):
    """本地匹配与排序。"""

    def test_exact_match(self):
        level, value = matching.score("テスト作品", make_work(original_title="テスト作品"))
        self.assertEqual(matching.MATCH_EXACT, level)
        self.assertEqual(1.0, value)

    def test_series_suffix_does_not_break_exact(self):
        """系列括号在比对时被剥离，因此算完全匹配。"""
        level, _ = matching.score(
            "テスト作品", make_work(original_title="テスト作品 (ブルーアーカイブ)")
        )
        self.assertEqual(matching.MATCH_EXACT, level)

    def test_contains_match(self):
        level, _ = matching.score(
            "魔法少女敗北実現委員会", make_work(original_title="魔法少女敗北実現委員会 前編")
        )
        self.assertEqual(matching.MATCH_CONTAINS, level)

    def test_unrelated_is_no_match(self):
        level, _ = matching.score("全然違う作品", make_work(original_title="テスト作品"))
        self.assertEqual(matching.MATCH_NONE, level)

    def test_short_substring_does_not_match(self):
        """过短的包含不算匹配，避免噪声。"""
        level, _ = matching.score("魔法", make_work(original_title="魔法少女敗北実現委員会"))
        self.assertEqual(matching.MATCH_NONE, level)

    def test_in_progress_ranks_above_published(self):
        """在制的撞车信号比已发布更强。"""
        candidates = [
            make_work(id="p", original_title="作品", state="published"),
            make_work(id="i", original_title="作品", state="in_progress"),
        ]
        suspects = matching.find_suspects("作品", candidates)
        self.assertEqual("i", suspects[0].work.id)
        self.assertEqual("p", suspects[1].work.id)

    def test_limit_is_applied(self):
        candidates = [make_work(id=f"w{i}", original_title="作品") for i in range(10)]
        self.assertEqual(3, len(matching.find_suspects("作品", candidates, limit=3)))

    def test_empty_candidates(self):
        self.assertEqual([], matching.find_suspects("作品", []))


class ConfigTest(TestCase):
    """配置优先级：环境变量 > config.local > 默认值。"""

    def tearDown(self):
        config.reset_cache()

    def test_defaults(self):
        config.reset_cache()
        self.assertFalse(config.include_inactive())
        self.assertEqual(3, config.max_pages())
        self.assertEqual(5, config.max_results())

    def test_env_overrides(self):
        with mock.patch.dict(
            "os.environ",
            {
                "ZITENG_PARTNER_INCLUDE_INACTIVE": "true",
                "ZITENG_PARTNER_MAX_PAGES": "7",
            },
        ):
            config.reset_cache()
            self.assertTrue(config.include_inactive())
            self.assertEqual(7, config.max_pages())

    def test_bool_env_parsing(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict(
                "os.environ", {"ZITENG_PARTNER_INCLUDE_INACTIVE": value}
            ):
                config.reset_cache()
                self.assertTrue(config.include_inactive(), value)
        for value in ("0", "false", "no", "off"):
            with mock.patch.dict(
                "os.environ", {"ZITENG_PARTNER_INCLUDE_INACTIVE": value}
            ):
                config.reset_cache()
                self.assertFalse(config.include_inactive(), value)

    def test_config_module_stays_out_of_core(self):
        """模块配置不得出现在核心 app/config.py（C1）。"""
        import app.config as core_config

        self.assertFalse(hasattr(core_config, "ZITENG_PARTNER_API_KEY"))
        self.assertFalse(hasattr(core_config, "ZITENG_PARTNER_INCLUDE_INACTIVE"))


class ConfigLocalFileTest(TestCase):
    """`config.local.py` 是本机开发放密钥的途径（需求：配置文件可控制模块配置）。

    优先级：环境变量 > config.local.py > 内置默认值。
    """

    def setUp(self):
        config.reset_cache()

    def tearDown(self):
        import os
        import sys

        config.reset_cache()
        # 清掉测试注入的假模块与临时环境变量，避免污染其它用例
        sys.modules.pop("app.modules.ziteng_partner.config_local", None)
        for key in ("ZITENG_PARTNER_MAX_PAGES", "ZITENG_PARTNER_INCLUDE_INACTIVE"):
            os.environ.pop(key, None)

    def _install_local(self, **values):
        import sys
        import types

        module = types.ModuleType("app.modules.ziteng_partner.config_local")
        for key, value in values.items():
            setattr(module, key, value)
        sys.modules["app.modules.ziteng_partner.config_local"] = module
        config.reset_cache()

    def test_local_file_overrides_defaults(self):
        self.assertEqual(3, config.max_pages())
        self._install_local(MAX_PAGES=9)
        self.assertEqual(9, config.max_pages())

    def test_local_file_supplies_api_key_when_env_absent(self):
        """没设环境变量时，config.local.py 能提供密钥。

        （测试环境默认设了 `ZITENG_PARTNER_API_KEY`，因此这里先把它摘掉，
        以模拟「只靠配置文件」的本机开发场景。）
        """
        self._install_local(API_KEY="from-local")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("ZITENG_PARTNER_API_KEY", None)
            config.reset_cache()
            self.assertEqual("from-local", config.api_key())

    def test_env_overrides_local_file(self):
        self._install_local(MAX_PAGES=9)
        with mock.patch.dict("os.environ", {"ZITENG_PARTNER_MAX_PAGES": "2"}):
            config.reset_cache()
            self.assertEqual(2, config.max_pages())

    def test_unknown_keys_in_local_file_are_ignored(self):
        """写错的键名不应让模块炸掉——只被忽略。"""
        self._install_local(NOT_A_REAL_SETTING="whatever", MAX_PAGES=4)
        self.assertEqual(4, config.max_pages())  # 仍能正常读取

    def test_local_file_absent_is_fine(self):
        """没有 config.local.py 时必须用默认值正常工作（零配置可跑）。"""
        import sys

        sys.modules.pop("app.modules.ziteng_partner.config_local", None)
        config.reset_cache()
        self.assertEqual(3, config.max_pages())