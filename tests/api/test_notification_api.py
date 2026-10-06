from datetime import timedelta
from unittest.mock import patch
from tests import MoeAPITestCase
from app.models.notification import (
    MODELS,
    Notification,
    NotificationReceipt,
    NotificationDelivery,
    NotificationAudit,
)
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.services import notifications as svc
from app.services.notification_content import parse_body, render_html, NotificationError
from app.tasks.notification import dispatch, deliver


class NotificationTestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(
            ENABLE_NOTIFICATIONS=True,
            ENABLE_USER_EMAIL=False,
            NOTIFICATION_EMAIL_ALLOWLIST=[],
            NOTIFICATION_EMAIL_UNRESTRICTED=False,
        )
        for model in MODELS:
            model.ensure_indexes()
        self.owner = self.create_user("notification-owner")
        self.member = self.create_user("notification-member")
        self.admin = self.create_user("notification-admin")
        self.admin.update(set__admin=True)
        self.admin.reload()
        self.team = Team.create("notification-team", creator=self.owner)
        TeamMember(
            team=self.team, user=self.member, status="active", base_tag="member"
        ).save()
        self.project = Project.create(
            "notification-project", team=self.team, creator=self.owner
        )
        ProjectMember(
            project=self.project,
            user=self.member,
            status="active",
            display_name="member",
            tags=["translator"],
        ).save()

    def data(self, category="team", **kwargs):
        data = {
            "category": category,
            "title": "测试标题",
            "body": "[b]内部通知[/b]",
            "audience": {"mode": "all"},
            "email": True,
        }
        if category != "system":
            data["scope_id"] = str(
                self.team.id if category == "team" else self.project.id
            )
        data.update(kwargs)
        return data

    def send(self, data=None, user=None, operation="test-operation-1"):
        return svc.publish(user or self.owner, data or self.data(), operation)

    def drain(self, note):
        for _ in range(5):
            dispatch(note.id)
        note.reload()

    def call(self, method, path, user=None, data=None, operation="test-operation-1"):
        headers = {
            "Authorization": "Bearer " + (user or self.owner).generate_token(),
            "Idempotency-Key": operation,
        }
        return self.client.open("/v1" + path, method=method, json=data, headers=headers)

    def test_publish_snapshot_and_inbox(self):
        response = self.call("POST", "/notifications", data=self.data())
        self.assertEqual(response.status_code, 202, response.json)
        note = svc.get_note(response.json["id"])
        self.assertEqual(NotificationReceipt.objects.count(), 0)
        self.drain(note)
        self.assertEqual(note.state, "published")
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
        self.assertEqual(
            self.call("GET", "/me/notifications/unread-counts", self.member).json[
                "total"
            ],
            1,
        )
        result = self.call("GET", "/me/notifications", self.member)
        self.assertEqual(len(result.json["items"]), 1)
        self.assertNotIn("body", result.json["items"][0])
        self.assertEqual(
            self.call(
                "PATCH", f"/me/notifications/{note.id}", self.member, {"read": True}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.call("GET", "/me/notifications/unread-counts", self.member).json[
                "total"
            ],
            0,
        )

    def test_admin_view_does_not_grant_publish_or_mark_read(self):
        note = self.send()
        self.drain(note)
        before = list(NotificationReceipt.objects.as_pymongo())
        result = self.call("GET", f"/admin/notifications/{note.id}", self.admin)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertIn("内部通知", result.json["body"])
        self.assertEqual(before, list(NotificationReceipt.objects.as_pymongo()))
        self.assertEqual(
            self.call("POST", "/notifications", self.admin, self.data()).status_code,
            403,
        )
        self.assertEqual(
            self.call(
                "POST", "/admin/notifications", self.admin, self.data()
            ).status_code,
            403,
        )
        self.assertEqual(
            self.call("GET", f"/me/notifications/{note.id}", self.admin).status_code,
            404,
        )
        self.assertEqual(
            self.call("GET", "/admin/notifications", self.member).status_code, 403
        )

    def test_revoke_confirmation_version_and_delivery(self):
        note = self.send()
        self.drain(note)
        path = f"/admin/notifications/{note.id}/revoke"
        self.assertEqual(
            self.call(
                "POST", path, self.admin, {"version": 0, "reason": "滥用"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.call(
                "POST",
                path,
                self.admin,
                {"confirmed": True, "version": 99, "reason": "滥用"},
            ).status_code,
            409,
        )
        data = {"confirmed": True, "version": 0, "reason": "滥用"}
        self.assertEqual(self.call("POST", path, self.admin, data).status_code, 200)
        self.assertEqual(self.call("POST", path, self.admin, data).status_code, 200)
        self.assertEqual(
            self.call("GET", f"/me/notifications/{note.id}", self.member).status_code,
            404,
        )
        self.assertEqual(
            self.call("GET", f"/admin/notifications/{note.id}", self.admin).json[
                "body"
            ],
            note.body,
        )
        self.assertEqual(
            NotificationDelivery.objects(
                notification_id=note.id, state="cancelled"
            ).count(),
            2,
        )
        self.assertEqual(NotificationAudit.objects(action="admin_revoke").count(), 1)

    def test_site_recipient_selection(self):
        data = self.data(
            "system", audience={"mode": "manual", "user_ids": [str(self.member.id)]}
        )
        response = self.call("POST", "/admin/notifications", self.admin, data)
        self.assertEqual(response.status_code, 202, response.json)
        note = svc.get_note(response.json["id"])
        self.drain(note)
        self.assertEqual(note.category, "system")
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).first().user_id,
            self.member.id,
        )
        selected = self.data(
            "system",
            audience={
                "mode": "condition",
                "team_ids": [str(self.team.id)],
                "site_roles": ["member"],
            },
        )
        self.assertEqual(
            self.call(
                "POST", "/admin/notifications/preview", self.admin, selected
            ).json["count"],
            2,
        )

    def test_idempotency_and_conflict(self):
        note = self.send()
        self.assertEqual(self.send().id, note.id)
        with self.assertRaises(NotificationError):
            self.send(self.data(title="不同标题"))
        self.drain(note)
        self.drain(note)
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
        self.assertEqual(
            NotificationDelivery.objects(notification_id=note.id).count(), 2
        )

    def test_removed_member_and_banned_user(self):
        note = self.send()
        self.drain(note)
        TeamMember.objects(team=self.team, user=self.member).update(
            set__status="removed"
        )
        self.assertEqual(
            self.call("GET", f"/me/notifications/{note.id}", self.member).status_code,
            404,
        )
        self.admin.update(set__banned=True)
        self.assertNotEqual(
            self.call("GET", "/admin/notifications", self.admin).status_code, 200
        )

    def test_team_policy_does_not_grant_inherited_project_send(self):
        TeamMember.objects(team=self.team, user=self.member).update(
            set__base_tag="admin"
        )
        self.assertFalse(svc.can_publish(self.member, "team", self.team.id))
        response = self.call(
            "PUT",
            f"/teams/{self.team.id}/notification-policy",
            data={"version": 0, "team_admin": True, "project_admin": True},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(svc.can_publish(self.member, "team", self.team.id))
        self.assertFalse(svc.can_publish(self.member, "project", self.project.id))
        self.assertEqual(
            self.call(
                "PUT",
                f"/teams/{self.team.id}/notification-policy",
                self.member,
                {"version": 1, "team_admin": False, "project_admin": True},
            ).status_code,
            403,
        )

    def test_scheduled_permission_recheck_and_no_later_member_backfill(self):
        note = self.send(
            self.data(
                publish_at=(svc.utcnow() + timedelta(minutes=2)).isoformat() + "Z"
            )
        )
        dispatch(note.id)
        self.assertEqual(NotificationReceipt.objects.count(), 0)
        TeamMember.objects(team=self.team, user=self.owner).update(
            set__base_tag="member"
        )
        note.update(set__publish_at=svc.utcnow() - timedelta(seconds=1))
        self.drain(note)
        self.assertEqual(note.state, "cancelled")

    def test_search_cross_scope_and_secret_exclusion(self):
        note = self.send(self.data(body="[b]仅供内部校对使用[/b]"))
        self.drain(note)
        response = self.call("GET", "/admin/notifications?q=内部校对", self.admin)
        self.assertEqual(len(response.json["items"]), 1, response.json)
        self.assertEqual(
            len(
                self.call("GET", "/me/notifications?q=内部校对", self.admin).json[
                    "items"
                ]
            ),
            0,
        )
        self.assertEqual(
            self.call("GET", "/admin/notifications?q=内", self.admin).status_code, 400
        )

    def test_content_is_safe_and_bounded(self):
        nodes = parse_body("[b]<script>alert(1)</script>[/b]")
        self.assertNotIn("<script>", render_html(nodes))
        for value in (
            "[b]oops",
            "[url=javascript:alert(1)]x[/url]",
            "[b]" * 15 + "x" + "[/b]" * 15,
        ):
            self.assertEqual(
                self.call(
                    "POST", "/notifications/preview", data=self.data(body=value)
                ).status_code,
                400,
            )
        other = self.create_project("hidden-project")
        self.assertEqual(
            self.call(
                "POST",
                "/notifications/preview",
                data=self.data(body=f"[project]{other.id}[/project]"),
            ).status_code,
            403,
        )

    def test_mail_disabled_and_current_preference(self):
        note = self.send()
        self.drain(note)
        row = NotificationDelivery.objects(
            notification_id=note.id, user_id=self.member.id
        ).first()
        with patch("app.tasks.notification.send_notification_mail") as smtp:
            deliver(row.id)
            smtp.assert_not_called()
        row.reload()
        self.assertEqual(row.reason, "channel_disabled")

    def test_transport_unknown_is_not_retried(self):
        self.app.config.update(
            ENABLE_USER_EMAIL=True, NOTIFICATION_TRUST_EXISTING_EMAILS=True
        )
        note = self.send()
        self.drain(note)
        row = NotificationDelivery.objects(
            notification_id=note.id, user_id=self.member.id
        ).first()
        with patch(
            "app.tasks.notification.send_notification_mail",
            return_value=("unknown", "transport_interrupted"),
        ) as smtp:
            deliver(row.id)
            deliver(row.id)
            self.assertEqual(smtp.call_count, 1)
        row.reload()
        self.assertEqual(row.state, "unknown")

    def test_personal_http_and_invalid_fields_rejected(self):
        for data in (
            self.data("personal"),
            self.data(source="module:fake"),
            self.data(audience={"mode": "all", "user_ids": [str(self.member.id)]}),
        ):
            self.assertGreaterEqual(
                self.call("POST", "/notifications", self.admin, data).status_code, 400
            )

    def test_preferences_cas(self):
        data = {"version": 0, "categories": {"team": False}}
        self.assertEqual(
            self.call(
                "PUT", "/me/notification-preferences", self.member, data
            ).status_code,
            200,
        )
        self.assertEqual(
            self.call(
                "PUT", "/me/notification-preferences", self.member, data
            ).status_code,
            409,
        )

    def test_draft_cannot_be_restored_after_moderation(self):
        note = self.send(self.data(draft=True))
        self.call(
            "POST",
            f"/admin/notifications/{note.id}/revoke",
            self.admin,
            {"confirmed": True, "version": 0, "reason": "stop"},
        )
        self.assertEqual(
            self.call(
                "PATCH", f"/notifications/{note.id}", data={**self.data(), "version": 1}
            ).status_code,
            409,
        )

    def test_no_real_mail_in_testing_without_allowlist(self):
        from app.services.notification_mail import send_notification_mail

        self.app.config.update(
            ENABLE_USER_EMAIL=True, NOTIFICATION_EMAIL_UNRESTRICTED=True
        )
        with patch("smtplib.SMTP_SSL") as smtp:
            state, reason = send_notification_mail(
                "real@example.com", "test", "test", message_id="<test@invalid>"
            )
            self.assertEqual((state, reason), ("skipped", "recipient_not_allowed"))
            smtp.assert_not_called()

    def test_proofread_migration_is_single_path_and_retry_stable(self):
        from app.models.file import Source, Translation

        file = self.project.create_file("p01.png")
        target = self.project.targets().first()
        source = Source(file=file, rank=0, content="source").save()
        translation = Translation.create(
            content="before", source=source, target=target, user=self.member
        )
        translation.proofread_content = "after"
        translation.proofreader = self.owner
        translation.save()
        path = f"/projects/{self.project.id}/targets/{target.id}/send-proofread-draft"
        with patch("app.apis.project.send_email") as legacy:
            response = self.call("POST", path, data={"cc_myself": True})
            self.assertEqual(response.status_code, 202, response.json)
            note = svc.get_note(response.json["notification_id"])
            self.assertEqual(note.event_type, "proofread_feedback")
            self.assertIn("after", note.payload["text"])
            self.assertIn("<ins", note.payload["html"])
            self.assertNotIn("recipients", response.json)
            # A retry returns the original operation, even after the underlying draft changes.
            translation.proofread_content = ""
            translation.save()
            self.assertEqual(
                self.call("POST", path, data={"cc_myself": True}).json[
                    "notification_id"
                ],
                str(note.id),
            )
            legacy.assert_not_called()
        self.drain(note)
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )

    def test_snapshot_recovery_after_chunk_write(self):
        from mongoengine.queryset import QuerySet

        original = QuerySet.update_one
        note = self.send()

        def crash(query, *args, **kwargs):
            if query._document is Notification and "inc__next_chunk" in kwargs:
                raise RuntimeError("simulated process death")
            return original(query, *args, **kwargs)

        with patch.object(QuerySet, "update_one", crash):
            with self.assertRaises(RuntimeError):
                dispatch(note.id)
        self.drain(note)
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
        self.assertEqual(note.recipient_count, 2)
        newcomer = self.create_user("late-member")
        TeamMember(team=self.team, user=newcomer, status="active").save()
        self.drain(note)
        self.assertFalse(
            NotificationReceipt.objects(
                notification_id=note.id, user_id=newcomer.id
            ).first()
        )

    def test_recovery_between_receipt_and_outbox(self):
        from mongoengine.queryset import QuerySet

        original = QuerySet.update_one
        note = self.send()

        def crash(query, *args, **kwargs):
            if query._document is NotificationDelivery:
                raise RuntimeError("simulated outbox write failure")
            return original(query, *args, **kwargs)

        with patch.object(QuerySet, "update_one", crash):
            with self.assertRaises(RuntimeError):
                dispatch(note.id)
        self.drain(note)
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
        self.assertEqual(
            NotificationDelivery.objects(notification_id=note.id).count(), 2
        )

    def test_current_email_and_preference_checked_at_delivery(self):
        self.app.config.update(
            ENABLE_USER_EMAIL=True, NOTIFICATION_TRUST_EXISTING_EMAILS=True
        )
        note = self.send()
        self.drain(note)
        row = NotificationDelivery.objects(
            notification_id=note.id, user_id=self.member.id
        ).first()
        self.member.update(set__email="changed@example.test")
        with patch(
            "app.tasks.notification.send_notification_mail",
            return_value=("accepted", ""),
        ) as smtp:
            deliver(row.id)
            self.assertEqual(smtp.call_args.args[0], "changed@example.test")
        row.reload()
        self.assertEqual(row.state, "accepted")
        second = self.send(operation="test-operation-2")
        self.drain(second)
        self.call(
            "PUT",
            "/me/notification-preferences",
            self.member,
            {"version": 0, "categories": {"team": False}},
        )
        other = NotificationDelivery.objects(
            notification_id=second.id, user_id=self.member.id
        ).first()
        with patch("app.tasks.notification.send_notification_mail") as smtp:
            deliver(other.id)
            smtp.assert_not_called()
        other.reload()
        self.assertEqual(other.reason, "preference_disabled")

    def test_security_generation_and_no_notification_record(self):
        from app.models.v_code import VCode
        from app.constants.v_code import VCodeType
        from app.tasks.notification_security import security_email, verification_version

        code = VCode.create(VCodeType.CONFIRM_EMAIL, "new@example.test")
        generation = verification_version(code)
        with patch(
            "app.tasks.notification_security.send_notification_mail",
            return_value=("accepted", ""),
        ) as smtp:
            security_email.run(str(code.id), "stale-generation")
            smtp.assert_not_called()
            security_email.run(str(code.id), generation)
            self.assertEqual(smtp.call_args.args[0], "new@example.test")
            self.assertEqual(Notification.objects.count(), 0)
            code.update(set__expires=svc.utcnow() - timedelta(seconds=1))
            security_email.run(str(code.id), generation)
            self.assertEqual(smtp.call_count, 1)

    def test_security_code_logs_are_redacted_and_verification_recorded(self):
        from app.models.v_code import VCode
        from app.constants.v_code import VCodeType
        from app.models.notification import NotificationVerifiedEmail

        code = VCode.create(VCodeType.CONFIRM_EMAIL, "new@example.test")
        with self.assertLogs("app.utils.logging", level="INFO") as logs:
            code.to_log("email", "new@example.test")
        self.assertNotIn(code.content, " ".join(logs.output))
        self.assertNotIn("new@example.test", " ".join(logs.output))
        VCode.verify(VCodeType.CONFIRM_EMAIL, "new@example.test", code.content)
        self.assertEqual(NotificationVerifiedEmail.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 0)

    def test_protected_smtp_headers_and_acceptance(self):
        from app.services.notification_mail import send_notification_mail

        self.app.config.update(
            ENABLE_USER_EMAIL=True,
            EMAIL_SMTP_HOST="smtp.invalid",
            EMAIL_SMTP_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_ADDRESS="sender@example.test",
            NOTIFICATION_EMAIL_ALLOWLIST=["test@example.test"],
        )
        with patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            smtp.return_value.close.side_effect = RuntimeError(
                "connection lost after acceptance"
            )
            state, _ = send_notification_mail(
                "test@example.test",
                "hello",
                "text",
                "<b>text</b>",
                message_id="<stable@example.test>",
            )
            self.assertEqual(state, "accepted")
            msg = smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(msg["To"], "test@example.test")
            self.assertNotIn("Cc", msg)
            self.assertEqual(msg["Message-ID"], "<stable@example.test>")
        with patch("smtplib.SMTP_SSL") as smtp:
            self.assertEqual(
                send_notification_mail(
                    "test@example.test",
                    "bad\nBcc: x@y.z",
                    "text",
                    message_id="<id@test>",
                )[0],
                "failed",
            )
            smtp.assert_not_called()

    def test_operational_mail_independent_and_rate_limited(self):
        import logging
        import threading
        from collections import deque
        from app.utils.logging import SMTPSSLHandler

        handler = SMTPSSLHandler(
            ("smtp.invalid", 465), "sender@example.test", ["ops@example.test"], "Error"
        )
        handler.notification_config = {
            **self.app.config,
            "ENABLE_LOG_EMAIL": True,
            "ENABLE_USER_EMAIL": False,
        }
        handler.notification_lock = threading.Lock()
        handler.notification_times = deque()
        record = logging.LogRecord(
            "example", logging.ERROR, __file__, 1, "operational diagnostic", (), None
        )
        with patch(
            "app.services.notification_mail.send_protected_mail",
            return_value=("skipped", "recipient_not_allowed"),
        ) as smtp:
            for _ in range(9):
                handler.emit(record)
            self.assertEqual(smtp.call_count, 5)
            self.assertFalse(smtp.call_args.kwargs["user_mail"])
        self.assertEqual(Notification.objects.count(), 0)

    def test_revoke_audit_recovers_after_audit_write_failure(self):
        note = self.send()
        with patch(
            "app.services.notifications.audit",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                svc.revoke(
                    self.admin,
                    note,
                    {"confirmed": True, "version": 0, "reason": "stop"},
                    "revoke-operation",
                    admin=True,
                )
        note.reload()
        self.assertTrue(note.revoked_at)
        self.assertFalse(note.moderation_audited)
        from app.tasks.notification import notification_scan

        notification_scan.run()
        note.reload()
        self.assertTrue(note.moderation_audited)
        self.assertEqual(
            NotificationAudit.objects(
                notification_id=note.id, action="admin_revoke"
            ).count(),
            1,
        )

    def test_mark_read_and_archive_leave_other_recipients_untouched(self):
        note = self.send()
        self.drain(note)
        self.call(
            "POST", "/me/notifications/mark-read", self.member, {"category": "team"}
        )
        self.assertIsNotNone(
            NotificationReceipt.objects(user_id=self.member.id).first().read_at
        )
        self.assertIsNone(
            NotificationReceipt.objects(user_id=self.owner.id).first().read_at
        )
        self.call(
            "PATCH", f"/me/notifications/{note.id}", self.member, {"archived": True}
        )
        self.assertEqual(
            len(self.call("GET", "/me/notifications", self.member).json["items"]), 0
        )
        self.assertEqual(
            len(
                self.call("GET", "/me/notifications?archived=true", self.member).json[
                    "items"
                ]
            ),
            1,
        )

    def test_single_recipient_check_matches_snapshot_selection(self):
        cases = [
            self.data(),
            self.data("project"),
            self.data(
                "project", audience={"mode": "condition", "tags": ["translator"]}
            ),
            self.data("team", audience={"mode": "condition", "base_roles": ["member"]}),
            self.data(
                "system", audience={"mode": "condition", "site_roles": ["admin"]}
            ),
            self.data(
                "system",
                audience={"mode": "condition", "team_ids": [str(self.team.id)]},
            ),
            self.data(
                "system", audience={"mode": "manual", "user_ids": [str(self.member.id)]}
            ),
        ]
        for data in cases:
            note = svc.make_note(
                self.admin if data["category"] == "system" else self.owner, data
            )
            selected = set(svc.recipient_ids(note))
            for user in (self.owner, self.member, self.admin):
                self.assertEqual(
                    svc.recipient_eligible(note, user), user.id in selected
                )

    def test_atomic_throttle_rejects_over_limit(self):
        svc.throttle(self.owner, "test", 2)
        svc.throttle(self.owner, "test", 2)
        with self.assertRaises(NotificationError) as error:
            svc.throttle(self.owner, "test", 2)
        self.assertEqual(error.exception.status_code, 429)

    def test_proofreader_sent_history_does_not_grant_announcement_rights(self):
        ProjectMember.objects(project=self.project, user=self.member).update(
            set__tags=["proofreader"]
        )
        note = svc.publish(
            self.member,
            self.data("project"),
            "proofreader-operation",
            trusted=True,
            event_type="proofread_feedback",
            payload={"text": "<script>safe text</script>"},
        )
        caps = self.call(
            "GET",
            f"/me/notification-capabilities?category=project&scope_id={self.project.id}",
            self.member,
        )
        self.assertTrue(caps.json["can_view_sent"])
        self.assertFalse(caps.json["can_send"])
        detail = self.call("GET", f"/notifications/{note.id}", self.member)
        self.assertEqual(detail.status_code, 200, detail.json)
        self.assertEqual(detail.json["feedback_text"], "<script>safe text</script>")
        sent = self.call(
            "GET",
            f"/notifications/sent?category=project&scope_id={self.project.id}",
            self.member,
        )
        self.assertEqual([n["id"] for n in sent.json["items"]], [str(note.id)])
        denied = self.call(
            "POST",
            "/notifications",
            self.member,
            self.data("project"),
            "generic-denied",
        )
        self.assertEqual(denied.status_code, 403, denied.json)

    def test_scoped_manager_list_matches_detail_authorization(self):
        from app.models.notification import NotificationPolicy

        TeamMember.objects(team=self.team, user=self.member).update(
            set__base_tag="admin"
        )
        NotificationPolicy(team_id=self.team.id, team_admin=True).save()
        owner_note = self.send(operation="owner-notice")
        member_note = self.send(user=self.member, operation="member-notice")
        path = f"/notifications/sent?category=team&scope_id={self.team.id}"
        rows = self.call("GET", path, self.member).json["items"]
        self.assertEqual([n["id"] for n in rows], [str(member_note.id)])
        self.assertEqual(len(self.call("GET", path, self.owner).json["items"]), 2)
        self.assertEqual(
            self.call(
                "GET", f"/notifications/{owner_note.id}", self.member
            ).status_code,
            403,
        )

    def test_inbox_and_admin_cursor_search_excerpt(self):
        notes = [
            self.send(
                self.data(title=f"正文检索{i}", body="[b]候选搜索内容[/b]"),
                operation=f"cursor-notice-{i}",
            )
            for i in range(5)
        ]
        for note in notes:
            self.drain(note)
        for user, route in [
            (self.member, "/me/notifications"),
            (self.admin, "/admin/notifications"),
        ]:
            result = self.call("GET", route + "?q=候选&limit=2", user)
            self.assertEqual(result.status_code, 200, result.json)
            first = result.json
            self.assertEqual(len(first["items"]), 2)
            self.assertIn("候选", first["items"][0]["excerpt"])
            self.assertNotIn("[b]", first["items"][0]["excerpt"])
            second = self.call(
                "GET", route + "?q=候选&limit=2&cursor=" + first["next_cursor"], user
            ).json
            self.assertFalse(
                {n["id"] for n in first["items"]} & {n["id"] for n in second["items"]}
            )
        self.assertEqual(
            self.call("GET", "/me/notifications?q=候", self.member).status_code, 400
        )

    def test_parallel_publication_has_one_root(self):
        from concurrent.futures import ThreadPoolExecutor

        def send_once(_):
            with self.app.app_context():
                return self.send(operation="parallel-publication").id

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(send_once, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(Notification.objects.count(), 1)

    def test_scheduled_edit_cannot_race_with_worker_lease(self):
        note = self.send(
            self.data(
                publish_at=(svc.utcnow() + timedelta(minutes=10)).isoformat() + "Z"
            )
        )
        note.update(
            set__lease_until=svc.utcnow() + timedelta(minutes=2),
            set__lease_token="worker",
        )
        with self.assertRaises(NotificationError):
            svc.edit_draft(
                self.owner, note, {**self.data(), "version": 0, "draft": False}
            )
        note.reload()
        self.assertEqual(note.state, "scheduled")

    def test_personal_source_is_only_available_to_trusted_service(self):
        name = "module:notification_test"
        svc.SOURCES.pop(name, None)
        svc.register_source(name, lambda actor, data: actor.id == self.owner.id)
        self.addCleanup(lambda: svc.SOURCES.pop(name, None))
        data = self.data(
            category="personal",
            audience={"mode": "manual", "user_ids": [str(self.member.id)]},
        )
        data.pop("scope_id", None)
        note = svc.publish_personal(self.owner, name, data, "trusted-personal")
        self.drain(note)
        self.assertEqual(
            self.call("GET", f"/me/notifications/{note.id}", self.member).status_code,
            200,
        )
        with self.assertRaises(NotificationError):
            svc.publish_personal(self.member, name, data, "forged-personal")

    def test_legacy_queued_mail_payload_is_still_consumable(self):
        from app import celery
        from app.tasks.email import email_task

        config = {
            **celery.conf.app_config,
            "ENABLE_USER_EMAIL": True,
            "EMAIL_USE_SSL": True,
        }
        with (
            patch.dict(celery.conf.app_config, config),
            patch("app.tasks.email.smtplib.SMTP_SSL") as smtp,
        ):
            # Existing positional payload (including Cc) is intentionally still supported.
            result = email_task.run(
                "old@example.test",
                "queued",
                "<p>old</p>",
                "old",
                None,
                None,
                None,
                ["copy@example.test"],
            )
            self.assertEqual(result, "发送成功")
            self.assertEqual(
                smtp.return_value.sendmail.call_args.args[1],
                ["old@example.test", "copy@example.test"],
            )

    def test_cross_origin_publication_preflight_allows_operation_key(self):
        response = self.client.open(
            "/v1/notifications",
            method="OPTIONS",
            headers={
                "Origin": "https://frontend.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "IDEMPOTENCY-KEY", response.headers["Access-Control-Allow-Headers"].upper()
        )
        self.assertEqual(Notification.objects.count(), 0)

    def test_sync_combines_badge_list_and_permissions_with_conditional_revision(self):
        first = self.send(operation="sync-one")
        self.drain(first)
        second = self.send(self.data(title="other subject"), operation="sync-two")
        self.drain(second)
        response = self.call(
            "GET", "/me/notification-sync?view=inbox&q=other", self.member
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["counts"]["total"], 2)
        self.assertEqual(len(response.json["page"]["items"]), 1)
        self.assertTrue(response.json["capabilities"]["enabled"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        unchanged = self.call(
            "GET",
            "/me/notification-sync?view=inbox&q=other&revision="
            + response.json["revision"],
            self.member,
        )
        self.assertTrue(unchanged.json["unchanged"])
        self.assertNotIn("page", unchanged.json)
        self.call(
            "PATCH", f"/me/notifications/{second.id}", self.member, {"read": True}
        )
        changed = self.call(
            "GET",
            "/me/notification-sync?view=inbox&q=other&revision="
            + response.json["revision"],
            self.member,
        )
        self.assertEqual(changed.json["counts"]["total"], 1)
        self.assertTrue(changed.json["page"]["items"][0]["read"])

    def test_sync_rechecks_revocation_and_never_shares_management_projection(self):
        note = self.send()
        self.drain(note)
        route = f"/me/notification-sync?view=inbox_detail&notification_id={note.id}"
        before = self.call("GET", route, self.member)
        self.assertEqual(before.json["notice"]["id"], str(note.id))
        denied = self.call("GET", "/me/notification-sync?view=admin", self.member)
        self.assertEqual(denied.json["view_error"]["status"], 403)
        self.assertNotIn("page", denied.json)
        svc.revoke(
            self.admin,
            note,
            {"confirmed": True, "version": note.version, "reason": "stop"},
            "sync-revoke",
            admin=True,
        )
        after = self.call(
            "GET", route + "&revision=" + before.json["revision"], self.member
        )
        self.assertEqual(after.json["view_error"]["status"], 404)
        self.assertNotIn("notice", after.json)
        self.assertEqual(after.json["counts"]["total"], 0)
        admin = self.call(
            "GET",
            f"/me/notification-sync?view=admin_detail&notification_id={note.id}",
            self.admin,
        )
        self.assertEqual(admin.json["notice"]["id"], str(note.id))
        self.assertTrue(admin.json["notice"]["revoked_at"])

    def test_sync_disabled_and_scoped_capability(self):
        response = self.call(
            "GET",
            f"/me/notification-sync?view=scope&category=team&scope_id={self.team.id}",
        )
        self.assertTrue(response.json["capabilities"]["can_send"])
        self.app.config["ENABLE_NOTIFICATIONS"] = False
        disabled = self.call("GET", "/me/notification-sync?view=inbox")
        self.assertFalse(disabled.json["enabled"])
        self.assertEqual(disabled.json["counts"]["total"], 0)
        self.assertNotIn("page", disabled.json)

    def test_small_audience_fanout_finishes_in_one_scan(self):
        note = self.send()
        dispatch(note.id)
        note.reload()
        self.assertEqual(note.state, "published")
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
        dispatch(note.id)
        self.assertEqual(
            NotificationReceipt.objects(notification_id=note.id).count(), 2
        )
