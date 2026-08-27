import datetime
from unittest.mock import patch

from app.exceptions import (
    ClearOperationRetryableError,
    CreatorCanNotLeaveError,
    IdentityPolicyInUseError,
    IdentityOperationInProgressError,
    IdentityVersionConflictError,
    MemberMergeRequiredError,
    MemberAlreadyOwnerError,
    MemberCapacityReachedError,
    NoPermissionError,
    OwnerConflictError,
    ProtectedIdentityTagError,
    ProjectMemberVersionConflictError,
    ProjectStateConflictError,
    IdentityMemberNotFoundError,
    IdentityTeamMemberNotFoundError,
    IdentityUserNotFoundError,
    InvalidIdentityRequestError,
)
from app.models.audit import IdentityAuditEvent
from app.models.identity_operation import IdentityOperation
from app.models.invitation import Invitation, InvitationStatus
from app.models.output import Output
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team_member import TeamMember
from app.constants.output import OutputTypes
from mongoengine.connection import get_db
from mongoengine.errors import SaveConditionError
from app.services.identity_permission import IdentityPermissionService
from app.services.project_invitation import ProjectInvitationAdapter
from app.services.project_lifecycle import ProjectLifecycleService
from app.services.project_member import ProjectMemberService
from app.services.project_member import IDENTITY_OPERATION_LEASE_SECONDS
from app.services.team_member import IdentityTagPolicyService, TeamMemberService
from tests import MoeTestCase


class IdentityServiceTestCase(MoeTestCase):
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

    def test_identity_member_runtime_indexes_reuse_legacy_generated_names(self):
        project = self.create_project("identity-index-compatibility")
        team = project.team
        db = get_db()

        for collection_name, indexes in (
            ("project_member", ProjectMember.INDEX_DEFINITIONS),
            ("team_member", TeamMember.INDEX_DEFINITIONS),
        ):
            collection = db[collection_name]
            for index_name in list(collection.index_information()):
                if index_name != "_id_":
                    collection.drop_index(index_name)
            for _, keys, options, fallback in indexes:
                effective_keys = fallback[0] if fallback else keys
                effective_options = fallback[1] if fallback else options
                collection.create_index(
                    effective_keys,
                    unique=effective_options.get("unique", False),
                    sparse=effective_options.get("sparse", False),
                )

        user = self.create_user("identity-index-compatibility-user")
        ProjectMember(
            project=project,
            user=user,
            display_name=user.name,
            tags=[],
        ).save()
        TeamMember(team=team, user=user).save()

        self.assertGreaterEqual(
            ProjectMember.objects(project=project, user=user).count(), 1
        )
        self.assertEqual(1, TeamMember.objects(team=team, user=user).count())

    def test_project_member_add_update_remove_and_version_conflict(self):
        project = self.create_project("member-service-lifecycle")
        creator = self.get_creator(project.team)
        user = self.create_user("member-service-user")
        self._add_team_member(project.team, user, qualifications=["translator"])

        member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(user.id), "tags": ["translator"]},
            request_id="add-request",
        )
        self.assertEqual("active", member.status)
        self.assertEqual(0, member.version)
        self.assertEqual(2, project.reload().user_count)
        self.assertEqual(1, IdentityAuditEvent.objects(action="project_member_add").count())

        updated = ProjectMemberService.update(
            project,
            user,
            {
                "member_id": str(member.id),
                "expected_member_version": member.version,
                "changes": {"display_name": "项目署名"},
            },
        )
        self.assertEqual("项目署名", updated.display_name)
        self.assertEqual(1, updated.version)

        with self.assertRaises(ProjectMemberVersionConflictError):
            ProjectMemberService.update(
                project,
                user,
                {
                    "member_id": str(member.id),
                    "expected_member_version": member.version,
                    "changes": {"tags": []},
                },
            )
        member.reload()
        self.assertEqual(["translator"], member.tags)
        self.assertEqual(1, member.version)

        removed = ProjectMemberService.update(
            project,
            user,
            {
                "member_id": str(member.id),
                "expected_member_version": member.version,
                "changes": {"status": "removed"},
            },
        )
        self.assertEqual("removed", removed.status)
        self.assertIsNotNone(removed.removed_time)
        # A removed project member keeps no access at all — the team
        # inheritance is pruned too.  Only a restore can bring the member
        # back.  (See test_removed_project_member_keeps_no_access_even_with_team_admin.)
        self.assertFalse(IdentityPermissionService.can_project(user, project, "project:ACCESS"))
        self.assertFalse(IdentityPermissionService.can_project(user, project, "project:ADD_FILE"))
        self.assertEqual(1, project.reload().user_count)

        restored = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(user.id), "tags": []},
        )
        self.assertEqual(member.id, restored.id)
        self.assertEqual("active", restored.status)
        self.assertEqual(3, restored.version)

        external = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "可恢复外部署名", "tags": ["translator"]},
        )
        removed_external = ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(external.id),
                "expected_member_version": external.version,
                "changes": {"status": "removed"},
            },
        )
        restored_external = ProjectMemberService.add(
            project,
            creator,
            {
                "member_id": str(external.id),
                "expected_member_version": removed_external.version,
                "display_name": "恢复后的外部署名",
                "tags": ["proofreader"],
            },
        )
        self.assertEqual(external.id, restored_external.id)
        self.assertEqual("恢复后的外部署名", restored_external.display_name)
        self.assertEqual(["proofreader"], restored_external.tags)
        self.assertEqual("active", restored_external.status)
        self.assertEqual(2, restored_external.version)

        removed_external = ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(external.id),
                "expected_member_version": restored_external.version,
                "changes": {"status": "removed"},
            },
        )
        with self.assertRaises(ProjectMemberVersionConflictError):
            ProjectMemberService.add(
                project,
                creator,
                {
                    "member_id": str(external.id),
                    "expected_member_version": removed_external.version - 1,
                    "tags": [],
                },
            )

    def test_external_members_are_distinct_and_only_managers_can_assign_tags(self):
        project = self.create_project("external-member-service")
        team = project.team
        creator = self.get_creator(team)
        operator = self.create_user("external-member-operator")
        self._add_team_member(team, operator)

        first = ProjectMemberService.add(
            project, operator, {"display_name": "同名外部署名"}
        )
        second = ProjectMemberService.add(
            project, operator, {"display_name": "同名外部署名"}
        )
        self.assertNotEqual(first.external_id, second.external_id)
        self.assertEqual(3, ProjectMember.objects(project=project, status="active").count())

        with self.assertRaises(NoPermissionError):
            ProjectMemberService.add(
                project,
                operator,
                {"display_name": "普通外部署名", "tags": ["translator"]},
            )

        managed = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "有标签外部署名", "tags": ["translator"]},
        )
        self.assertEqual(["translator"], managed.tags)
        self.assertIsNone(IdentityPermissionService.project_member(project, managed.user))

    def test_registration_add_uses_invitation_projection_and_preserves_lifecycle(self):
        project = self.create_project("invitation-projection-service")
        team = project.team
        operator = self.create_user("invitation-operator")
        self._add_team_member(team, operator)
        target = self.create_user("invitation-target")

        pending = ProjectMemberService.add(
            project,
            operator,
            {"user_id": str(target.id), "tags": []},
            request_id="invite-request",
        )
        self.assertEqual("invited", pending.status)
        invitation = Invitation.objects(
            group=project, user=target, status=InvitationStatus.PENDING
        ).first()
        self.assertIsNotNone(invitation)
        self.assertEqual(1, ProjectMember.objects(project=project, user=target).count())

        invitation.allow()
        pending.reload()
        self.assertEqual("active", pending.status)
        self.assertEqual(1, ProjectMember.objects(project=project, user=target).count())

        # A stale cancellation callback must never downgrade an already active
        # member created by the legacy allow path.
        ProjectInvitationAdapter.project_removed(invitation)
        pending.reload()
        self.assertEqual("active", pending.status)

        denied_target = self.create_user("invitation-denied-target")
        denied = ProjectMemberService.add(
            project,
            operator,
            {"user_id": str(denied_target.id), "tags": []},
        )
        denied_invitation = Invitation.objects(
            group=project,
            user=denied_target,
            status=InvitationStatus.PENDING,
        ).first()
        denied_invitation.deny()
        denied.reload()
        self.assertEqual("removed", denied.status)
        self.assertFalse(
            IdentityPermissionService.can_project(
                denied_target, project, "project:ACCESS"
            )
        )

    def test_pending_invitation_is_reused_without_creating_a_duplicate(self):
        project = self.create_project("invitation-reuse-service")
        team = project.team
        operator = self.create_user("invitation-reuse-operator")
        self._add_team_member(team, operator)
        target = self.create_user("invitation-reuse-target")
        invitation = Invitation(
            user=target,
            operator=operator,
            group=project,
            role=project.role_cls.by_system_code("translator"),
            status=InvitationStatus.PENDING,
        ).save()

        with patch.object(
            operator,
            "invite",
            side_effect=AssertionError("pending invitations must be reused"),
        ):
            member = ProjectInvitationAdapter.create_or_reuse(
                project,
                operator,
                target,
                tags=[],
                display_name="待处理邀请成员",
            )

        self.assertEqual("invited", member.status)
        self.assertEqual(1, Invitation.objects(id=invitation.id).count())
        self.assertEqual(
            1, Invitation.objects(user=target, group=project, status=InvitationStatus.PENDING).count()
        )
        self.assertEqual(1, ProjectMember.objects(project=project, user=target).count())


    def test_apply_changes_is_ordered_partially_successful_and_idempotent(self):
        project = self.create_project("member-operations-service")
        creator = self.get_creator(project.team)
        first_user = self.create_user("member-operation-first")
        second_user = self.create_user("member-operation-second")
        self._add_team_member(project.team, first_user, qualifications=["translator"])
        self._add_team_member(project.team, second_user)

        payload = {
            "operations": [
                {
                    "operation_id": "op-add-first",
                    "action": "add",
                    "user_id": str(first_user.id),
                    "tags": ["translator"],
                },
                {
                    "operation_id": "op-invalid",
                    "action": "unsupported",
                },
                {
                    "operation_id": "op-never-run",
                    "action": "add",
                    "display_name": "不应创建",
                },
            ]
        }
        result = ProjectMemberService.apply_changes(project, creator, payload)
        self.assertEqual(["op-add-first"], [item["operation_id"] for item in result["results"]])
        self.assertTrue(result["stopped"])
        self.assertEqual("INVALID_REQUEST", result["failed"]["code"])
        self.assertIsNotNone(
            IdentityOperation.objects(project=project, operation_id="op-add-first").first()
        )
        self.assertIsNone(
            IdentityOperation.objects(project=project, operation_id="op-never-run").first()
        )

        replay = ProjectMemberService.apply_changes(
            project,
            creator,
            {"operations": [payload["operations"][0]]},
        )
        self.assertEqual(result["results"], replay["results"])
        self.assertEqual(1, ProjectMember.objects(project=project, user=first_user).count())
        self.assertEqual(1, IdentityAuditEvent.objects(action="project_member_add").count())

    def test_failed_operation_can_be_corrected_with_the_same_operation_id(self):
        project = self.create_project("member-operation-retry-service")
        creator = self.get_creator(project.team)
        operation_id = "op-correct-after-failure"

        failed = ProjectMemberService.apply_changes(
            project,
            creator,
            {
                "operations": [
                    {"operation_id": operation_id, "action": "unsupported"}
                ]
            },
        )
        self.assertEqual(operation_id, failed["failed"]["operation_id"])
        self.assertEqual("failed", IdentityOperation.objects(
            project=project, operation_id=operation_id
        ).first().status)

        retried = ProjectMemberService.apply_changes(
            project,
            creator,
            {
                "operations": [
                    {
                        "operation_id": operation_id,
                        "action": "add",
                        "display_name": "修正后的外部署名",
                    }
                ]
            },
        )

        self.assertFalse(retried["failed"])
        self.assertEqual("succeeded", retried["results"][0]["status"])
        self.assertEqual(
            "succeeded",
            IdentityOperation.objects(
                project=project, operation_id=operation_id
            ).first().status,
        )
        self.assertEqual(
            1,
            ProjectMember.objects(
                project=project, display_name="修正后的外部署名", status="active"
            ).count(),
        )
        self.assertEqual(1, IdentityAuditEvent.objects(action="project_external_member_add").count())

    def test_external_bind_requires_team_member_and_preserves_display_name(self):
        project = self.create_project("external-bind-service")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("external-bind-target")
        member = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "待绑定署名", "tags": ["translator"]},
        )

        with self.assertRaises(NoPermissionError):
            ProjectMemberService.bind(
                project,
                member.id,
                creator,
                {"expected_version": member.version, "user_id": str(target.id)},
            )
        self._add_team_member(team, target, qualifications=["translator"])
        bound, event = ProjectMemberService.bind(
            project,
            member.id,
            creator,
            {"expected_version": member.version, "user_id": str(target.id)},
        )
        self.assertEqual(target, bound.user)
        self.assertIsNone(bound.external_id)
        self.assertEqual("待绑定署名", bound.display_name)
        self.assertEqual("project_external_member_bind", event.action)
        self.assertTrue(IdentityPermissionService.can_project(target, project, "project:ADD_FILE"))

        other = self.create_user("external-bind-other")
        self._add_team_member(team, other, qualifications=["translator"])
        duplicate = ProjectMemberService.add(project, creator, {"display_name": "另一个外部"})
        ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(duplicate.id),
                "expected_member_version": duplicate.version,
                "changes": {"tags": ["translator"]},
            },
        )
        # The first registered target already has a member, so binding it again
        # must enter the explicit merge path.
        with self.assertRaises(MemberMergeRequiredError):
            ProjectMemberService.bind(
                project,
                duplicate.id,
                creator,
                {"expected_version": duplicate.version + 1, "user_id": str(target.id)},
            )

    def test_external_bind_save_race_is_reported_as_member_version_conflict(self):
        project = self.create_project("external-bind-version-race")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("external-bind-version-target")
        self._add_team_member(team, target)
        member = ProjectMemberService.add(
            project, creator, {"display_name": "竞态外部署名"}
        )

        with patch.object(ProjectMember, "save", side_effect=SaveConditionError):
            with self.assertRaises(ProjectMemberVersionConflictError):
                ProjectMemberService.bind(
                    project,
                    member.id,
                    creator,
                    {"expected_version": member.version, "user_id": str(target.id)},
                )

    def test_external_member_merge_requires_both_versions_and_preserves_one_identity(self):
        project = self.create_project("external-merge-service")
        team = project.team
        creator = self.get_creator(team)
        target = self.create_user("external-merge-target")
        self._add_team_member(
            team, target, qualifications=["translator", "proofreader"]
        )
        target_member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(target.id), "display_name": "已有展示名", "tags": ["translator"]},
        )
        source = ProjectMemberService.add(
            project,
            creator,
            {"display_name": "外部署名", "tags": ["proofreader"]},
        )

        merged, removed, event = ProjectMemberService.merge(
            project,
            source.id,
            creator,
            {
                "target_member_id": str(target_member.id),
                "expected_source_version": source.version,
                "expected_target_version": target_member.version,
                "display_name": "保留外部署名",
                "tags": ["translator", "proofreader"],
            },
        )

        self.assertEqual(target, merged.user)
        self.assertEqual("保留外部署名", merged.display_name)
        self.assertEqual(["proofreader", "translator"], merged.tags)
        self.assertEqual("removed", removed.status)
        self.assertEqual(2, ProjectMember.objects(project=project, status="active").count())
        self.assertEqual("project_external_member_merge", event.action)

        with self.assertRaises(ProjectMemberVersionConflictError):
            ProjectMemberService.merge(
                project,
                source.id,
                creator,
                {
                    "target_member_id": str(target_member.id),
                    "expected_source_version": 0,
                    "expected_target_version": target_member.version,
                    "display_name": "不应覆盖",
                    "tags": ["translator"],
                },
            )

    def test_project_capacity_counts_registered_and_external_members_together(self):
        project = self.create_project("project-member-capacity")
        project.max_user = 2
        project.save()
        creator = self.get_creator(project.team)
        ProjectMemberService.add(project, creator, {"display_name": "外部成员"})
        project.reload()
        self.assertEqual(2, project.user_count)

        target = self.create_user("project-capacity-target")
        self._add_team_member(project.team, target)
        with self.assertRaises(MemberCapacityReachedError):
            ProjectMemberService.add(
                project, creator, {"user_id": str(target.id)}
            )

    def test_in_progress_operation_cannot_execute_side_effect_twice(self):
        project = self.create_project("member-operation-in-progress")
        creator = self.get_creator(project.team)
        IdentityOperation(
            project=project, operation_id="op-in-progress", status="processing"
        ).save()

        with self.assertRaises(IdentityOperationInProgressError):
            ProjectMemberService.apply_changes(
                project,
                creator,
                {
                    "operations": [
                        {
                            "operation_id": "op-in-progress",
                            "action": "add",
                            "display_name": "不应重复创建",
                        }
                    ]
                },
            )
        self.assertEqual(
            0,
            ProjectMember.objects(
                project=project, display_name="不应重复创建"
            ).count(),
        )

    def test_fresh_explicit_operation_lease_cannot_be_reclaimed(self):
        project = self.create_project("member-operation-lease-fresh")
        creator = self.get_creator(project.team)
        now = datetime.datetime.utcnow()
        IdentityOperation(
            project=project,
            operation_id="op-lease-fresh",
            status="processing",
            edit_time=now - datetime.timedelta(
                seconds=IDENTITY_OPERATION_LEASE_SECONDS + 1
            ),
            lease_expires_at=now + datetime.timedelta(seconds=30),
            claim_token="live-worker",
        ).save()

        with self.assertRaises(IdentityOperationInProgressError):
            ProjectMemberService.apply_changes(
                project,
                creator,
                {
                    "operations": [
                        {
                            "operation_id": "op-lease-fresh",
                            "action": "add",
                            "display_name": "租约仍有效",
                        }
                    ]
                },
            )
        self.assertEqual(
            0,
            ProjectMember.objects(project=project, display_name="租约仍有效").count(),
        )

    def test_stale_processing_operation_is_reclaimed(self):
        project = self.create_project("member-operation-stale")
        creator = self.get_creator(project.team)
        stale_time = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=IDENTITY_OPERATION_LEASE_SECONDS + 1
        )
        IdentityOperation(
            project=project,
            operation_id="op-stale",
            status="processing",
            edit_time=stale_time,
            create_time=stale_time,
        ).save()

        result = ProjectMemberService.apply_changes(
            project,
            creator,
            {
                "operations": [
                    {
                        "operation_id": "op-stale",
                        "action": "add",
                        "display_name": "过期操作外部署名",
                    }
                ]
            },
        )

        self.assertIsNone(result["failed"])
        operation = IdentityOperation.objects(
            project=project, operation_id="op-stale"
        ).first()
        self.assertEqual("succeeded", operation.status)
        self.assertIsNone(operation.claim_token)
        self.assertIsNone(operation.lease_expires_at)
        self.assertEqual(
            1,
            ProjectMember.objects(
                project=project, display_name="过期操作外部署名"
            ).count(),
        )

    def test_reclaimed_external_operation_reuses_side_effect_without_duplicate_audit(self):
        project = self.create_project("member-operation-recovery")
        creator = self.get_creator(project.team)
        operation_id = "op-external-recovery"
        payload = {
            "operations": [
                {
                    "operation_id": operation_id,
                    "action": "add",
                    "display_name": "崩溃后恢复的外部署名",
                }
            ]
        }

        first = ProjectMemberService.apply_changes(project, creator, payload)
        member = ProjectMember.objects(
            project=project, display_name="崩溃后恢复的外部署名"
        ).first()
        operation = IdentityOperation.objects(
            project=project, operation_id=operation_id
        ).first()
        operation.status = "processing"
        operation.result = {}
        operation.error_code = None
        operation.edit_time = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=IDENTITY_OPERATION_LEASE_SECONDS + 1
        )
        operation.lease_expires_at = None
        operation.claim_token = "crashed-worker"
        operation.save()

        recovered = ProjectMemberService.apply_changes(project, creator, payload)

        self.assertEqual(first["results"], recovered["results"])
        self.assertIsNone(recovered["failed"])
        self.assertEqual(
            1,
            ProjectMember.objects(
                project=project, external_id=member.external_id
            ).count(),
        )
        self.assertEqual(
            1,
            IdentityAuditEvent.objects(
                action="project_external_member_add"
            ).count(),
        )

    def test_reclaimed_invitation_projection_does_not_bump_version_or_audit(self):
        project = self.create_project("invitation-operation-recovery")
        team = project.team
        operator = self.create_user("invitation-operation-recovery-operator")
        self._add_team_member(team, operator)
        target = self.create_user("invitation-operation-recovery-target")
        operation_id = "op-invitation-recovery"
        payload = {
            "operations": [
                {
                    "operation_id": operation_id,
                    "action": "add",
                    "user_id": str(target.id),
                    "tags": [],
                }
            ]
        }

        first = ProjectMemberService.apply_changes(project, operator, payload)
        member = ProjectMember.objects(project=project, user=target).first()
        version = member.version
        operation = IdentityOperation.objects(
            project=project, operation_id=operation_id
        ).first()
        operation.status = "processing"
        operation.result = {}
        operation.error_code = None
        operation.edit_time = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=IDENTITY_OPERATION_LEASE_SECONDS + 1
        )
        operation.lease_expires_at = None
        operation.claim_token = "crashed-invitation-worker"
        operation.save()

        recovered = ProjectMemberService.apply_changes(project, operator, payload)

        self.assertEqual(first["results"], recovered["results"])
        member.reload()
        self.assertEqual(version, member.version)
        self.assertEqual(
            1,
            IdentityAuditEvent.objects(
                action="project_invitation_projection_invited"
            ).count(),
        )

    def test_team_member_permissions_versions_aliases_and_owner_protection(self):
        project = self.create_project("team-member-service")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("team-member-service-user")
        relation = self._add_team_member(team, user)

        with self.assertRaises(NoPermissionError):
            TeamMemberService.update(
                team,
                relation.id,
                user,
                {"expected_version": relation.version, "worker_qualifications": ["translator"]},
            )

        aliases = TeamMemberService.update_aliases(
            team,
            relation.id,
            user,
            [" 团队署名 ", "团队署名"],
            expected_version=relation.version,
        )
        self.assertEqual(["团队署名"], aliases.aliases)
        self.assertEqual([], user.reload().aliases)
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update(
                team,
                relation.id,
                user,
                {"expected_version": aliases.version, "tags": ["admin"]},
            )
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update_aliases(
                team,
                relation.id,
                self.create_user("team-member-outsider"),
                [],
                expected_version=aliases.version,
            )

        creator_update = TeamMemberService.update(
            team,
            relation.id,
            creator,
            {
                "expected_version": aliases.version,
                "worker_qualifications": ["translator"],
            },
        )
        self.assertEqual(["translator"], creator_update.worker_qualifications)

        IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": 0,
                "upserts": [
                    {
                        "scope": "team",
                        "code": "self_reviewer",
                        "name": "Self reviewer",
                        "permissions": ["team:ACCESS"],
                    }
                ],
            },
        )
        creator_relation = TeamMemberService.for_user(team, creator)
        creator_self_update = TeamMemberService.update(
            team,
            creator_relation.id,
            creator,
            {
                "expected_version": creator_relation.version,
                "tags": ["self_reviewer"],
                "worker_qualifications": ["translator", "proofreader"],
            },
        )
        self.assertEqual(["self_reviewer"], creator_self_update.tags)
        self.assertEqual(["proofreader", "translator"], creator_self_update.worker_qualifications)

        with self.assertRaises(ProtectedIdentityTagError):
            TeamMemberService.update(
                team,
                relation.id,
                creator,
                {"expected_version": creator_update.version, "base_tag": "creator"},
            )
        with self.assertRaises(ProtectedIdentityTagError):
            TeamMemberService.remove(team, TeamMemberService.for_user(team, creator).id, creator)

    def test_legacy_join_projection_upgrades_existing_team_member_admin(self):
        team = self.create_team("legacy-team-admin-projection")
        user = self.create_user("legacy-team-admin-user")
        member = self._add_team_member(team, user)

        user.join(team, role=team.role_cls.by_system_code("admin"))

        member.reload()
        self.assertEqual("admin", member.base_tag)
        self.assertEqual("active", member.status)

    def test_project_owner_join_repairs_missing_or_downgraded_identity_projection(self):
        project = self.create_project("owner-join-projection-repair")
        owner = self.get_creator(project.team)
        owner_member = ProjectMemberService.for_user(project, owner)
        self.assertEqual(["creator"], owner_member.tags)

        # A project can have an owner field without a corresponding identity
        # member after an interrupted migration.  Rejoining must rebuild it as
        # creator even when the compatibility caller supplies admin.
        owner_member.delete()
        owner.join(project, role=project.role_cls.by_system_code("admin"))
        repaired = ProjectMemberService.for_user(project, owner)
        self.assertIsNotNone(repaired)
        self.assertEqual(["creator"], repaired.tags)
        self.assertEqual("creator", owner.get_role(project).system_code)

        # Also repair an already-active projection that was written by an old
        # role-based path with the wrong tag.
        repaired.tags = ["admin"]
        repaired.save()
        owner.join(project, role=project.role_cls.by_system_code("admin"))
        repaired.reload()
        self.assertEqual(["creator"], repaired.tags)

        updated = ProjectMemberService.update(
            project,
            owner,
            {
                "member_id": str(repaired.id),
                "expected_member_version": repaired.version,
                "changes": {"tags": ["creator"]},
            },
        )
        self.assertEqual(["creator"], updated.tags)

    def test_legacy_role_changes_update_identity_projection(self):
        project = self.create_project("legacy-role-change-projection")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("legacy-role-change-user")
        user.join(team, role=team.role_cls.by_system_code("member"))
        user.join(project, role=project.role_cls.by_system_code("translator"))

        team.change_user_role(
            user,
            team.role_cls.by_system_code("admin"),
            operator=creator,
        )
        team_member = TeamMemberService.for_user(team, user)
        self.assertEqual("admin", team_member.base_tag)

        team.change_user_role(
            user,
            team.role_cls.by_system_code("member"),
            operator=creator,
        )
        team_member.reload()
        self.assertEqual("member", team_member.base_tag)

        project.change_user_role(
            user,
            project.role_cls.by_system_code("proofreader"),
            operator=creator,
        )
        project_member = ProjectMemberService.for_user(project, user)
        self.assertEqual(["proofreader"], project_member.tags)

    def test_personal_projects_use_active_identity_members_only(self):
        project = self.create_project("personal-project-identity-filter")
        creator = self.get_creator(project.team)
        user = self.create_user("personal-project-filter-user")
        user.join(project, role=project.role_cls.by_system_code("translator"))

        self.assertEqual([project.id], [item.id for item in user.projects()])
        member = ProjectMemberService.for_user(project, user)
        ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(member.id),
                "expected_member_version": member.version,
                "changes": {"status": "removed"},
            },
        )
        self.assertEqual([], [item.id for item in user.projects()])

        restored = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(user.id), "tags": []},
        )
        self.assertEqual("active", restored.status)
        self.assertEqual([project.id], [item.id for item in user.projects()])

    def test_creator_cannot_leave_team_through_legacy_model_path(self):
        team = self.create_team("legacy-team-creator-leave")
        creator = self.get_creator(team)

        with self.assertRaises(CreatorCanNotLeaveError):
            creator.leave(team)

        member = TeamMemberService.for_user(team, creator)
        team.reload()
        self.assertEqual("creator", member.base_tag)
        self.assertEqual("active", member.status)
        self.assertEqual(1, team.user_count)

    def test_legacy_owner_leave_keeps_owner_active_and_creator(self):
        project = self.create_project("legacy-owner-leave")
        owner = self.get_creator(project.team)
        legacy_relation = owner.get_relation(project)
        member = ProjectMemberService.for_user(project, owner)

        with self.assertRaises(MemberAlreadyOwnerError):
            owner.leave(project)

        legacy_relation.reload()
        member.reload()
        project.reload()
        self.assertEqual(owner, project.owner_user)
        self.assertEqual("active", member.status)
        self.assertIn("creator", member.tags)
        self.assertEqual(1, project.user_count)

    def test_policy_update_overrides_system_tags_and_protects_in_use_custom_tags(self):
        project = self.create_project("policy-service")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("policy-service-user")
        relation = self._add_team_member(team, user)

        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": 0,
                "upserts": [
                    {
                        "scope": "team",
                        "code": "reviewer",
                        "permissions": ["team:INSIGHT"],
                    },
                    {
                        "scope": "project",
                        "code": "quality",
                        "permissions": ["project:CHECK_TRA"],
                    },
                ],
            },
        )
        self.assertEqual(1, policy.version)
        self.assertEqual("team", IdentityTagPolicyService.response(team)["team_tags"]["reviewer"]["source"])

        with self.assertRaises(ProtectedIdentityTagError):
            IdentityTagPolicyService.update(
                team,
                creator,
                {
                    "expected_version": policy.version,
                    "upserts": [
                        {
                            "scope": "project",
                            "code": "unsafe",
                            "permissions": ["project:COMPLETE_PROJECT"],
                        }
                    ],
                },
            )

        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": policy.version,
                "upserts": [
                    {
                        "scope": "project",
                        "code": "admin",
                        "permissions": ["project:ACCESS"],
                    }
                ],
            },
        )
        overridden = IdentityTagPolicyService.response(team)["project_tags"]["admin"]
        self.assertEqual("team_override", overridden["source"])
        self.assertEqual(["project:ACCESS"], overridden["permissions"])

        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": policy.version,
                "removes": [{"scope": "project", "code": "admin"}],
            },
        )
        self.assertEqual("site", IdentityTagPolicyService.response(team)["project_tags"]["admin"]["source"])

        team_admin = self.create_user("policy-team-admin")
        self._add_team_member(team, team_admin, base_tag="admin")
        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": policy.version,
                "upserts": [
                    {
                        "scope": "team",
                        "code": "admin",
                        "permissions": ["team:ACCESS", "team:INSIGHT"],
                    }
                ],
            },
        )
        overridden_team = IdentityTagPolicyService.response(team)["team_tags"]["admin"]
        self.assertEqual("team_override", overridden_team["source"])
        self.assertEqual(["team:ACCESS", "team:INSIGHT"], overridden_team["permissions"])
        self.assertEqual(
            {"team:ACCESS", "team:INSIGHT"},
            IdentityPermissionService.team_snapshot(team_admin, team).effective_permissions,
        )

        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": policy.version,
                "removes": [{"scope": "team", "code": "admin"}],
            },
        )
        self.assertEqual("site", IdentityTagPolicyService.response(team)["team_tags"]["admin"]["source"])

        TeamMemberService.update(
            team,
            relation.id,
            creator,
            {"expected_version": relation.version, "tags": ["reviewer"]},
        )
        with self.assertRaises(IdentityPolicyInUseError):
            IdentityTagPolicyService.update(
                team,
                creator,
                {
                    "expected_version": policy.version,
                    "removes": [{"scope": "team", "code": "reviewer"}],
                },
            )

        with self.assertRaises(IdentityVersionConflictError):
            IdentityTagPolicyService.update(
                team,
                creator,
                {"expected_version": 0, "upserts": []},
            )

    def test_project_lifecycle_has_clear_boundary_and_retryable_failure(self):
        project = self.create_project("lifecycle-service")
        team = project.team
        creator = self.get_creator(team)
        initial_version = project.status_version

        completed, changed = ProjectLifecycleService.complete(
            project,
            creator,
            {"expected_version": initial_version, "expected_status": "NORMAL"},
        )
        self.assertTrue(changed)
        self.assertEqual("COMPLETED", ProjectLifecycleService.status_name(completed))
        completed_version = completed.status_version
        completed_again, changed = ProjectLifecycleService.complete(
            completed,
            creator,
            {"expected_version": completed_version, "expected_status": "COMPLETED"},
        )
        self.assertFalse(changed)
        self.assertEqual(completed_version, completed_again.status_version)

        reopened, changed = ProjectLifecycleService.reopen(
            completed,
            creator,
            {"expected_version": completed_version, "expected_status": "COMPLETED"},
        )
        self.assertTrue(changed)
        self.assertEqual("NORMAL", ProjectLifecycleService.status_name(reopened))
        retry_version = reopened.status_version

        with patch.object(
            ProjectLifecycleService,
            "clear_contents",
            side_effect=RuntimeError("temporary cleanup failure"),
        ):
            with self.assertRaises(ClearOperationRetryableError):
                ProjectLifecycleService.clear(
                    reopened,
                    creator,
                    {"expected_version": retry_version, "expected_status": "NORMAL"},
                )
        reopened.reload()
        self.assertEqual("NORMAL", ProjectLifecycleService.status_name(reopened))
        self.assertEqual(retry_version, reopened.status_version)
        self.assertFalse(reopened.clear_in_progress)

        cleared, changed = ProjectLifecycleService.clear(
            reopened,
            creator,
            {"expected_version": retry_version, "expected_status": "NORMAL"},
        )
        self.assertTrue(changed)
        self.assertEqual("CLEARED", ProjectLifecycleService.status_name(cleared))
        with self.assertRaises(ProjectStateConflictError):
            ProjectLifecycleService.reopen(cleared, creator, {})

    def test_project_clear_strict_storage_failure_keeps_outputs_and_releases_lock(self):
        project = self.create_project("lifecycle-strict-storage")
        creator = self.get_creator(project.team)
        target = project.targets().first()
        Output.create(
            project=project,
            target=target,
            user=creator,
            type=OutputTypes.ALL,
        )

        with patch.object(
            Output,
            "delete_real_files_strict",
            side_effect=RuntimeError("storage unavailable"),
        ):
            with self.assertRaises(ClearOperationRetryableError):
                ProjectLifecycleService.clear(project, creator, {})

        project.reload()
        self.assertEqual("NORMAL", ProjectLifecycleService.status_name(project))
        self.assertFalse(project.clear_in_progress)
        self.assertEqual(1, Output.objects(project=project).count())

        cleared, changed = ProjectLifecycleService.clear(project, creator, {})
        self.assertTrue(changed)
        self.assertEqual("CLEARED", ProjectLifecycleService.status_name(cleared))
        self.assertEqual(0, Output.objects(project=project).count())

    def test_project_clear_lock_blocks_concurrent_lifecycle_transitions(self):
        project = self.create_project("lifecycle-clear-lock")
        creator = self.get_creator(project.team)

        def observe_lock(value):
            value.reload()
            self.assertTrue(value.clear_in_progress)
            with self.assertRaises(ProjectStateConflictError):
                ProjectLifecycleService.complete(value, creator, {})
            with self.assertRaises(ProjectStateConflictError):
                ProjectLifecycleService.clear(value, creator, {})

        with patch.object(
            ProjectLifecycleService, "clear_contents", side_effect=observe_lock
        ):
            cleared, changed = ProjectLifecycleService.clear(project, creator, {})
        self.assertTrue(changed)
        self.assertEqual("CLEARED", ProjectLifecycleService.status_name(cleared))

    def test_project_admin_can_complete_but_only_team_creator_can_clear(self):
        project = self.create_project("lifecycle-boundaries")
        team = project.team
        team_creator = self.get_creator(team)
        admin = self.create_user("lifecycle-admin")
        self._add_team_member(team, admin, base_tag="admin")
        ProjectMemberService.add(project, team_creator, {"user_id": str(admin.id), "tags": ["admin"]})

        completed, _ = ProjectLifecycleService.complete(project, admin, {})
        self.assertEqual("COMPLETED", ProjectLifecycleService.status_name(completed))
        with self.assertRaises(NoPermissionError):
            ProjectLifecycleService.clear(completed, admin, {})
        reopened, _ = ProjectLifecycleService.reopen(completed, admin, {})
        self.assertEqual("NORMAL", ProjectLifecycleService.status_name(reopened))

    def test_owner_transfer_updates_both_members_and_rolls_back_on_owner_conflict(self):
        project = self.create_project("owner-transfer-service")
        team = project.team
        old_owner = self.get_creator(team)
        new_owner = self.create_user("owner-transfer-new")
        self._add_team_member(team, new_owner, qualifications=["translator"])
        ProjectMemberService.add(
            project,
            old_owner,
            {"user_id": str(new_owner.id), "tags": ["translator"]},
        )
        old_member = ProjectMemberService.for_user(project, old_owner)

        transferred, promoted, changed = ProjectLifecycleService.transfer_owner(
            project,
            old_owner,
            {
                "new_owner_user_id": str(new_owner.id),
                "expected_owner_user_id": str(old_owner.id),
                "expected_version": project.owner_version,
                "keep_old_owner_admin": True,
            },
        )
        self.assertTrue(changed)
        self.assertEqual(new_owner, transferred.owner_user)
        self.assertEqual(new_owner, promoted.user)
        old_member.reload()
        promoted.reload()
        self.assertEqual(["admin"], old_member.tags)
        self.assertIn("creator", promoted.tags)
        self.assertTrue(IdentityPermissionService.project_snapshot(new_owner, project).is_owner)

        # Build a second project so the rollback assertion observes a real
        # two-member transfer without depending on the first transfer's state.
        rollback_project = self.create_project("owner-transfer-rollback")
        rollback_team = rollback_project.team
        rollback_old = self.get_creator(rollback_team)
        rollback_new = self.create_user("owner-transfer-rollback-new")
        self._add_team_member(rollback_team, rollback_new, qualifications=["translator"])
        rollback_new_member = ProjectMemberService.add(
            rollback_project,
            rollback_old,
            {"user_id": str(rollback_new.id), "tags": ["translator"]},
        )
        rollback_old_member = ProjectMemberService.for_user(rollback_project, rollback_old)
        old_tags = list(rollback_old_member.tags)
        new_tags = list(rollback_new_member.tags)
        old_version = rollback_old_member.version
        new_version = rollback_new_member.version
        Project.objects(id=rollback_project.id).update_one(inc__owner_version=1)
        with self.assertRaises(OwnerConflictError):
            ProjectLifecycleService.transfer_owner(
                rollback_project,
                rollback_old,
                {
                    "new_owner_user_id": str(rollback_new.id),
                    "expected_owner_user_id": str(rollback_old.id),
                    "expected_version": rollback_project.owner_version,
                },
            )
        rollback_old_member.reload()
        rollback_new_member.reload()
        self.assertEqual(old_tags, rollback_old_member.tags)
        self.assertEqual(new_tags, rollback_new_member.tags)
        self.assertEqual(old_version, rollback_old_member.version)
        self.assertEqual(new_version, rollback_new_member.version)
        rollback_project.reload()
        self.assertEqual(rollback_old, rollback_project.owner_user)

    def test_plain_team_member_cannot_manage_other_members_or_self_tags(self):
        team = self.create_team("team-escalation-guard")
        victim = self.create_user("team-escalation-victim")
        attacker = self.create_user("team-escalation-attacker")
        outsider = self.create_user("team-escalation-outsider")
        victim_member = self._add_team_member(team, victim)
        attacker_member = self._add_team_member(team, attacker)

        # Tags/qualifications of another member
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update(
                team,
                victim_member.id,
                attacker,
                {"expected_version": victim_member.version, "tags": ["admin"]},
            )
        # Aliases of another member
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update_aliases(
                team,
                victim_member.id,
                attacker,
                ["越权别名"],
                expected_version=victim_member.version,
            )
        # Remove another member
        with self.assertRaises(NoPermissionError):
            TeamMemberService.remove(team, victim_member.id, attacker)
        # A plain member may not even edit their own tags/qualifications
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update(
                team,
                attacker_member.id,
                attacker,
                {
                    "expected_version": attacker_member.version,
                    "worker_qualifications": ["translator"],
                },
            )
        # Outsiders (no team relation) are rejected as well
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update_aliases(
                team,
                victim_member.id,
                outsider,
                [],
                expected_version=victim_member.version,
            )

    def test_team_member_list_members_splits_comma_separated_statuses(self):
        """A comma-joined ``status`` value (the frontend one-request pattern,
        e.g. ``status=active,removed``) must be split into individual statuses
        instead of being matched as a single literal status.
        """
        team = self.create_team("team-comma-status")
        creator = self.get_creator(team)
        active_user = self.create_user("team-comma-active-user")
        removed_user = self.create_user("team-comma-removed-user")
        self._add_team_member(team, active_user, base_tag="member")
        removed = self._add_team_member(team, removed_user, base_tag="member")
        TeamMemberService.remove(team, removed.id, creator)

        members = TeamMemberService.list_members(
            team, creator, status="active,removed"
        )
        names = {member.user.name for member in members}
        # The team creator is themselves an active member, so assert presence
        # of the specific users rather than an exact set.
        self.assertIn(active_user.name, names)
        self.assertIn(removed_user.name, names)
        # Both requested statuses must actually be present in the result.
        self.assertEqual(
            {"active", "removed"},
            {member.status for member in members} & {"active", "removed"},
        )

        # A single status and the default still behave as before: only active
        # members are returned, and a removed member never leaks back in.
        active_names = {
            member.user.name
            for member in TeamMemberService.list_members(
                team, creator, status="active"
            )
        }
        self.assertIn(active_user.name, active_names)
        self.assertNotIn(removed_user.name, active_names)
        default_names = {
            member.user.name for member in TeamMemberService.list_members(team, creator)
        }
        self.assertIn(active_user.name, default_names)

    def test_team_member_search_uses_user_and_team_alias_projections(self):
        team = self.create_team("team-search-projections")
        creator = self.get_creator(team)
        user = self.create_user("team-search-user")
        member = self._add_team_member(team, user)
        TeamMemberService.update_aliases(
            team,
            member.id,
            user,
            ["Project Alias"],
            expected_version=member.version,
        )

        matches = TeamMemberService.list_members(team, creator, word="alias")
        self.assertEqual([user.id], [item.user.id for item in matches])

    def test_policy_base_tag_override_and_removal_are_creator_only(self):
        team = self.create_team("policy-base-tag-creator")
        creator = self.get_creator(team)
        admin = self.create_user("policy-base-tag-admin")
        self._add_team_member(team, admin, base_tag="admin")

        with self.assertRaises(NoPermissionError):
            IdentityTagPolicyService.update(
                team,
                admin,
                {
                    "expected_version": 0,
                    "upserts": [
                        {
                            "scope": "team",
                            "code": "member",
                            "permissions": ["team:ACCESS"],
                        }
                    ],
                },
            )
        with self.assertRaises(NoPermissionError):
            IdentityTagPolicyService.update(
                team,
                admin,
                {"expected_version": 0, "removes": [{"scope": "team", "code": "member"}]},
            )

        policy = IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": 0,
                "upserts": [
                    {"scope": "team", "code": "member", "permissions": ["team:ACCESS"]}
                ],
            },
        )
        response = IdentityTagPolicyService.response(team)
        self.assertEqual("team_override", response["team_tags"]["member"]["source"])
        self.assertFalse(response["team_tags"]["member"]["initial_assignable"])
        self.assertEqual(["team:ACCESS"], sorted(response["team_tags"]["member"]["permissions"]))

        IdentityTagPolicyService.update(
            team,
            creator,
            {
                "expected_version": policy.version,
                "removes": [{"scope": "team", "code": "member"}],
            },
        )
        after = IdentityTagPolicyService.response(team)
        self.assertEqual("site", after["team_tags"]["member"]["source"])

    def test_update_aliases_requires_expected_version(self):
        team = self.create_team("team-aliases-version")
        user = self.create_user("team-aliases-version-user")
        member = self._add_team_member(team, user)
        with self.assertRaises(InvalidIdentityRequestError):
            TeamMemberService.update_aliases(
                team, member.id, user, ["署名"], expected_version=None
            )

    def test_malformed_ids_raise_business_errors_not_500(self):
        team = self.create_team("team-malformed-ids")
        creator = self.get_creator(team)
        with self.assertRaises(IdentityTeamMemberNotFoundError):
            TeamMemberService.get(team, "not-an-objectid")
        with self.assertRaises(IdentityUserNotFoundError):
            TeamMemberService.add(
                team, creator, {"user_id": "not-an-objectid"}
            )

        project = self.create_project("pm-malformed-ids")
        # create_project creates its own team, so the project operator is the
        # new team's creator, not the earlier ``creator`` variable.
        project_creator = self.get_creator(project.team)
        with self.assertRaises(IdentityUserNotFoundError):
            ProjectMemberService.add(
                project, project_creator, {"user_id": "not-an-objectid"}
            )
        with self.assertRaises(IdentityMemberNotFoundError):
            ProjectMemberService.get(project, "not-an-objectid")
        with self.assertRaises(IdentityUserNotFoundError):
            ProjectLifecycleService.transfer_owner(
                project,
                project_creator,
                {"new_owner_user_id": "not-an-objectid"},
            )

    def test_merge_cannot_strip_owner_creator_tag(self):
        project = self.create_project("merge-owner-strip")
        team = project.team
        creator = self.get_creator(team)
        # Open qualification mode so the worker tag validates for the owner
        # target and the owner-invariant check is the one that fires.
        team.worker_qualification_mode = "open"
        team.save()
        owner_member = ProjectMemberService.for_user(project, creator)
        source = ProjectMemberService.add(
            project, creator, {"display_name": "外部成员", "tags": []}
        )
        with self.assertRaises(MemberAlreadyOwnerError):
            ProjectMemberService.merge(
                project,
                source.id,
                creator,
                {
                    "target_member_id": str(owner_member.id),
                    "expected_source_version": source.version,
                    "expected_target_version": owner_member.version,
                    "display_name": "不应覆盖",
                    "tags": ["translator"],
                },
            )

    def test_merge_target_must_be_an_active_team_member(self):
        project = self.create_project("merge-team-active")
        creator = self.get_creator(project.team)
        registered = self.create_user("merge-not-in-team")
        target_member = ProjectMemberService.add(
            project,
            creator,
            {"user_id": str(registered.id), "display_name": "未入队", "tags": []},
        )
        source = ProjectMemberService.add(
            project, creator, {"display_name": "外部成员", "tags": []}
        )
        with self.assertRaises(NoPermissionError):
            ProjectMemberService.merge(
                project,
                source.id,
                creator,
                {
                    "target_member_id": str(target_member.id),
                    "expected_source_version": source.version,
                    "expected_target_version": target_member.version,
                    "display_name": "合并",
                    "tags": [],
                },
            )

    def test_restoring_removed_members_respects_project_capacity(self):
        project = self.create_project("restore-capacity")
        project.max_user = 2  # owner + 1 active member
        project.save()
        creator = self.get_creator(project.team)
        external_a = ProjectMemberService.add(
            project, creator, {"display_name": "外部甲", "tags": []}
        )
        # Remove A so a second member can be added
        ProjectMemberService.update(
            project,
            creator,
            {
                "member_id": str(external_a.id),
                "expected_member_version": external_a.version,
                "changes": {"status": "removed"},
            },
        )
        ProjectMemberService.add(
            project, creator, {"display_name": "外部乙", "tags": []}
        )
        project.reload()
        self.assertEqual(2, project.user_count)
        # Restoring A would exceed max_user: the restore path must enforce it
        # just like a fresh add does.  Restore is a CAS write like any other
        # change, so the caller must supply the member's current version.
        external_a.reload()
        with self.assertRaises(MemberCapacityReachedError):
            ProjectMemberService.add(
                project,
                creator,
                {
                    "member_id": str(external_a.id),
                    "expected_member_version": external_a.version,
                    "display_name": "外部甲",
                    "tags": [],
                },
            )

    def test_restoring_removed_team_members_respects_team_capacity(self):
        team = self.create_team("restore-team-capacity")
        team.max_user = 2  # creator + one active member
        team.save()
        creator = self.get_creator(team)
        first_user = self.create_user("restore-team-capacity-first")
        second_user = self.create_user("restore-team-capacity-second")
        first = self._add_team_member(team, first_user)
        TeamMemberService.remove(team, first.id, creator)
        self._add_team_member(team, second_user)
        first.reload()
        with self.assertRaises(MemberCapacityReachedError):
            TeamMemberService.add(
                team,
                creator,
                {"user_id": str(first_user.id)},
            )

    def test_legacy_join_team_path_respects_team_capacity(self):
        team = self.create_team("legacy-team-capacity")
        team.max_user = 2
        team.save()
        first = self.create_user("legacy-team-capacity-first")
        second = self.create_user("legacy-team-capacity-second")
        first.join(team)
        with self.assertRaises(MemberCapacityReachedError):
            second.join(team)

    def test_group_mixin_users_skips_external_members_without_crashing(self):
        project = self.create_project("rbac-external-users")
        team = project.team
        creator = self.get_creator(team)
        registered = self.create_user("rbac-registered")
        self._add_team_member(team, registered)
        ProjectMemberService.add(
            project, creator, {"user_id": str(registered.id), "tags": []}
        )
        ProjectMemberService.add(
            project, creator, {"display_name": "外部成员", "tags": []}
        )
        # On real MongoDB the old ``user__ne=None`` filter also matched docs
        # whose ``user`` field is absent (external members) and crashed on
        # member.user.  users() must return only registered users.
        users = project.users()
        self.assertIn(registered, users)
        self.assertEqual(len(users), len([user for user in users if user is not None]))

    def test_team_member_default_display_name_self_and_permissions(self):
        """默认展示名：本人可改（含 trim/清空），越权/版本/长度拒绝，并审计。"""
        project = self.create_project("ddn-service")
        team = project.team
        creator = self.get_creator(team)
        user = self.create_user("ddn-service-user")
        relation = self._add_team_member(team, user)
        other = self.create_user("ddn-service-other")
        other_relation = self._add_team_member(team, other)

        updated = TeamMemberService.update_default_display_name(
            team,
            relation.id,
            user,
            "  我的署名  ",
            expected_version=relation.version,
        )
        self.assertEqual("我的署名", updated.default_display_name)

        # 清空恢复未设置
        cleared = TeamMemberService.update_default_display_name(
            team,
            relation.id,
            user,
            "",
            expected_version=updated.version,
        )
        self.assertEqual("", cleared.default_display_name)
        # 后续断言带当前版本（version 检查先于权限/长度检查执行）
        current_version = cleared.version

        # 普通成员不能修改其他成员
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update_default_display_name(
                team,
                other_relation.id,
                user,
                "越权",
                expected_version=other_relation.version,
            )

        # 非团队成员不能修改任何人的
        with self.assertRaises(NoPermissionError):
            TeamMemberService.update_default_display_name(
                team,
                relation.id,
                self.create_user("ddn-service-outsider"),
                "外人",
                expected_version=current_version,
            )

        # 版本过期
        with self.assertRaises(IdentityVersionConflictError):
            TeamMemberService.update_default_display_name(
                team,
                relation.id,
                user,
                "新名",
                expected_version=current_version - 1,
            )

        # 超长
        with self.assertRaises(InvalidIdentityRequestError):
            TeamMemberService.update_default_display_name(
                team,
                relation.id,
                user,
                "长" * 141,
                expected_version=current_version,
            )

        # 创建者可代设其他成员,并写审计
        before = IdentityAuditEvent.objects(
            action="team_member_default_display_name_update"
        ).count()
        TeamMemberService.update_default_display_name(
            team,
            other_relation.id,
            creator,
            "管理员代设",
            expected_version=other_relation.version,
        )
        self.assertEqual("管理员代设", other_relation.reload().default_display_name)
        self.assertEqual(
            before + 1,
            IdentityAuditEvent.objects(
                action="team_member_default_display_name_update"
            ).count(),
        )

    def test_inviting_team_member_uses_team_default_display_name(self):
        """被邀请加入：非创建者操作者添加团队成员时，邀请投影用默认展示名，
        接受邀请后保持不变。"""
        project = self.create_project("ddn-invite-direct")
        team = project.team
        operator = self.create_user("ddn-invite-direct-operator")
        self._add_team_member(team, operator, qualifications=["translator"])
        target = self.create_user("ddn-invite-direct-target")
        self._add_team_member(team, target, qualifications=["translator"])
        TeamMember.objects(team=team, user=target).update(
            set__default_display_name="受邀默认署名"
        )

        member = ProjectMemberService.add(
            project,
            operator,
            {
                "user_id": str(target.id),
                "display_name": target.name,
                "tags": [],
            },
        )
        # 非创建者走邀请生命周期 → invited 投影；前端预填的站点名视为隐式默认。
        self.assertEqual("invited", member.status)
        self.assertEqual("受邀默认署名", member.display_name)

        invitation = Invitation.objects(
            user=target, group=project, status=InvitationStatus.PENDING
        ).first()
        self.assertIsNotNone(invitation)
        invitation.allow()
        member.reload()
        self.assertEqual("active", member.status)
        self.assertEqual("受邀默认署名", member.display_name)
