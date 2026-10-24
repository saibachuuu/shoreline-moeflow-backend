"""职位锁定（两级锁）测试：团队创建者专属的职位指派限制。"""
from app.models.identity_tag import IdentityTagPolicy
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.exceptions import NoPermissionError
from tests import MoeAPITestCase

ALL_WORKER_TAGS = [
    "raw_provider",
    "scanner",
    "cropper",
    "cleaner",
    "translator",
    "proofreader",
    "typesetter",
]


class PositionLockTestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.creator = self.create_user("lock-creator", "lock-creator@moeflow.com")
        self.padmin = self.create_user("lock-padmin", "lock-padmin@moeflow.com")
        self.worker = self.create_user("lock-worker", "lock-worker@moeflow.com")
        self.target = self.create_user("lock-target", "lock-target@moeflow.com")
        self.team = Team.create(name="lock-team", creator=self.creator)
        self.project = Project.create(name="lock-project", team=self.team)
        # 被指派对象需要持有全部工种资格，否则指派会被资格校验拦截
        for user in (self.worker, self.target):
            TeamMember.objects.create(
                team=self.team,
                user=user,
                base_tag="member",
                status="active",
                worker_qualifications=ALL_WORKER_TAGS,
            )
        for user, tags in (
            (self.padmin, ["admin"]),
            (self.worker, ["cropper"]),
            (self.target, []),
        ):
            ProjectMember.objects.create(
                project=self.project,
                user=user,
                identity_key=f"{self.project.id}:u:{user.id}",
                display_name=user.name,
                tags=tags,
                status="active",
            )

    def member_id(self, user):
        return str(
            ProjectMember.objects(project=self.project, user=user).first().id
        )

    def member_version(self, user):
        return ProjectMember.objects(project=self.project, user=user).first().version

    def set_lock(self, enabled, tags=None, user=None):
        return self.client.put(
            f"/v1/projects/{self.project.id}",
            json={"position_lock_enabled": enabled, **({"position_lock_tags": tags} if tags is not None else {})},
            headers={
                "Authorization": "Bearer "
                + (user or self.creator).generate_token(),
            },
        )

    def member_changes(self, operations, user):
        return self.client.post(
            f"/v1/projects/{self.project.id}/members/changes",
            json={"operations": operations},
            headers={
                "Authorization": "Bearer " + user.generate_token(),
                "Idempotency-Key": f"pos-lock-{abs(hash(str(operations)))}",
            },
        )

    def update_tags(self, user, target_user, tags):
        return self.member_changes(
            [
                {
                    "operation_id": f"op-{user.name}-{target_user.name}-{tags}",
                    "action": "update",
                    "member_id": self.member_id(target_user),
                    "expected_member_version": self.member_version(target_user),
                    "changes": {"tags": tags},
                }
            ],
            user,
        )

    def remove_member(self, user, target_user):
        return self.member_changes(
            [
                {
                    "operation_id": f"rm-{user.name}-{target_user.name}",
                    "action": "update",
                    "member_id": self.member_id(target_user),
                    "expected_member_version": self.member_version(target_user),
                    "changes": {"status": "removed"},
                }
            ],
            user,
        )

    def test_lock_settings_are_team_creator_only(self):
        res = self.set_lock(True, ["translator"], user=self.padmin)
        self.assertErrorEqual(res, NoPermissionError)
        res = self.set_lock(True, ["translator"])
        self.assertEqual(res.status_code, 200)
        project = Project.objects(id=self.project.id).first()
        self.assertTrue(project.position_lock_enabled)
        self.assertEqual(project.position_lock_tags, ["translator"])

    def test_second_level_lock_blocks_worker_tag(self):
        self.set_lock(True, ["translator"])
        # 未锁定的工种照常可编辑
        res = self.update_tags(self.padmin, self.worker, ["cropper", "cleaner"])
        self.assertEqual(res.status_code, 200)
        # 被锁工种的增改仅团队创建者可操作
        res = self.update_tags(self.padmin, self.target, ["translator"])
        self.assertEqual(res.json["failed"]["error_code"], 103)
        res = self.update_tags(self.creator, self.target, ["translator"])
        self.assertEqual(res.json["failed"], None)

    def test_first_level_lock_blocks_admin_and_custom_tags(self):
        # 团队自定义职位 artist
        policy = IdentityTagPolicy(team=self.team).save()
        policy.project_tags["artist"] = {
            "name": "artist",
            "permissions": ["project:ACCESS"],
            "assignable": True,
        }
        policy.save()

        self.set_lock(True, [])
        # 总开关开启后 admin 与自定义职位均仅团队创建者可指派
        res = self.update_tags(self.padmin, self.target, ["admin"])
        self.assertEqual(res.json["failed"]["error_code"], 103)
        res = self.update_tags(self.padmin, self.target, ["artist"])
        self.assertEqual(res.json["failed"]["error_code"], 103)
        res = self.update_tags(self.creator, self.target, ["artist"])
        self.assertEqual(res.json["failed"], None)

    def test_removal_of_locked_tag_member_is_locked(self):
        # worker 已持有 cropper；锁定 cropper 后其整体移除受锁
        self.set_lock(True, ["cropper"])
        res = self.remove_member(self.padmin, self.worker)
        self.assertEqual(res.json["failed"]["error_code"], 103)
        res = self.remove_member(self.creator, self.worker)
        self.assertEqual(res.json["failed"], None)
        self.assertEqual(
            ProjectMember.objects(project=self.project, user=self.worker).first().status,
            "removed",
        )

    def test_unlock_restores_normal_editing(self):
        self.set_lock(True, ["translator"])
        self.set_lock(False)
        project = Project.objects(id=self.project.id).first()
        self.assertFalse(project.position_lock_enabled)
        self.assertEqual(project.position_lock_tags, [])
        # 关锁后普通管理者恢复编辑权
        res = self.update_tags(self.padmin, self.target, ["translator"])
        self.assertEqual(res.status_code, 200)

    def test_invitation_with_locked_tag_is_rejected(self):
        self.set_lock(True, ["translator"])
        res = self.client.post(
            f"/v1/projects/{self.project.id}/invitations",
            json={"user_id": str(self.target.id), "message": "pos-lock-invite", "tags": ["translator"]},
            headers={"Authorization": "Bearer " + self.padmin.generate_token()},
        )
        self.assertErrorEqual(res, NoPermissionError)
        res = self.client.post(
            f"/v1/projects/{self.project.id}/invitations",
            json={"user_id": str(self.target.id), "message": "pos-lock-invite", "tags": ["translator"]},
            headers={"Authorization": "Bearer " + self.creator.generate_token()},
        )
        self.assertEqual(res.status_code, 200)

    def test_creator_tag_cannot_be_locked(self):
        # creator 职位不在可锁清单中（schema 只接受七工种）
        res = self.set_lock(True, ["creator"])
        self.assertEqual(res.status_code, 400)
