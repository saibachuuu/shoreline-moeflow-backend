from app.exceptions import (
    IdentityVersionConflictError,
    InvalidIdentityRequestError,
    NoPermissionError,
    NeedTokenError,
    TeamQualificationRequiredError,
)
from app.models.application import Application
from app.models.invitation import Invitation, InvitationStatus
from app.models.project import Project, ProjectAllowApplyType
from app.models.project_member import ProjectMember
from app.models.team_member import TeamMember
from app.services.project_member import ProjectMemberService
from app.services.project_lifecycle import ProjectLifecycleService
from app.services.team_member import TeamMemberService
from tests import MoeAPITestCase


class IdentityAPITestCase(MoeAPITestCase):
    def _add_team_member(
        self, team, user, *, base_tag="member", qualifications=None, tags=None
    ):
        creator = self.get_creator(team)
        return TeamMemberService.add(
            team,
            creator,
            {
                "user_id": str(user.id),
                "base_tag": base_tag,
                "worker_qualifications": qualifications or [],
                "tags": tags or [],
            },
        )

    def test_user_team_list_includes_viewer_identity_permissions(self):
        team = self.create_team("api-viewer-team")
        creator = self.get_creator(team)

        response = self.get("/v1/user/teams", token=creator.generate_token())

        self.assertErrorEqual(response)
        item = next(item for item in response.json if item["id"] == str(team.id))
        self.assertEqual("creator", item["base_tag"])
        self.assertIn("team:CREATE_PROJECT_SET", item["effective_permissions"])
        self.assertEqual("qualified", item["worker_qualification_mode"])

    def test_user_project_list_includes_viewer_project_permissions(self):
        project = self.create_project("api-viewer-project")
        creator = self.get_creator(project.team)

        response = self.get("/v1/user/projects", token=creator.generate_token())

        self.assertErrorEqual(response)
        item = next(item for item in response.json if item["id"] == str(project.id))
        self.assertIn("project:ACCESS", item["effective_permissions"])
        self.assertIn("project:MANAGE_MEMBERS", item["effective_permissions"])

    def test_project_application_allow_projects_into_identity_member(self):
        project = self.create_project("api-application-identity-projection")
        project.allow_apply_type = ProjectAllowApplyType.ALL
        project.save()
        creator = self.get_creator(project.team)
        applicant = self.create_user("api-application-identity-applicant")

        created = self.post(
            f"/v1/projects/{project.id}/applications",
            token=applicant.generate_token(),
            json={"message": "请加入"},
        )
        self.assertErrorEqual(created)
        application = Application.objects(group=project, user=applicant).first()
        self.assertIsNotNone(application)

        allowed = self.patch(
            f"/v1/applications/{application.id}",
            token=creator.generate_token(),
            json={"allow": True},
        )
        self.assertErrorEqual(allowed)
        member = ProjectMember.objects(project=project, user=applicant).first()
        self.assertIsNotNone(member)
        self.assertEqual("active", member.status)

    def test_project_member_changes_reports_missing_worker_qualification(self):
        project = self.create_project("api-qualification-error")
        creator = self.get_creator(project.team)
        target = self.create_user("api-qualification-error-target")
        self._add_team_member(project.team, target)

        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-qualification-error-add",
                        "action": "add",
                        "user_id": str(target.id),
                        "tags": ["translator"],
                    }
                ]
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertEqual("TEAM_QUALIFICATION_REQUIRED", response.json["failed"]["code"])
        self.assertIsNone(ProjectMember.objects(project=project, user=target).first())

    def test_project_manager_can_add_worker_tag_to_self_through_changes_api(self):
        project = self.create_project("api-manager-self-worker")
        creator = self.get_creator(project.team)
        admin = self.create_user("api-manager-self-worker-admin")
        self._add_team_member(project.team, admin, base_tag="admin")

        member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(admin.id), "display_name": admin.name},
        )
        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=admin.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-manager-self-worker-update",
                        "action": "update",
                        "member_id": str(member.id),
                        "expected_member_version": member.version,
                        "changes": {"tags": ["translator"]},
                    }
                ]
            },
        )

        self.assertErrorEqual(response)
        self.assertEqual(["translator"], ProjectMember.objects.get(id=member.id).tags)

    def test_alias_api_keeps_site_and_team_scopes_independent(self):
        user = self.create_user("api-alias-user")
        token = user.generate_token()

        response = self.patch("/v1/me/aliases", token=token, json={"aliases": [" SiteName ", "sitename"]})
        self.assertErrorEqual(response)
        user.reload()
        self.assertEqual(["SiteName"], user.aliases)
        self.assertEqual(["SiteName"], response.json["user"]["aliases"])

        team = self.create_team("api-alias-team")
        relation = self._add_team_member(team, user)
        response = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}",
            token=token,
            json={"expected_version": relation.version, "tags": ["admin"]},
        )
        self.assertErrorEqual(response, NoPermissionError)
        relation.reload()
        self.assertEqual([], relation.tags)

        response = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/aliases",
            token=token,
            json={"aliases": [" TeamName ", "TEAMNAME"], "expected_version": relation.version},
        )
        self.assertErrorEqual(response)
        relation.reload()
        self.assertEqual(["TeamName"], relation.aliases)
        self.assertEqual(["SiteName"], user.reload().aliases)

        other_team = self.create_team("api-alias-other-team")
        other_relation = self._add_team_member(other_team, user)
        response = self.patch(
            f"/v1/teams/{other_team.id}/members/{other_relation.id}/aliases",
            token=token,
            json={"aliases": ["OtherTeam"], "expected_version": other_relation.version},
        )
        self.assertErrorEqual(response)
        self.assertEqual(["TeamName"], TeamMember.objects(team=team, user=user).first().aliases)
        self.assertEqual(["OtherTeam"], TeamMember.objects(team=other_team, user=user).first().aliases)

        outsider = self.create_user("api-alias-outsider")
        response = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/aliases",
            token=outsider.generate_token(),
            json={"aliases": [], "expected_version": relation.version},
        )
        self.assertErrorEqual(response, NoPermissionError)

        admin = self.create_user("api-alias-admin")
        admin.admin = True
        admin.save()
        response = self.patch(
            f"/v1/users/{user.id}/aliases",
            token=admin.generate_token(),
            json={"aliases": ["AdminEdited"]},
        )
        self.assertErrorEqual(response)
        self.assertEqual(["AdminEdited"], user.reload().aliases)

        response = self.get("/v1/users?word=AdminEdited", token=token)
        self.assertErrorEqual(response)
        self.assertEqual("1", response.headers["X-Pagination-Count"])
        self.assertEqual("api-alias-user", response.json[0]["name"])
        self.assertNotIn("email", response.json[0])

        self.assertErrorEqual(self.patch("/v1/me/aliases", json={"aliases": []}), NeedTokenError)

    def test_insight_uses_identity_members_and_keeps_external_members(self):
        project = self.create_project("api-identity-insight")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("api-identity-insight-user")
        self._add_team_member(
            team, user, qualifications=["translator", "proofreader"]
        )
        registered_member = ProjectMemberService.add(
            project,
            creator,
            {
                "user_id": str(user.id),
                "display_name": "注册成员展示名",
                "tags": ["translator", "proofreader"],
            },
        )
        external_member = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "外部署名", "tags": ["typesetter"]},
        )

        response = self.get(
            f"/v1/teams/{team.id}/insight/projects/{project.id}/users",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        members = {item["id"]: item for item in response.json}
        self.assertIn(str(registered_member.id), members)
        self.assertIn(str(external_member.id), members)
        self.assertEqual("注册成员展示名", members[str(registered_member.id)]["display_name"])
        self.assertEqual(["proofreader", "translator"], members[str(registered_member.id)]["tags"])
        self.assertEqual("api-identity-insight-user", members[str(registered_member.id)]["name"])
        self.assertIsNone(members[str(external_member.id)]["user_id"])
        self.assertEqual("外部署名", members[str(external_member.id)]["display_name"])
        self.assertNotIn("role", members[str(registered_member.id)])

        response = self.get(
            f"/v1/teams/{team.id}/insight/users/{user.id}/projects",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual(1, len(response.json))
        self.assertEqual(
            ["proofreader", "translator"],
            response.json[0]["member_summary"][0]["tags"],
        )
        self.assertNotIn("role", response.json[0])

    def test_project_member_changes_api_enforces_auth_and_returns_identity_fields(self):
        project = self.create_project("api-member-changes")
        creator = self.get_creator(project.team)
        creator_token = creator.generate_token()
        self.assertErrorEqual(
            self.get(f"/v1/projects/{project.id}/members"), NeedTokenError
        )

        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator_token,
            headers={"Idempotency-Key": "api-external-add"},
            json={
                "operations": [
                    {
                        "operation_id": "api-external-add",
                        "action": "add",
                        "display_name": "API External",
                    }
                ]
            },
        )
        self.assertErrorEqual(response)
        self.assertFalse(response.json["failed"])
        external_id = response.json["results"][0]["member_id"]
        member = ProjectMember.objects(id=external_id).first()
        self.assertIsNone(member.user)
        self.assertEqual([], member.to_api()["effective_permissions"])

        replay = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator_token,
            json={
                "operations": [
                    {
                        "operation_id": "api-external-add",
                        "action": "add",
                        "display_name": "different retry name",
                    }
                ]
            },
        )
        self.assertErrorEqual(replay)
        self.assertEqual(response.json["results"], replay.json["results"])
        self.assertEqual(2, ProjectMember.objects(project=project, status="active").count())

        updated = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator_token,
            json={
                "operations": [
                    {
                        "operation_id": "api-external-update",
                        "action": "update",
                        "member_id": external_id,
                        "expected_member_version": 0,
                        "changes": {"display_name": "API External Renamed"},
                    }
                ]
            },
        )
        self.assertErrorEqual(updated)
        self.assertEqual("API External Renamed", ProjectMember.objects(id=external_id).first().display_name)

        listing = self.get(
            f"/v1/projects/{project.id}/members?word=Renamed&status=active",
            token=creator_token,
        )
        self.assertErrorEqual(listing)
        self.assertEqual("1", listing.headers["X-Pagination-Count"])
        item = listing.json[0]
        self.assertEqual(external_id, item["id"])
        self.assertEqual([], item["effective_permissions"])
        self.assertNotIn("workers", item)

    def test_project_member_can_hold_multiple_worker_tags(self):
        """一个成员可同时拥有多个职位 tag（改造目标）；add 与 update 均须保留多值。"""
        project = self.create_project("api-multi-worker-tags")
        creator = self.get_creator(project.team)
        token = creator.generate_token()
        user = self.create_user("api-multi-tags-user")
        self._add_team_member(
            project.team,
            user,
            qualifications=["translator", "proofreader", "typesetter"],
        )

        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-multi-tags-add",
                        "action": "add",
                        "user_id": str(user.id),
                        "display_name": "多职位成员",
                        "tags": ["translator", "proofreader"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member_id = added.json["results"][0]["member_id"]
        member = ProjectMember.objects(id=member_id).first()
        self.assertEqual(["proofreader", "translator"], sorted(member.tags))

        updated = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-multi-tags-update",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"tags": ["translator", "proofreader", "typesetter"]},
                    }
                ]
            },
        )
        self.assertErrorEqual(updated)
        member.reload()
        self.assertEqual(
            ["proofreader", "translator", "typesetter"], sorted(member.tags)
        )

    def test_sequential_updates_with_distinct_operation_ids_all_apply(self):
        """同一成员的多次不同 update（不同 operation_id）必须全部生效。

        幂等只按 operation_id 去重；前端为同成员的每次不同修改生成不同 id，
        后端不得把第二次保存当作重放吞掉。
        """
        project = self.create_project("api-seq-updates")
        creator = self.get_creator(project.team)
        token = creator.generate_token()
        user = self.create_user("api-seq-updates-user")
        self._add_team_member(
            project.team,
            user,
            qualifications=["translator", "proofreader", "typesetter"],
        )
        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-seq-add",
                        "action": "add",
                        "user_id": str(user.id),
                        "display_name": "顺序更新成员",
                        "tags": ["translator"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member_id = added.json["results"][0]["member_id"]
        member = ProjectMember.objects(id=member_id).first()

        first = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-seq-update-1",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"tags": ["translator"]},
                    }
                ]
            },
        )
        self.assertErrorEqual(first)
        member.reload()
        self.assertEqual(["translator"], member.tags)

        # 第二次保存：追加第二个职位。不同 operation_id 必须真正执行。
        second = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-seq-update-2",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"tags": ["translator", "proofreader"]},
                    }
                ]
            },
        )
        self.assertErrorEqual(second)
        member.reload()
        self.assertEqual(["proofreader", "translator"], sorted(member.tags))

        # 重放同一 operation_id 的 update 返回原结果、不再执行（幂等语义保留）。
        replay = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-seq-update-2",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"tags": ["translator", "proofreader", "typesetter"]},
                    }
                ]
            },
        )
        self.assertErrorEqual(replay)
        self.assertEqual(second.json["results"], replay.json["results"])
        member.reload()
        self.assertEqual(["proofreader", "translator"], sorted(member.tags))

    def test_self_demotion_of_identity_tags_is_blocked(self):
        """创建者/管理员不能通过成员编辑移除自己的身份 tag。

        前端已拦截此行为；后端必须兜底：管理员自我移除 admin tag → 422
        PROTECTED_IDENTITY_TAG；创建者（owner）移除 creator tag → 409
        MEMBER_ALREADY_OWNER（既有保护）。
        """
        project = self.create_project("api-self-demote")
        creator = self.get_creator(project.team)
        token = creator.generate_token()

        admin_user = self.create_user("api-self-demote-admin")
        self._add_team_member(project.team, admin_user, base_tag="admin")
        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-self-demote-admin-add",
                        "action": "add",
                        "user_id": str(admin_user.id),
                        "display_name": "项目管理员",
                        "tags": ["admin"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member = ProjectMember.objects(project=project, user=admin_user).first()
        self.assertEqual(["admin"], member.tags)

        demoted = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=admin_user.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-self-demote-admin-update",
                        "action": "update",
                        "member_id": str(member.id),
                        "expected_member_version": member.version,
                        "changes": {"tags": []},
                    }
                ]
            },
        )
        self.assertEqual(422, demoted.status_code)
        self.assertEqual("PROTECTED_IDENTITY_TAG", demoted.json["failed"]["code"])
        member.reload()
        self.assertEqual(["admin"], member.tags)

        creator_member = ProjectMember.objects(project=project, user=creator).first()
        demoted_creator = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-self-demote-creator-update",
                        "action": "update",
                        "member_id": str(creator_member.id),
                        "expected_member_version": creator_member.version,
                        "changes": {"tags": []},
                    }
                ]
            },
        )
        self.assertEqual(409, demoted_creator.status_code)
        self.assertEqual("MEMBER_ALREADY_OWNER", demoted_creator.json["failed"]["code"])
        creator_member.reload()
        self.assertIn("creator", creator_member.tags)

    def test_identity_changes_require_project_creator(self):
        """更改他人项目身份（授予/移除 admin）仅项目创建者（owner）可执行。

        前端按「只有 creator 可以修改项目身份」拆分编辑权限；后端兜底：
        非 owner 移除他人身份 tag → 422 PROTECTED_IDENTITY_TAG。
        """
        project = self.create_project("api-identity-creator-only")
        creator = self.get_creator(project.team)
        token = creator.generate_token()

        admin_user = self.create_user("api-identity-admin-user")
        self._add_team_member(project.team, admin_user, base_tag="admin")
        other_user = self.create_user("api-identity-other-user")
        self._add_team_member(project.team, other_user, base_tag="member")
        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-identity-owner-add-admin",
                        "action": "add",
                        "user_id": str(other_user.id),
                        "display_name": "被管理员",
                        "tags": ["admin"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member = ProjectMember.objects(project=project, user=other_user).first()
        self.assertEqual(["admin"], member.tags)

        # 团队 admin（非项目 owner）降级他人 admin → 拒绝（422）。
        demoted = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=admin_user.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-identity-admin-demote",
                        "action": "update",
                        "member_id": str(member.id),
                        "expected_member_version": member.version,
                        "changes": {"tags": []},
                    }
                ]
            },
        )
        self.assertEqual(422, demoted.status_code)
        self.assertEqual("PROTECTED_IDENTITY_TAG", demoted.json["failed"]["code"])
        member.reload()
        self.assertEqual(["admin"], member.tags)

        # 项目创建者移除他人 admin → 允许。
        ok = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-identity-owner-demote",
                        "action": "update",
                        "member_id": str(member.id),
                        "expected_member_version": member.version,
                        "changes": {"tags": []},
                    }
                ]
            },
        )
        self.assertErrorEqual(ok)
        member.reload()
        self.assertEqual([], member.tags)

    def test_identity_object_endpoints_reject_non_object_json(self):
        project = self.create_project("api-malformed-identity-json")
        team = project.team
        creator = self.get_creator(team)
        token = creator.generate_token()

        member_response = self.post(
            f"/v1/teams/{team.id}/members",
            token=token,
            data="[]",
            headers={"Content-Type": "application/json"},
        )
        self.assertErrorEqual(member_response, InvalidIdentityRequestError)

        policy_response = self.patch(
            f"/v1/teams/{team.id}/identity-tag-policy",
            token=token,
            data="[]",
            headers={"Content-Type": "application/json"},
        )
        self.assertErrorEqual(policy_response, InvalidIdentityRequestError)

    def test_project_member_changes_rejects_malformed_tag_values(self):
        project = self.create_project("api-malformed-identity-tags")
        creator = self.get_creator(project.team)
        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-malformed-tags",
                        "action": "add",
                        "display_name": "非法标签成员",
                        "tags": "translator",
                    }
                ]
            },
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_REQUEST", response.json["failed"]["code"])
        self.assertEqual(
            0,
            ProjectMember.objects(
                project=project, display_name="非法标签成员"
            ).count(),
        )

    def test_project_member_changes_can_restore_the_same_removed_identity(self):
        project = self.create_project("api-member-restore")
        creator = self.get_creator(project.team)
        token = creator.generate_token()
        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-restore-add",
                        "action": "add",
                        "display_name": "待移除外部署名",
                        "tags": ["translator"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member_id = added.json["results"][0]["member_id"]
        member = ProjectMember.objects(id=member_id).first()
        self.assertIsNotNone(member)

        removed = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-restore-remove",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"status": "removed"},
                    }
                ]
            },
        )
        self.assertErrorEqual(removed)
        member.reload()
        self.assertEqual("removed", member.status)

        restored = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-restore-member",
                        "action": "add",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "display_name": "恢复后的外部署名",
                        "tags": ["proofreader"],
                    }
                ]
            },
        )
        self.assertErrorEqual(restored)
        member.reload()
        self.assertEqual("active", member.status)
        self.assertEqual(member_id, restored.json["results"][0]["member_id"])
        self.assertEqual("恢复后的外部署名", member.display_name)
        self.assertEqual(["proofreader"], member.tags)

        removed_again = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-restore-remove-again",
                        "action": "update",
                        "member_id": member_id,
                        "expected_member_version": member.version,
                        "changes": {"status": "removed"},
                    }
                ]
            },
        )
        self.assertErrorEqual(removed_again)
        member.reload()
        conflict = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=token,
            json={
                "operations": [
                    {
                        "operation_id": "api-restore-stale",
                        "action": "add",
                        "member_id": member_id,
                        "expected_member_version": member.version - 1,
                    }
                ]
            },
        )
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("PROJECT_MEMBER_VERSION_CONFLICT", conflict.json["failed"]["code"])

    def test_project_member_invite_projection_and_lifecycle_api(self):
        project = self.create_project("api-invitation-lifecycle")
        team = project.team
        creator = self.get_creator(team)
        operator = self.create_user("api-invitation-operator")
        self._add_team_member(team, operator)
        target = self.create_user("api-invitation-target")

        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=operator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-invite-target",
                        "action": "add",
                        "user_id": str(target.id),
                    }
                ]
            },
        )
        self.assertErrorEqual(response)
        member = ProjectMember.objects(project=project, user=target).first()
        self.assertEqual("invited", member.status)
        self.assertTrue(response.json["results"][0]["member_status"] == "invited")

        complete_response = self.post(
            f"/v1/projects/{project.id}/complete",
            token=creator.generate_token(),
            json={},
        )
        self.assertErrorEqual(complete_response)
        self.assertEqual("COMPLETED", complete_response.json["status"])
        # Single-project lifecycle responses carry the member summary like the
        # list endpoints (regression: an undefined memberSummary froze the
        # frontend member stats in an infinite render loop).
        self.assertIn("member_summary", complete_response.json["project"])
        project.reload()
        self.assertEqual("COMPLETED", project.to_api()["identity_status"])
        reopen_response = self.post(
            f"/v1/projects/{project.id}/reopen",
            token=creator.generate_token(),
            json={},
        )
        self.assertErrorEqual(reopen_response)
        self.assertEqual("NORMAL", reopen_response.json["status"])
        self.assertIn("member_summary", reopen_response.json["project"])
        project.reload()
        self.assertEqual("NORMAL", project.to_api()["identity_status"])

        project_member = ProjectMember.objects(project=project, user=creator).first()
        self.assertIsNotNone(project_member)
        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-remove-owner",
                        "action": "update",
                        "member_id": str(project_member.id),
                        "expected_member_version": project_member.version,
                        "changes": {"status": "removed"},
                    }
                ]
            },
        )
        self.assertTrue(response.status_code >= 400)
        self.assertEqual("MEMBER_ALREADY_OWNER", response.json["failed"]["code"])

    def test_project_member_merge_api_requires_explicit_name_and_tag_choice(self):
        project = self.create_project("api-member-merge")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("api-member-merge-target")
        self._add_team_member(team, target, qualifications=["translator", "proofreader"])
        target_member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(target.id), "display_name": "已有成员", "tags": ["translator"]},
        )
        source = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "外部成员", "tags": ["proofreader"]},
        )

        response = self.post(
            f"/v1/projects/{project.id}/members/{source.id}/merge",
            token=creator.generate_token(),
            json={
                "target_member_id": str(target_member.id),
                "expected_source_version": source.version,
                "expected_target_version": target_member.version,
                "display_name": "最终成员名",
                "tags": ["translator", "proofreader"],
            },
        )
        self.assertErrorEqual(response)
        self.assertEqual(str(target_member.id), response.json["member"]["id"])
        self.assertEqual("最终成员名", response.json["member"]["display_name"])
        self.assertEqual("removed", response.json["source_member"]["status"])

    def test_team_member_policy_api_and_project_search_filter_before_pagination(self):
        project = self.create_project("api-search-project")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("api-search-user")
        relation = self._add_team_member(team, user, qualifications=["translator"])
        ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(user.id), "display_name": "Project Alias", "tags": ["translator"]},
        )
        second_project = Project.create(
            "api-search-project-second",
            team=team,
            project_set=project.project_set,
            creator=creator,
        )
        ProjectMemberService.add(
            second_project,
            creator,
            {
                "user_id": str(user.id),
                "display_name": "Project Alias Second",
                "tags": ["translator"],
            },
        )

        response = self.get(f"/v1/teams/{team.id}/members", token=creator.generate_token())
        self.assertErrorEqual(response)
        self.assertGreaterEqual(int(response.headers["X-Pagination-Count"]), 2)

        response = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}",
            token=creator.generate_token(),
            json={
                "expected_version": relation.version,
                "worker_qualifications": ["translator"],
            },
        )
        self.assertErrorEqual(response)
        relation.reload()
        self.assertEqual(["translator"], relation.worker_qualifications)

        response = self.patch(
            f"/v1/teams/{team.id}/identity-tag-policy",
            token=creator.generate_token(),
            json={
                "expected_version": 0,
                "upserts": [
                    {
                        "scope": "project",
                        "code": "quality",
                        "name": "Quality",
                        "permissions": ["project:CHECK_TRA"],
                    }
                ],
            },
        )
        self.assertErrorEqual(response)
        self.assertEqual("team", response.json["project_tags"]["quality"]["source"])
        self.assertEqual(1, response.json["version"])

        response = self.get(
            f"/v1/teams/{team.id}/projects?mode=search-worker&worker_name=Project%20Alias&tag=translator&page=1&limit=1",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual("2", response.headers["X-Pagination-Count"])
        first_page_id = response.json[0]["id"]
        self.assertIn(first_page_id, {str(project.id), str(second_project.id)})

        response = self.get(
            f"/v1/teams/{team.id}/projects?mode=search-worker&worker_name=Project%20Alias&tag=translator&page=2&limit=1",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual("2", response.headers["X-Pagination-Count"])
        self.assertEqual(1, len(response.json))
        self.assertIn(response.json[0]["id"], {str(project.id), str(second_project.id)})
        self.assertNotEqual(first_page_id, response.json[0]["id"])

        outsider = self.create_user("api-search-outsider")
        response = self.get(
            f"/v1/teams/{team.id}/identity-tag-policy", token=outsider.generate_token()
        )
        self.assertErrorEqual(response, NoPermissionError)

    def test_team_project_search_uses_normalized_name_projection(self):
        project = self.create_project("Case-Insensitive-ABC")
        creator = self.get_creator(project.team)

        response = self.get(
            f"/v1/teams/{project.team.id}/projects"
            "?mode=search-project-name&word=abc",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual("1", response.headers["X-Pagination-Count"])
        self.assertEqual(str(project.id), response.json[0]["id"])

        project.update(name="Renamed XYZ")
        response = self.get(
            f"/v1/teams/{project.team.id}/projects"
            "?mode=search-project-name&word=xyz",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual("1", response.headers["X-Pagination-Count"])

    def test_team_member_list_supports_comma_separated_status(self):
        """GET /v1/teams/{id}/members?status=active,removed (the frontend's
        one-request pattern) must return both active and removed members
        instead of matching the comma-joined string as a single status.
        """
        team = self.create_team("api-team-comma-status")
        creator = self.get_creator(team)
        active_user = self.create_user("api-team-comma-active")
        removed_user = self.create_user("api-team-comma-removed")
        self._add_team_member(team, active_user, base_tag="member")
        removed = self._add_team_member(team, removed_user, base_tag="member")

        response = self.delete(
            f"/v1/teams/{team.id}/members/{removed.id}",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)

        response = self.get(
            f"/v1/teams/{team.id}/members?status=active%2Cremoved&limit=3000",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        names = {item["user"]["name"] for item in response.json}
        self.assertIn(active_user.name, names)
        self.assertIn(removed_user.name, names)

    def test_completed_project_filter_includes_completed_and_cleared_projects(self):
        completed_project = self.create_project("api-filter-completed")
        creator = self.get_creator(completed_project.team)
        cleared_project = Project.create(
            "api-filter-cleared",
            completed_project.team,
            project_set=completed_project.project_set,
            creator=creator,
        )

        ProjectLifecycleService.complete(completed_project, creator, {})
        ProjectLifecycleService.clear(cleared_project, creator, {})

        response = self.get(
            "/v1/user/projects?status=COMPLETED&limit=100",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual(
            {str(completed_project.id), str(cleared_project.id)},
            {item["id"] for item in response.json},
        )
        # The list contract keeps only the card fields; detail-only status
        # aliases are intentionally omitted.
        self.assertTrue(all("identity_status" not in item for item in response.json))

        response = self.get(
            f"/v1/teams/{completed_project.team.id}/projects"
            f"?mode=search-project-name&status=COMPLETED&limit=100",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual(
            {str(completed_project.id), str(cleared_project.id)},
            {item["id"] for item in response.json},
        )

        response = self.get(
            f"/v1/teams/{completed_project.team.id}/projects"
            f"?mode=search-project-name&status=CLEARED&limit=100",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual([str(cleared_project.id)], [item["id"] for item in response.json])

    def test_project_list_rejects_legacy_numeric_status_values(self):
        project = self.create_project("api-filter-rejects-numeric-status")
        creator = self.get_creator(project.team)

        response = self.get(
            "/v1/user/projects?status=1",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response, InvalidIdentityRequestError)

        response = self.get(
            f"/v1/teams/{project.team.id}/projects"
            "?mode=search-project-name&status=1",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response, InvalidIdentityRequestError)

    def test_member_list_carries_site_user_identity_and_skips_permission_snapshot(self):
        """成员列表返回注册用户的站点身份，但不做逐行权限快照计算。"""
        project = self.create_project("api-member-list-user")
        creator = self.get_creator(project.team)
        registered = self.create_user("api-member-list-registered")
        ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(registered.id), "display_name": "同名展示", "tags": []},
        )
        external = ProjectMemberService.add(
            project, creator, {"display_name": "同名展示", "tags": []}
        )
        ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(external.id),
                "expected_member_version": external.version,
                "changes": {"status": "removed"},
            },
        )

        # All member states come back from a single comma-separated request.
        # (Three rows: the project creator, the registered member and the
        # removed external member.)
        response = self.get(
            f"/v1/projects/{project.id}/members"
            "?status=active,invited,removed&limit=100",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response)
        self.assertEqual(3, len(response.json))
        registered_item = next(
            item for item in response.json
            if (item.get("user") or {}).get("name") == "api-member-list-registered"
        )
        self.assertEqual(str(registered.id), registered_item["user"]["id"])
        self.assertEqual([], registered_item["effective_permissions"])
        self.assertNotIn("email", registered_item["user"])
        external_item = next(
            item for item in response.json
            if not item["user"] and item["status"] == "removed"
        )
        self.assertEqual("同名展示", external_item["display_name"])
        self.assertEqual(
            {"active", "removed"},
            {item["status"] for item in response.json},
        )

    # ===== 邀请接口的当前身份系统 =====

    def test_project_invitation_with_position_tags_joins_qualified_team_member(self):
        """项目邀请携带职位标签时，合格的团队成员直接加入并带标签。"""
        project = self.create_project("api-invitation-position-join")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("api-invitation-position-target")
        self._add_team_member(
            team, target, qualifications=["translator", "proofreader"]
        )

        response = self.post(
            f"/v1/projects/{project.id}/invitations",
            token=creator.generate_token(),
            json={
                "user_id": str(target.id),
                "tags": ["translator", "proofreader"],
                "message": "来做校对",
            },
        )
        self.assertErrorEqual(response)
        member = ProjectMember.objects(project=project, user=target).first()
        self.assertIsNotNone(member)
        self.assertEqual("active", member.status)
        self.assertEqual(["proofreader", "translator"], member.tags)
        self.assertIn("project", response.json)

    def test_project_invitation_position_requires_team_qualification(self):
        """qualified 模式下，无对应资格的非管理成员不能被邀请担任职位。"""
        project = self.create_project("api-invitation-position-qualified")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("api-invitation-position-unqualified")
        self._add_team_member(team, target)

        response = self.post(
            f"/v1/projects/{project.id}/invitations",
            token=creator.generate_token(),
            json={
                "user_id": str(target.id),
                "tags": ["translator"],
                "message": "",
            },
        )
        self.assertErrorEqual(response, TeamQualificationRequiredError)
        self.assertIsNone(ProjectMember.objects(project=project, user=target).first())

    def test_open_mode_skips_qualification_for_project_invitation_positions(self):
        """团队创建者开启全员模式后，无资格成员也可被邀请担任任意职位。"""
        project = self.create_project("api-invitation-position-open")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("api-invitation-position-open-target")
        self._add_team_member(team, target)

        switched = self.put(
            f"/v1/teams/{team.id}",
            token=creator.generate_token(),
            json={"worker_qualification_mode": "open"},
        )
        self.assertErrorEqual(switched)
        team.reload()
        self.assertEqual("open", team.worker_qualification_mode)

        response = self.post(
            f"/v1/projects/{project.id}/invitations",
            token=creator.generate_token(),
            json={
                "user_id": str(target.id),
                "tags": ["translator"],
                "message": "",
            },
        )
        self.assertErrorEqual(response)
        member = ProjectMember.objects(project=project, user=target).first()
        self.assertIsNotNone(member)
        self.assertEqual("active", member.status)
        self.assertEqual(["translator"], member.tags)

    def test_team_worker_qualification_mode_is_creator_only(self):
        """工作人员资格校验模式只允许团队创建者修改。"""
        team = self.create_team("api-wqm-creator-only")
        creator = self.get_creator(team)
        admin = self.create_user("api-wqm-creator-only-admin")
        self._add_team_member(team, admin, base_tag="admin")

        denied = self.put(
            f"/v1/teams/{team.id}",
            token=admin.generate_token(),
            json={"worker_qualification_mode": "open"},
        )
        self.assertErrorEqual(denied, NoPermissionError)
        team.reload()
        self.assertEqual("qualified", team.worker_qualification_mode)

        allowed = self.put(
            f"/v1/teams/{team.id}",
            token=creator.generate_token(),
            json={"worker_qualification_mode": "open"},
        )
        self.assertErrorEqual(allowed)
        team.reload()
        self.assertEqual("open", team.worker_qualification_mode)
        fetched = self.get(f"/v1/teams/{team.id}", token=creator.generate_token())
        self.assertErrorEqual(fetched)
        self.assertEqual("open", fetched.json["worker_qualification_mode"])

    def test_pending_project_invitation_updates_positions_via_tags(self):
        """修改待处理项目邀请的职位会投影到 invited 成员，列表带 tags。"""
        project = self.create_project("api-invitation-position-put")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("api-invitation-position-put-target")
        # 非团队创建者操作者走邀请生命周期（团队创建者会直接拉入为 active）。
        operator = self.create_user("api-invitation-position-put-operator")
        self._add_team_member(team, operator, qualifications=["translator"])

        # 无标签邀请（非团队成员走邀请生命周期 → invited 投影）。
        response = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=operator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-invitation-position-put-add",
                        "action": "add",
                        "user_id": str(target.id),
                    }
                ]
            },
        )
        self.assertErrorEqual(response)
        member = ProjectMember.objects(project=project, user=target).first()
        self.assertEqual("invited", member.status)
        invitation = Invitation.objects(
            user=target, group=project, status=InvitationStatus.PENDING
        ).first()
        self.assertIsNotNone(invitation)

        # 让目标成为团队正式成员（带翻译资格），随后可修改邀请职位。
        self._add_team_member(team, target, qualifications=["translator"])
        changed = self.put(
            f"/v1/invitations/{invitation.id}",
            token=creator.generate_token(),
            json={"tags": ["translator"]},
        )
        self.assertErrorEqual(changed)
        member.reload()
        self.assertEqual(["translator"], member.tags)

        listed = self.get(
            f"/v1/projects/{project.id}/invitations",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(listed)
        item = next(
            item for item in listed.json
            if item["id"] == str(invitation.id)
        )
        self.assertEqual(["translator"], item["tags"])

    def test_team_invitation_still_requires_legacy_role(self):
        """团队邀请仍走旧角色路径，缺 role_id 会被校验拒绝。"""
        team = self.create_team("api-invitation-team-role")
        creator = self.get_creator(team)
        target = self.create_user("api-invitation-team-role-target")

        denied = self.post(
            f"/v1/teams/{team.id}/invitations",
            token=creator.generate_token(),
            json={
                "user_id": str(target.id),
                "tags": ["translator"],
                "message": "",
            },
        )
        self.assertEqual(400, denied.status_code)

    # ---- 团队默认展示名（default_display_name）----

    def test_team_member_self_edits_own_default_display_name(self):
        """普通成员可通过专属接口编辑自己的加入项目默认展示名。"""
        team = self.create_team("api-ddn-self")
        member_user = self.create_user("api-ddn-self-user")
        relation = self._add_team_member(team, member_user)

        response = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/default-display-name",
            token=member_user.generate_token(),
            json={"default_display_name": "  我的项目署名  ", "expected_version": relation.version},
        )
        self.assertErrorEqual(response)
        self.assertEqual("我的项目署名", response.json["member"]["default_display_name"])
        self.assertEqual("我的项目署名", relation.reload().default_display_name)

        # 清空 = 恢复未设置（加入项目时回退到注册用户名）
        cleared = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/default-display-name",
            token=member_user.generate_token(),
            json={"default_display_name": "", "expected_version": relation.version},
        )
        self.assertErrorEqual(cleared)
        self.assertEqual("", relation.reload().default_display_name)

    def test_team_member_default_display_name_permissions_and_validation(self):
        """默认展示名接口：越权/版本/长度校验。"""
        team = self.create_team("api-ddn-perm")
        creator = self.get_creator(team)
        member_user = self.create_user("api-ddn-perm-member")
        relation = self._add_team_member(team, member_user)
        other = self.create_user("api-ddn-perm-other")
        other_relation = self._add_team_member(team, other)

        # 普通成员不能修改其他成员
        denied = self.patch(
            f"/v1/teams/{team.id}/members/{other_relation.id}/default-display-name",
            token=member_user.generate_token(),
            json={"default_display_name": "越权", "expected_version": other_relation.version},
        )
        self.assertErrorEqual(denied, NoPermissionError)

        # 非团队成员不能修改任何人的
        outsider = self.create_user("api-ddn-perm-outsider")
        denied2 = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/default-display-name",
            token=outsider.generate_token(),
            json={"default_display_name": "外人", "expected_version": relation.version},
        )
        self.assertErrorEqual(denied2, NoPermissionError)

        # 超长拒绝
        too_long = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/default-display-name",
            token=member_user.generate_token(),
            json={"default_display_name": "长" * 141, "expected_version": relation.version},
        )
        self.assertErrorEqual(too_long, InvalidIdentityRequestError)

        # 过期版本拒绝
        stale = self.patch(
            f"/v1/teams/{team.id}/members/{relation.id}/default-display-name",
            token=member_user.generate_token(),
            json={"default_display_name": "新名", "expected_version": relation.version - 1},
        )
        self.assertErrorEqual(stale, IdentityVersionConflictError)

        # 团队创建者可代设其他成员
        ok = self.patch(
            f"/v1/teams/{team.id}/members/{other_relation.id}/default-display-name",
            token=creator.generate_token(),
            json={"default_display_name": "管理员代设", "expected_version": other_relation.version},
        )
        self.assertErrorEqual(ok)
        self.assertEqual("管理员代设", other_relation.reload().default_display_name)

    def test_project_application_allow_fills_team_default_display_name(self):
        """主动加入项目：批准申请时新成员默认使用团队默认展示名。"""
        project = self.create_project("api-ddn-application")
        project.allow_apply_type = ProjectAllowApplyType.ALL
        project.save()
        creator = self.get_creator(project.team)
        applicant = self.create_user("api-ddn-application-applicant")
        self._add_team_member(project.team, applicant)
        TeamMember.objects(team=project.team, user=applicant).update(
            set__default_display_name="我的加入署名"
        )

        created = self.post(
            f"/v1/projects/{project.id}/applications",
            token=applicant.generate_token(),
            json={"message": "请加入"},
        )
        self.assertErrorEqual(created)
        application = Application.objects(group=project, user=applicant).first()
        self.assertIsNotNone(application)

        allowed = self.patch(
            f"/v1/applications/{application.id}",
            token=creator.generate_token(),
            json={"allow": True},
        )
        self.assertErrorEqual(allowed)
        member = ProjectMember.objects(project=project, user=applicant).first()
        self.assertEqual("active", member.status)
        self.assertEqual("我的加入署名", member.display_name)

    def test_team_default_display_name_used_when_adding_registered_user(self):
        """被邀请加入：添加注册成员时默认使用团队默认展示名。

        前端对新成员总是预填站点用户名，因此显式值等于站点用户名时也
        视为隐式默认，命中团队默认展示名；显式不同的展示名优先；
        未设置默认时回退到站点用户名（既有行为不变）。
        """
        project = self.create_project("api-ddn-add")
        creator = self.get_creator(project.team)
        target = self.create_user("api-ddn-add-target")
        self._add_team_member(project.team, target, qualifications=["translator"])
        TeamMember.objects(team=project.team, user=target).update(
            set__default_display_name="团队默认展示名"
        )

        added = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-ddn-add",
                        "action": "add",
                        "user_id": str(target.id),
                        "display_name": target.name,
                        "tags": ["translator"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added)
        member = ProjectMember.objects(project=project, user=target).first()
        self.assertEqual("active", member.status)
        self.assertEqual("团队默认展示名", member.display_name)

        # 显式提供不同的展示名时团队默认不覆盖
        explicit = self.create_user("api-ddn-add-explicit")
        self._add_team_member(project.team, explicit, qualifications=["translator"])
        TeamMember.objects(team=project.team, user=explicit).update(
            set__default_display_name="团队默认展示名"
        )
        added2 = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-ddn-add-explicit",
                        "action": "add",
                        "user_id": str(explicit.id),
                        "display_name": "特邀署名",
                        "tags": ["translator"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added2)
        self.assertEqual(
            "特邀署名",
            ProjectMember.objects(project=project, user=explicit).first().display_name,
        )

        # 未设置默认展示名 → 回退到站点用户名
        plain = self.create_user("api-ddn-add-plain")
        self._add_team_member(project.team, plain, qualifications=["translator"])
        added3 = self.post(
            f"/v1/projects/{project.id}/members/changes",
            token=creator.generate_token(),
            json={
                "operations": [
                    {
                        "operation_id": "api-ddn-add-plain",
                        "action": "add",
                        "user_id": str(plain.id),
                        "display_name": plain.name,
                        "tags": ["translator"],
                    }
                ]
            },
        )
        self.assertErrorEqual(added3)
        self.assertEqual(
            plain.name,
            ProjectMember.objects(project=project, user=plain).first().display_name,
        )
