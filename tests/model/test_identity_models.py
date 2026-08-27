import unicodedata

from mongoengine.errors import NotUniqueError
from mongoengine.connection import get_db

from app.constants.project import ProjectStatus
from app.exceptions import AliasValidationError, InvalidIdentityRequestError
from app.models.identity_tag import IdentityTagPolicy, normalize_tag_list
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team_member import TeamMember
from app.services.identity_permission import (
    IdentityPermissionService,
    normalize_aliases,
)
from app.services.project_member import ProjectMemberService
from app.services.user_alias import UserAliasService
from tests import MoeTestCase


class IdentityModelTestCase(MoeTestCase):
    def test_identity_indexes_use_the_migration_names(self):
        project = self.create_project("identity-index-names")
        ProjectMember(project=project, external_id="index-external", display_name="外部").save()
        ProjectMember(project=project, user=self.create_user("index-user"), display_name="注册").save()

        project_indexes = get_db().project_member.index_information()
        team_indexes = get_db().team_member.index_information()
        self.assertIn("project_member_identity_key_v1", project_indexes)
        self.assertIn("project_member_project_user_v1", project_indexes)
        self.assertIn("project_member_project_external_v1", project_indexes)
        self.assertIn("project_member_status_v1", project_indexes)
        self.assertIn("project_member_tags_v1", project_indexes)
        self.assertIn("project_member_display_name_v1", project_indexes)
        self.assertIn("team_member_team_user_v1", team_indexes)
        self.assertNotIn("ik_1", project_indexes)
        self.assertNotIn("project_1_u_1", project_indexes)
        self.assertNotIn("team_1_u_1", team_indexes)
        self.assertTrue(project_indexes["project_member_project_user_v1"].get("unique"))
        self.assertTrue(project_indexes["project_member_project_external_v1"].get("unique"))

    def test_tag_normalization_rejects_non_array_and_non_string_values(self):
        with self.assertRaises(InvalidIdentityRequestError):
            normalize_tag_list("translator")
        with self.assertRaises(InvalidIdentityRequestError):
            normalize_tag_list(["translator", 1])

    def test_project_member_requires_one_subject_and_has_logical_unique_key(self):
        project = self.create_project("identity-model-project")
        creator = self.get_creator(project.team)

        with self.assertRaises(ValueError):
            ProjectMember(project=project, display_name="missing").save()
        with self.assertRaises(ValueError):
            ProjectMember(
                project=project,
                user=creator,
                external_id="also-present",
                display_name="both",
            ).save()

        external = ProjectMember(
            project=project,
            external_id="external-1",
            display_name="外部署名",
        ).save()
        self.assertEqual(
            f"{project.id}:e:external-1", external.identity_key
        )
        with self.assertRaises(NotUniqueError):
            ProjectMember(
                project=project,
                external_id="external-1",
                display_name="重复外部署名",
            ).save()

        registered_user = self.create_user("identity-registered-member")
        registered = ProjectMember(
            project=project,
            user=registered_user,
            display_name=registered_user.name,
        ).save()
        self.assertEqual(
            f"{project.id}:u:{registered_user.id}", registered.identity_key
        )
        with self.assertRaises(NotUniqueError):
            ProjectMember(
                project=project,
                user=registered_user,
                display_name=registered_user.name,
            ).save()

        external.status = "removed"
        external.save()
        self.assertIsNotNone(external.removed_time)
        external.status = "active"
        external.save()
        self.assertIsNone(external.removed_time)

    def test_alias_normalization_is_nfc_case_insensitive_and_scoped(self):
        decomposed = "e\u0301"
        aliases = normalize_aliases(
            [decomposed, " É ", "署名", "署名"], name="alice"
        )
        self.assertEqual(["é", "署名"], aliases)
        self.assertEqual(aliases[0], unicodedata.normalize("NFC", decomposed))
        self.assertEqual(aliases[0], unicodedata.normalize("NFC", aliases[0]))

        with self.assertRaises(AliasValidationError):
            normalize_aliases([" Alice "], name="alice")
        with self.assertRaises(AliasValidationError):
            normalize_aliases(["a", "b", "c"], max_count=2)
        with self.assertRaises(AliasValidationError):
            normalize_aliases(["x" * 65])

        user = self.create_user("identity-alias-user")
        self.app.config["MAX_USER_ALIASES"] = 2
        with self.assertRaises(AliasValidationError):
            UserAliasService.replace(
                user, user, ["站点一", "站点二", "站点三"]
            )
        UserAliasService.replace(user, user, [decomposed, "署名"])
        user.reload()
        self.assertEqual(["é", "署名"], user.aliases)

        team = self.create_team("identity-alias-team")
        relation = TeamMember(team=team, user=user, aliases=[decomposed, "署名"]).save()
        self.assertEqual(["é", "署名"], relation.aliases)
        self.assertEqual([], user.aliases[2:] if len(user.aliases) > 2 else [])

    def test_policy_is_team_scoped_and_project_status_mapping_is_explicit(self):
        project = self.create_project("identity-status-project")
        team = project.team
        policy = IdentityTagPolicy(
            team=team,
            project_tags={
                "letterer": {
                    "name": "特别嵌字",
                    "permissions": ["project:ACCESS"],
                    "assignable": True,
                }
            },
        ).save()
        self.assertEqual(
            "project:ACCESS",
            IdentityPermissionService.tag_definition(
                team, "project", "letterer"
            )["permissions"][0],
        )
        self.assertIsNone(
            IdentityPermissionService.tag_definition(
                self.create_team("identity-other-team"), "project", "letterer"
            )
        )
        self.assertEqual(0, policy.version)

        project.status = ProjectStatus.CLEARED
        project.save()
        self.assertEqual("CLEARED", IdentityPermissionService.project_status_name(project))
        self.assertEqual("CLEARED", project.to_api()["identity_status"])
        project.status = ProjectStatus.COMPLETED
        project.save()
        self.assertEqual("COMPLETED", IdentityPermissionService.project_status_name(project))
        self.assertEqual("COMPLETED", project.to_api()["status_name"])

    def test_project_member_to_api_carries_site_user_identity(self):
        """成员管理需要区分同名展示名背后的注册用户；外部署名则无 user 子对象。"""
        project = self.create_project("to-api-user-identity")
        creator = self.get_creator(project.team)
        registered = self.create_user("to-api-registered")

        external = ProjectMemberService.add(
            project, creator, {"display_name": "同一署名", "tags": []}
        )
        external_api = external.to_api()
        self.assertIsNone(external_api["user"])
        self.assertEqual("同一署名", external_api["display_name"])

        internal = ProjectMemberService.add(
            project, creator, {"user_id": str(registered.id), "display_name": "同一署名", "tags": []}
        )
        internal.reload()
        internal_api = internal.to_api()
        self.assertEqual(str(registered.id), internal_api["user"]["id"])
        self.assertEqual("to-api-registered", internal_api["user"]["name"])
        self.assertEqual([], internal_api["user"]["aliases"])
        self.assertNotIn("email", internal_api["user"])

    def test_project_member_to_api_can_skip_permission_snapshot(self):
        """列表接口不消费逐行权限快照；include_permissions=False 必须返回空权限。"""
        from app.services.identity_permission import IdentityPermissionService

        project = self.create_project("to-api-skip-permissions")
        creator = self.get_creator(project.team)
        snapshot_calls = []
        original = IdentityPermissionService.project_snapshot
        try:

            def counting_snapshot(*args, **kwargs):
                snapshot_calls.append(args)
                return original(*args, **kwargs)

            IdentityPermissionService.project_snapshot = counting_snapshot
            member = ProjectMemberService.for_user(project, creator)
            payload = member.to_api(include_permissions=False)
            self.assertEqual([], payload["effective_permissions"])
            self.assertEqual([], snapshot_calls)
        finally:
            IdentityPermissionService.project_snapshot = original

    def test_member_summaries_include_invited_members(self):
        """列表摘要必须带上邀请中(invited)成员，供前端职位图标区分邀请色。"""
        project = self.create_project("summary-invited-project")
        plain_project = self.create_project("summary-plain-project")
        ProjectMember(
            project=project,
            user=self.create_user("summary-active-user"),
            display_name="活跃成员",
            tags=["translator"],
            status="active",
        ).save()
        ProjectMember(
            project=project,
            user=self.create_user("summary-invited-user"),
            display_name="邀请中成员",
            tags=["typesetter"],
            status="invited",
        ).save()

        summaries = ProjectMemberService.member_summaries([project, plain_project])
        statuses = {item["status"] for item in summaries[str(project.id)]}
        self.assertIn("active", statuses)
        self.assertIn("invited", statuses)
        # 每个项目都有 create_project 自动创建的 creator 成员（active），
        # plain_project 无邀请成员，故其摘要只含 active。
        plain_statuses = {item["status"] for item in summaries[str(plain_project.id)]}
        self.assertEqual({"active"}, plain_statuses)

    def test_batch_project_api_matches_single_project_api(self):
        """批量列表必须复用查询，但不能改变逐行序列化的权限语义。"""
        project = self.create_project("batch-api-project")
        creator = self.get_creator(project.team)

        single = project.to_api(user=creator)
        batch = Project.batch_to_api([project], creator)[0]
        self.assertEqual(single, batch)

    def test_team_member_to_api_supports_batch_user_map(self):
        """团队列表批量预取 user 后，to_api(user_map=...) 必须与默认一致。

        成员管理列表会一次性加载上千团队成员；逐条 `member.user` 是惰性
        引用（N+1）。API 层用一次 ``User.objects(id__in=...)`` 预取后传入
        ``user_map``，断言命中预取 user 的响应与默认逐条序列化完全一致，
        且 user_map 缺 id 时仍回退到默认惰性加载而不抛错。
        """
        from app.models.team_member import TeamMember
        from app.models.user import User

        team = self.create_team("team-member-to-api-map")
        creator = self.get_creator(team)
        member_user = self.create_user("team-member-to-api-map-user")
        relation = TeamMember.objects(team=team, user=creator).first()
        self.assertIsNotNone(relation)

        default = relation.to_api()
        # 批量预取：一次查询取全部本页 user。
        prefetched = list(User.objects(id__in=[creator.id, member_user.id]))
        user_map = {str(user.id): user for user in prefetched}
        batched = relation.to_api(user_map=user_map)
        self.assertEqual(default, batched)
        self.assertEqual(str(creator.id), batched["user"]["id"])
        # user_map 缺少某 id 时回退到默认惰性加载，不抛错、响应不变。
        partial_map = {str(member_user.id): member_user}
        still_ok = relation.to_api(user_map=partial_map)
        self.assertEqual(default, still_ok)
