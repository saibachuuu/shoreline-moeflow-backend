from app.exceptions import InvalidIdentityTagError, TeamQualificationRequiredError
from app.models.identity_tag import IdentityTagPolicy
from app.models.project_member import ProjectMember
from app.services.identity_permission import IdentityPermissionService
from app.services.project_member import ProjectMemberService
from app.services.team_member import TeamMemberService
from tests import MoeTestCase


class IdentityPermissionTestCase(MoeTestCase):
    def _add_team_member(
        self,
        team,
        user,
        *,
        base_tag="member",
        qualifications=None,
        tags=None,
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

    def test_project_permission_is_union_of_active_tags_and_team_inheritance(self):
        project = self.create_project("permission-union-project")
        team = project.team
        creator = self.get_creator(team)

        worker = self.create_user("permission-union-worker")
        self._add_team_member(
            team,
            worker,
            qualifications=["translator", "proofreader"],
        )
        member = ProjectMemberService.add(
            project,
            creator,
            {
                "user_id": str(worker.id),
                "tags": ["proofreader", "translator", "translator"],
            },
        )
        self.assertEqual(["proofreader", "translator"], member.tags)
        snapshot = IdentityPermissionService.project_snapshot(worker, project)
        self.assertTrue(snapshot.has("project:ACCESS"))
        self.assertTrue(snapshot.has("project:ADD_FILE"))
        self.assertTrue(snapshot.has("project:PROOFREAD_TRA"))
        self.assertTrue(snapshot.has("project:CHECK_TRA"))
        self.assertTrue(snapshot.has("project:DELETE_TRA"))
        self.assertIn("project_tag:translator", snapshot.permission_sources["project:ADD_TRA"])
        self.assertIn("project_tag:proofreader", snapshot.permission_sources["project:CHECK_TRA"])

        plain = self.create_user("permission-union-plain")
        self._add_team_member(team, plain)
        plain_snapshot = IdentityPermissionService.project_snapshot(plain, project)
        self.assertTrue(plain_snapshot.has("project:ACCESS"))
        self.assertFalse(plain_snapshot.has("project:ADD_FILE"))
        self.assertFalse(plain_snapshot.has("project:COMPLETE_PROJECT"))

        team_admin = self.create_user("permission-union-admin")
        self._add_team_member(team, team_admin, base_tag="admin")
        admin_snapshot = IdentityPermissionService.project_snapshot(team_admin, project)
        self.assertTrue(admin_snapshot.has("project:ACCESS"))
        self.assertTrue(admin_snapshot.has("project:CHANGE"))
        self.assertTrue(admin_snapshot.has("project:COMPLETE_PROJECT"))
        self.assertTrue(admin_snapshot.has("project:MANAGE_MEMBERS"))
        self.assertFalse(admin_snapshot.is_owner)

        creator_snapshot = IdentityPermissionService.project_snapshot(creator, project)
        self.assertTrue(creator_snapshot.is_owner)
        self.assertTrue(creator_snapshot.has("project:COMPLETE_PROJECT"))

    def test_invited_removed_and_external_members_do_not_gain_tag_permissions(self):
        project = self.create_project("permission-state-project")
        invited_user = self.create_user("permission-invited")
        invited = ProjectMember(
            project=project,
            user=invited_user,
            display_name=invited_user.name,
            tags=["translator"],
            status="invited",
        ).save()
        invited_snapshot = IdentityPermissionService.project_snapshot(invited_user, project)
        self.assertTrue(invited_snapshot.has("project:ACCESS"))
        self.assertFalse(invited_snapshot.has("project:ADD_FILE"))
        self.assertFalse(invited_snapshot.has("project:ADD_TRA"))
        self.assertEqual(["project:ACCESS"], invited.to_api()["effective_permissions"])

        removed_user = self.create_user("permission-removed")
        removed = ProjectMember(
            project=project,
            user=removed_user,
            display_name=removed_user.name,
            tags=["translator"],
            status="removed",
        ).save()
        removed_snapshot = IdentityPermissionService.project_snapshot(removed_user, project)
        self.assertFalse(removed_snapshot.has("project:ACCESS"))
        self.assertEqual([], removed.to_api()["effective_permissions"])

        external = ProjectMember(
            project=project,
            external_id="external-only-permission",
            display_name="外部署名",
            tags=["translator", "typesetter"],
            status="active",
        ).save()
        self.assertEqual([], external.to_api()["effective_permissions"])
        self.assertFalse(IdentityPermissionService.can_project(None, project, "project:ACCESS"))

    def test_worker_tags_require_target_team_qualification_until_open_mode(self):
        project = self.create_project("permission-qualification-project")
        team = project.team
        creator = self.get_creator(team)
        worker = self.create_user("permission-unqualified-worker")
        relation = self._add_team_member(team, worker)

        with self.assertRaises(TeamQualificationRequiredError):
            ProjectMemberService.add(
                project,
                creator,
                {"user_id": str(worker.id), "tags": ["translator"]},
            )

        relation = TeamMemberService.update(
            team,
            relation.id,
            creator,
            {"expected_version": relation.version, "worker_qualifications": ["translator"]},
        )
        member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(worker.id), "tags": ["translator"]},
        )
        self.assertEqual(["translator"], member.tags)
        self.assertEqual(1, relation.version)

        unqualified = self.create_user("permission-open-worker")
        open_relation = self._add_team_member(team, unqualified)
        team.worker_qualification_mode = "open"
        team.save()
        open_member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(unqualified.id), "tags": ["typesetter"]},
        )
        self.assertEqual(["typesetter"], open_member.tags)
        open_relation.reload()
        self.assertEqual([], open_relation.worker_qualifications)

    def test_project_manager_can_add_worker_tag_to_own_identity(self):
        project = self.create_project("permission-manager-self-worker")
        creator = self.get_creator(project.team)

        member = ProjectMember.objects(project=project, user=creator).first()
        member = ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(member.id),
                "expected_member_version": member.version,
                "changes": {"tags": ["creator", "translator"]},
            },
        )

        self.assertEqual(["creator", "translator"], member.tags)

    def test_team_and_project_custom_tags_are_scoped_and_recomputed(self):
        project = self.create_project("permission-policy-project")
        team = project.team
        creator = self.get_creator(team)
        policy = IdentityTagPolicy(
            team=team,
            team_tags={
                "reviewer": {
                    "name": "Reviewer",
                    "permissions": ["team:INSIGHT"],
                    "assignable": True,
                }
            },
            project_tags={
                "quality": {
                    "name": "Quality",
                    "permissions": ["project:CHECK_TRA"],
                    "assignable": True,
                }
            },
        ).save()
        user = self.create_user("permission-policy-user")
        relation = self._add_team_member(team, user, tags=["reviewer"])
        member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(user.id), "tags": ["quality"]},
        )

        team_snapshot = IdentityPermissionService.team_snapshot(user, team)
        project_snapshot = IdentityPermissionService.project_snapshot(user, project)
        self.assertTrue(team_snapshot.has("team:INSIGHT"))
        self.assertTrue(project_snapshot.has("project:CHECK_TRA"))
        self.assertFalse(project_snapshot.has("team:INSIGHT"))
        self.assertEqual(["reviewer"], relation.tags)
        self.assertEqual(["quality"], member.tags)

        policy.project_tags["quality"]["permissions"] = ["project:ADD_LABEL"]
        policy.version += 1
        policy.save()
        refreshed = IdentityPermissionService.project_snapshot(user, project)
        self.assertFalse(refreshed.has("project:CHECK_TRA"))
        self.assertTrue(refreshed.has("project:ADD_LABEL"))

    def test_project_and_team_admins_can_assign_project_admin(self):
        project = self.create_project("permission-admin-assignment-boundary")
        team = project.team
        creator = self.get_creator(team)

        project_admin = self.create_user("permission-project-admin")
        self._add_team_member(team, project_admin)
        ProjectMemberService.add(
            project, creator, {"user_id": str(project_admin.id), "tags": ["admin"]}
        )

        team_admin = self.create_user("permission-team-admin")
        self._add_team_member(team, team_admin, base_tag="admin")

        regular_member = self.create_user("permission-regular-member")
        self._add_team_member(team, regular_member, base_tag="member")

        target = self.create_user("permission-admin-target")
        self._add_team_member(team, target)

        self.assertIn(
            "admin",
            IdentityPermissionService.assignable_project_tags(
                project_admin, project, target
            ),
        )
        self.assertIn(
            "admin",
            IdentityPermissionService.assignable_project_tags(
                team_admin, project, target
            ),
        )
        self.assertEqual(
            ["admin"],
            IdentityPermissionService.validate_project_tags(
                project_admin, project, target, ["admin"]
            ),
        )
        self.assertEqual(
            ["admin"],
            IdentityPermissionService.validate_project_tags(
                team_admin, project, target, ["admin"]
            ),
        )
        self.assertIn(
            "admin",
            IdentityPermissionService.assignable_project_tags(
                creator, project, target
            ),
        )

        self.assertNotIn(
            "admin",
            IdentityPermissionService.assignable_project_tags(
                regular_member, project, target
            ),
        )
        with self.assertRaises(InvalidIdentityTagError):
            IdentityPermissionService.validate_project_tags(
                regular_member, project, target, ["admin"]
            )

    def test_removed_project_member_keeps_no_access_even_with_team_admin(self):
        project = self.create_project("permission-removed-member-admin")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("permission-removed-member-admin-user")
        self._add_team_member(team, user, base_tag="admin")
        member = ProjectMemberService.add(
            project, creator, {"user_id": str(user.id), "tags": []}
        )
        before = IdentityPermissionService.project_snapshot(user, project)
        self.assertTrue(before.has("project:ACCESS"))
        self.assertTrue(before.has("project:MANAGE_MEMBERS"))

        ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(member.id),
                "expected_member_version": member.version,
                "changes": {"status": "removed"},
            },
        )

        # Soft-removing the project member revokes access entirely: the still
        # active team-admin relation must not restore project access through
        # team inheritance (doc 2.1).
        after = IdentityPermissionService.project_snapshot(user, project)
        self.assertFalse(after.has("project:ACCESS"))
        self.assertFalse(after.has("project:MANAGE_MEMBERS"))

    def test_removed_team_member_loses_team_inheritance_but_keeps_membership(self):
        project = self.create_project("permission-removed-team")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("permission-removed-team-user")
        relation = self._add_team_member(team, user, base_tag="admin")
        ProjectMemberService.add(
            project, creator, {"user_id": str(user.id), "tags": []}
        )
        before = IdentityPermissionService.project_snapshot(user, project)
        self.assertTrue(before.has("project:MANAGE_MEMBERS"))

        TeamMemberService.remove(team, relation.id, creator)

        # Removing the team relation prunes the admin inheritance; the direct
        # project membership (and its own tags) stay untouched.
        after = IdentityPermissionService.project_snapshot(user, project)
        self.assertTrue(after.has("project:ACCESS"))
        self.assertFalse(after.has("project:MANAGE_MEMBERS"))
