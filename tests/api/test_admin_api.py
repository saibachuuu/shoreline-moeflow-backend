from app.exceptions.base import NoPermissionError
from app.models.user import User
from tests import MoeAPITestCase


class AdminAPITestCase(MoeAPITestCase):
    def test_admin_admin_status_api(self):
        """仅配置的站点所有者（ADMIN_EMAIL）可以修改管理员状态"""
        owner = User.get_by_email(self.app.config["ADMIN_EMAIL"])
        owner_token = owner.generate_token()
        # Owner can enable admin status
        user = self.create_user("user")
        token = user.generate_token()
        self.assertFalse(user.admin)
        data = self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(user.id), "status": True},
            token=owner_token,
        )
        self.assertErrorEqual(data)
        user.reload()
        self.assertTrue(user.admin)
        # Owner can disable admin status
        data = self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(user.id), "status": False},
            token=owner_token,
        )
        self.assertErrorEqual(data)
        user.reload()
        self.assertFalse(user.admin)
        # Other admins cannot edit admin status (owner-only since 1.2.6)
        delegated = self.create_user("delegated-admin")
        delegated.admin = True
        delegated.save()
        data = self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(user.id), "status": True},
            token=delegated.generate_token(),
        )
        self.assertErrorEqual(data, NoPermissionError)
        # Non-admin cannot edit admin status
        data = self.put(
            "/v1/admin/admin-status",
            json={"user_id": str(user.id), "status": True},
            token=token,
        )
        self.assertErrorEqual(data, NoPermissionError)
