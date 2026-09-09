from tests import MoeAPITestCase
from app.models.presence import ProjectPresence


class PresenceAPITestCase(MoeAPITestCase):
    def test_project_heartbeat_and_presence_query(self):
        """测试项目心跳上报与活跃成员查询"""
        project = self.create_project("presence-proj-1")
        user = self.create_user("presence-user-1")
        token = user.generate_token()

        # 上报心跳
        hb_resp = self.post(
            f"/v1/projects/{project.id}/presence/heartbeat",
            token=token,
            json={"action": "translation"},
        )
        self.assertErrorEqual(hb_resp)
        self.assertEqual("ok", hb_resp.json["message"])
        self.assertEqual(str(project.id), hb_resp.json["project_id"])
        self.assertEqual(1, len(hb_resp.json["active_users"]))
        active_u = hb_resp.json["active_users"][0]
        self.assertEqual(str(user.id), active_u["id"])
        self.assertEqual(user.name, active_u["name"])
        self.assertEqual("translation", active_u["action"])

        # GET /v1/projects/<id>/presence
        get_resp = self.get(
            f"/v1/projects/{project.id}/presence",
            token=token,
        )
        self.assertErrorEqual(get_resp)
        self.assertEqual(str(project.id), get_resp.json["project_id"])
        self.assertEqual(1, len(get_resp.json["active_users"]))
        self.assertEqual(str(user.id), get_resp.json["active_users"][0]["id"])

    def test_project_presence_leave(self):
        """测试离开项目工作状态"""
        project = self.create_project("presence-proj-leave")
        user = self.create_user("presence-user-leave")
        token = user.generate_token()

        # 先上报心跳
        self.post(
            f"/v1/projects/{project.id}/presence/heartbeat",
            token=token,
            json={"action": "setting"},
        )

        # 离开项目
        leave_resp = self.post(
            f"/v1/projects/{project.id}/presence/leave",
            token=token,
        )
        self.assertErrorEqual(leave_resp)
        self.assertEqual("ok", leave_resp.json["message"])

        # 查询活跃成员，应为空
        get_resp = self.get(
            f"/v1/projects/{project.id}/presence",
            token=token,
        )
        self.assertErrorEqual(get_resp)
        self.assertEqual(0, len(get_resp.json["active_users"]))

    def test_team_active_presence_aggregation(self):
        """测试团队下多项目活跃成员聚合统计"""
        project1 = self.create_project("presence-team-proj-1")
        team = project1.team
        creator = self.get_creator(team)

        # 在同一个团队下创建第二个项目
        from app.models.project import Project, ProjectSet

        project_set = ProjectSet.create("presence-pset-2", team=team)
        project2 = Project.create(
            "presence-team-proj-2",
            team,
            project_set=project_set,
            creator=creator,
        )

        user1 = self.create_user("presence-multi-u1")
        user2 = self.create_user("presence-multi-u2")

        # user1 在 project1 (translation)
        self.post(
            f"/v1/projects/{project1.id}/presence/heartbeat",
            token=user1.generate_token(),
            json={"action": "translation"},
        )
        # user2 在 project1 (staff)
        self.post(
            f"/v1/projects/{project1.id}/presence/heartbeat",
            token=user2.generate_token(),
            json={"action": "staff"},
        )
        # user1 同时在 project2 (setting)
        self.post(
            f"/v1/projects/{project2.id}/presence/heartbeat",
            token=user1.generate_token(),
            json={"action": "setting"},
        )

        # GET /v1/teams/<team_id>/projects/active-presence
        team_resp = self.get(
            f"/v1/teams/{team.id}/projects/active-presence",
            token=user1.generate_token(),
        )
        self.assertErrorEqual(team_resp)
        active_projects = team_resp.json["active_projects"]
        self.assertIn(str(project1.id), active_projects)
        self.assertIn(str(project2.id), active_projects)

        self.assertEqual(2, active_projects[str(project1.id)]["user_count"])
        self.assertEqual(1, active_projects[str(project2.id)]["user_count"])

        p1_actions = {u["action"] for u in active_projects[str(project1.id)]["users"]}
        self.assertEqual({"translation", "staff"}, p1_actions)

    def test_presence_uses_display_name(self):
        """活跃成员信息优先显示展示名"""
        project = self.create_project("presence-proj-dn")
        user = self.create_user("presence-dn-user")
        user.aliases = ["首选别名"]
        user.default_display_name = "首选别名"
        user.save()

        self.post(
            f"/v1/projects/{project.id}/presence/heartbeat",
            token=user.generate_token(),
            json={"action": "working"},
        )

        resp = self.get(
            f"/v1/projects/{project.id}/presence",
            token=user.generate_token(),
        )
        self.assertErrorEqual(resp)
        self.assertEqual("首选别名", resp.json["active_users"][0]["name"])
