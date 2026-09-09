import os
from bson import ObjectId

from app.constants.project import ProjectStatus
from app.models.partner_search import PartnerSearchThrottle
from app.models.project import Project
from app.models.site_setting import SiteSetting
from tests import MoeAPITestCase, TEST_FILE_PATH


class TestPartnerSearchAPI(MoeAPITestCase):
    URL = "/v1/partner-search-query-entry"

    def _enable(self, team_ids=None, rate_limit=10, max_limit=20):
        """开启外部撞车查询并配置团队等参数。"""
        site_setting = SiteSetting.get()
        site_setting.partner_search_enabled = True
        site_setting.partner_search_team_ids = (
            [ObjectId(id) for id in team_ids] if team_ids else []
        )
        site_setting.partner_search_rate_limit_seconds = rate_limit
        site_setting.partner_search_max_limit = max_limit
        site_setting.save()
        site_setting.reload()

    def _post_search(self, payload, source_ip="1.2.3.4"):
        return self.post(
            self.URL,
            json=payload,
            headers={"X-Forwarded-For": source_ip},
        )

    def _create_team_project(self, name, status=ProjectStatus.WORKING):
        """创建团队并返回其中的项目，团队不自动加入搜索列表。"""
        project = self.create_project(name)
        if status != ProjectStatus.WORKING:
            project.status = status
            project.save()
        return project

    def _all_team_ids(self, projects):
        return [str(project.team.pk) for project in projects]

    def test_disabled_returns_403(self):
        data = self._post_search({"keyword": "anyone"})
        self.assertEqual(data.status_code, 403)

    def test_no_keyword_returns_empty(self):
        project = self._create_team_project("某作品")
        self._enable(team_ids=self._all_team_ids([project]), rate_limit=0)
        data = self._post_search({})
        self.assertErrorEqual(data)
        self.assertEqual(
            data.json, {"total": 0, "page": 1, "limit": 20, "projects": []}
        )
        self.assertEqual(Project.objects(id=project.id).count(), 1)

    def test_search_matches_by_name(self):
        project = self._create_team_project("某某汉化组作品A")
        self._enable(team_ids=self._all_team_ids([project]), rate_limit=0)
        data = self._post_search({"keyword": "作品A"})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["total"], 1)
        self.assertEqual(data.json["projects"][0]["id"], str(project.id))
        self.assertEqual(data.json["projects"][0]["name"], "某某汉化组作品A")

    def test_completed_project_ignored(self):
        """已完结（COMPLETED）或已清空（CLEARED）的项目不纳入搜索范围。"""
        working_project = self._create_team_project(
            "进行中作品", status=ProjectStatus.WORKING
        )
        completed_project = self._create_team_project(
            "已完结作品", status=ProjectStatus.COMPLETED
        )
        cleared_project = self._create_team_project(
            "已清空作品", status=ProjectStatus.CLEARED
        )
        self._enable(
            team_ids=self._all_team_ids(
                [working_project, completed_project, cleared_project]
            ),
            rate_limit=0,
        )
        # 搜索“作品”，只应返回进行中的项目
        data = self._post_search({"keyword": "作品"})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["total"], 1)
        self.assertEqual(data.json["projects"][0]["id"], str(working_project.id))

    def test_search_ignores_other_teams(self):
        project_a = self._create_team_project("组A作品")
        project_b = self._create_team_project("组B作品")
        # 只把 group_a 的团队加入搜索列表
        self._enable(team_ids=[str(project_a.team.pk)], rate_limit=0)
        data = self._post_search({"keyword": "作品"})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["total"], 1)
        self.assertEqual(data.json["projects"][0]["id"], str(project_a.id))
        self.assertNotEqual(data.json["projects"][0]["id"], str(project_b.id))

    def test_search_uses_normalized_keyword(self):
        # 项目名会存为 NFC+casefold 投影，搜索应大小写不敏感且忽略首尾空白
        project = self._create_team_project("  NAME PROJECT  作品 ")
        self._enable(team_ids=self._all_team_ids([project]), rate_limit=0)
        data = self._post_search({"keyword": "  name project  "})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["total"], 1)
        self.assertEqual(data.json["projects"][0]["id"], str(project.id))

    def test_limit_is_capped_by_site_setting(self):
        projects = [self._create_team_project(f"多作品{i}") for i in range(5)]
        self._enable(team_ids=self._all_team_ids(projects), rate_limit=0, max_limit=1)
        data = self._post_search({"keyword": "多作品", "limit": 100})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["limit"], 1)
        self.assertLessEqual(len(data.json["projects"]), 1)

    def test_pagination_uses_page(self):
        projects = [self._create_team_project(f"分页作品{i}") for i in range(5)]
        self._enable(team_ids=self._all_team_ids(projects), rate_limit=0, max_limit=2)
        data = self._post_search({"keyword": "分页作品", "page": 2})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["page"], 2)
        self.assertEqual(data.json["limit"], 2)
        self.assertEqual(data.json["total"], 5)
        self.assertEqual(len(data.json["projects"]), 2)

    def test_rate_limit_blocks_second_request(self):
        project = self._create_team_project("限流作品")
        self._enable(team_ids=self._all_team_ids([project]), rate_limit=100)
        first = self._post_search({"keyword": "限流作品"})
        self.assertErrorEqual(first)
        self.assertEqual(first.json["total"], 1)
        # 同一来源第二发被限流
        second = self._post_search({"keyword": "限流作品"})
        self.assertEqual(second.status_code, 429)
        # 不同来源不受影响
        third = self._post_search({"keyword": "限流作品"}, source_ip="5.6.7.8")
        self.assertErrorEqual(third)
        self.assertEqual(third.json["total"], 1)

    def test_client_key_scopes_rate_limit(self):
        project = self._create_team_project("键控作品")
        self._enable(team_ids=self._all_team_ids([project]), rate_limit=100)
        self._post_search({"keyword": "键控作品", "client_key": "groupA"})
        # 同 ip 但不同 client_key 应放行
        data = self._post_search({"keyword": "键控作品", "client_key": "groupB"})
        self.assertErrorEqual(data)
        self.assertEqual(data.json["total"], 1)


    def test_search_returns_none_thumbnail_when_no_images(self):
        project = self._create_team_project("无图作品")
        self._enable(team_ids=self._all_team_ids([project]))
        data = self._post_search({"keyword": "无图作品"})
        self.assertErrorEqual(data)
        item = data.json["projects"][0]
        self.assertIn("thumbnail_url", item)
        self.assertIsNone(item["thumbnail_url"])

    def test_search_returns_first_page_thumbnail_url(self):
        project = self._create_team_project("有图作品")
        self._enable(team_ids=self._all_team_ids([project]))
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            img1 = project.upload("001.png", file)
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("002.png", file)
        data = self._post_search({"keyword": "有图作品"})
        self.assertErrorEqual(data)
        item = data.json["projects"][0]
        self.assertIn("thumbnail_url", item)
        self.assertIsNotNone(item["thumbnail_url"])
        self.assertEqual(item["thumbnail_url"], img1.cover_url)

    def test_search_picks_first_page_by_sort_order(self):
        project = self._create_team_project("排序作品")
        self._enable(team_ids=self._all_team_ids([project]))
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            img2 = project.upload("002.png", file)
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            img1 = project.upload("001.png", file)
        data = self._post_search({"keyword": "排序作品"})
        self.assertErrorEqual(data)
        item = data.json["projects"][0]
        self.assertEqual(item["thumbnail_url"], img1.cover_url)
        self.assertNotEqual(item["thumbnail_url"], img2.cover_url)
    def tearDown(self):
        try:
            PartnerSearchThrottle.drop_collection()
        except Exception:
            pass
        super().tearDown()
