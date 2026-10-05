"""Only ADMIN_EMAIL may change site administrator status; other admin APIs stay intact."""

from app.exceptions import NeedTokenError, NoPermissionError, UserBannedError
from app.models.user import User
from tests import MoeAPITestCase


class SiteAdminPermissionsAPITestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.owner = User.get_by_email(self.app.config["ADMIN_EMAIL"])
        self.delegate = self.create_user("delegated-admin")
        self.delegate.update(set__admin=True)
        self.delegate.reload()
        self.member = self.create_user("ordinary-member")

    def change_status(self, actor, target, status, **extra):
        return self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(target.id), "status": status, **extra},
            token=actor.generate_token() if actor else None,
        )

    def test_owner_can_grant_and_revoke(self):
        self.assertErrorEqual(self.change_status(self.owner, self.member, True))
        self.assertTrue(self.member.reload().admin)
        self.assertErrorEqual(self.change_status(self.owner, self.member, False))
        self.assertFalse(self.member.reload().admin)
        self.assertErrorEqual(self.change_status(self.owner, self.delegate, False))
        self.assertFalse(self.delegate.reload().admin)

    def test_delegated_admin_cannot_grant_revoke_or_demote_owner(self):
        for target, status in (
            (self.member, True),
            (self.owner, False),
            (self.delegate, False),
        ):
            with self.subTest(target=target.name):
                previous = target.admin
                self.assertErrorEqual(
                    self.change_status(self.delegate, target, status),
                    NoPermissionError,
                )
                self.assertEqual(target.reload().admin, previous)

    def test_member_cannot_self_promote_or_demote_owner(self):
        self.assertErrorEqual(
            self.change_status(self.member, self.member, True), NoPermissionError
        )
        self.assertErrorEqual(
            self.change_status(self.member, self.owner, False), NoPermissionError
        )
        self.assertFalse(self.member.reload().admin)
        self.assertTrue(self.owner.reload().admin)

    def test_anonymous_cannot_change_status(self):
        self.assertErrorEqual(
            self.change_status(None, self.member, True), NeedTokenError
        )
        self.assertFalse(self.member.reload().admin)

    def test_capability_or_email_in_payload_cannot_impersonate_owner(self):
        self.assertErrorEqual(
            self.change_status(
                self.delegate,
                self.member,
                True,
                can_manage_site_admins=True,
                email=self.owner.email,
                ADMIN_EMAIL=self.delegate.email,
            ),
            NoPermissionError,
        )
        self.assertFalse(self.member.reload().admin)

    def test_invalid_config_fails_closed(self):
        for value in (None, "", "   ", 123, "unregistered@example.com"):
            with self.subTest(configured=value):
                self.app.config["ADMIN_EMAIL"] = value
                self.assertFalse(self.owner.can_manage_site_admins())
                self.assertErrorEqual(
                    self.change_status(self.owner, self.member, True),
                    NoPermissionError,
                )
        self.app.config.pop("ADMIN_EMAIL")
        self.assertFalse(self.owner.can_manage_site_admins())
        self.assertErrorEqual(
            self.change_status(self.owner, self.member, True), NoPermissionError
        )

    def test_configured_email_is_case_insensitive(self):
        self.app.config["ADMIN_EMAIL"] = " " + self.owner.email.upper() + " "
        self.assertTrue(self.owner.can_manage_site_admins())
        self.assertErrorEqual(self.change_status(self.owner, self.member, True))

    def test_changing_configuration_reassigns_only_special_capability(self):
        self.app.config["ADMIN_EMAIL"] = self.delegate.email
        self.assertErrorEqual(
            self.change_status(self.owner, self.member, True), NoPermissionError
        )
        self.assertErrorEqual(self.change_status(self.delegate, self.member, True))
        self.assertTrue(self.owner.reload().admin)
        self.assertTrue(self.owner.admin_can())

    def test_configured_non_admin_does_not_receive_permission(self):
        self.app.config["ADMIN_EMAIL"] = self.member.email
        self.assertErrorEqual(
            self.change_status(self.member, self.member, True), NoPermissionError
        )
        self.assertFalse(self.member.reload().admin)

    def test_owner_demotion_is_checked_again_even_with_existing_token(self):
        token = self.owner.generate_token()
        # Preserve the existing ability to demote oneself; no new self-protection rule.
        self.assertErrorEqual(self.change_status(self.owner, self.owner, False))
        response = self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(self.member.id), "status": True},
            token=token,
        )
        self.assertErrorEqual(response, NoPermissionError)
        self.assertFalse(self.member.reload().admin)

    def test_banned_owner_cannot_manage_admins(self):
        self.owner.update(set__banned=True)
        self.owner.reload()
        self.assertFalse(self.owner.can_manage_site_admins())
        self.assertErrorEqual(
            self.change_status(self.owner, self.member, True), UserBannedError
        )

    def test_changing_owner_email_revokes_special_permission(self):
        self.owner.update(set__email="different-owner@example.com")
        self.owner.reload()
        self.assertErrorEqual(
            self.change_status(self.owner, self.member, True), NoPermissionError
        )
        self.assertTrue(self.owner.admin_can())

    def test_current_user_info_exposes_only_own_capability(self):
        for actor, expected in (
            (self.owner, True),
            (self.delegate, False),
            (self.member, False),
        ):
            with self.subTest(actor=actor.name):
                response = self.get("/v1/user/info", token=actor.generate_token())
                self.assertErrorEqual(response)
                self.assertIs(response.json["can_manage_site_admins"], expected)
                self.assertIs(response.json["admin"], actor.admin)

    def test_public_profile_does_not_expose_owner_capability(self):
        response = self.get("/v1/users/" + self.owner.name)
        self.assertErrorEqual(response)
        self.assertNotIn("can_manage_site_admins", response.json)

    def test_user_list_does_not_expose_other_users_owner_capability(self):
        response = self.get(
            "/v1/admin/users?word=", token=self.delegate.generate_token()
        )
        self.assertErrorEqual(response)
        for item in response.json:
            if item["id"] != str(self.delegate.id):
                self.assertNotIn("can_manage_site_admins", item)

    def test_delegated_admin_keeps_other_administration_permissions(self):
        token = self.delegate.generate_token()
        self.assertTrue(self.delegate.admin_can())
        self.assertErrorEqual(self.get("/v1/admin/site-setting", token=token))
        self.assertErrorEqual(self.get("/v1/admin/users?word=", token=token))
        self.assertErrorEqual(
            self.post(
                "/v1/admin/users",
                token=token,
                json={
                    "name": "created_by_admin",
                    "email": "created@example.com",
                    "password": "test-admin-created-password",
                },
            )
        )
        # Password management is deliberately not changed by this narrow request.
        self.assertErrorEqual(
            self.put(
                "/v1/admin/users/" + str(self.member.id),
                token=token,
                json={"password": "changed-by-delegated-admin"},
            )
        )
        self.assertFalse(self.member.reload().admin)
