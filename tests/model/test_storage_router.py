"""储存路由（核心空注册表 + 项目指针）的测试。

阶段 2 的验收标准只有一条：**注册表为空时，行为与改造前完全一致**；
接上注册器后，读写都跟着 ``Project.storage_source_id`` 走，且列表序列化
不会退化成 N+1。
"""

from unittest import TestCase
from unittest.mock import Mock, patch

from bson import ObjectId

from app.constants.file import ThumbnailStatus
from app.models.project import Project
from app.services import storage_router
from tests import MoeTestCase


class RouterUnitTestCase(TestCase):
    """不碰数据库的部分：注册表语义。"""

    def setUp(self):
        storage_router.clear()

    def tearDown(self):
        storage_router.clear()

    def test_empty_registry_returns_none(self):
        self.assertFalse(storage_router.has_resolvers())
        project = Mock(storage_source_id="abc")
        self.assertIsNone(storage_router.backend_for(project))
        self.assertIsNone(storage_router.backend_for_id("abc"))

    def test_registered_resolver_wins(self):
        backend = Mock(name="pool-backend")
        storage_router.register(lambda source_id: backend)
        self.assertTrue(storage_router.has_resolvers())
        self.assertEqual(
            backend, storage_router.backend_for(Mock(storage_source_id="abc"))
        )

    def test_none_source_id_falls_back_to_default(self):
        storage_router.register(lambda source_id: Mock(name="unreachable"))
        self.assertIsNone(storage_router.backend_for(Mock(storage_source_id=None)))

    def test_unclaimed_source_falls_back_to_default(self):
        storage_router.register(lambda source_id: None)
        self.assertIsNone(storage_router.backend_for(Mock(storage_source_id="abc")))

    def test_failing_resolver_does_not_break_reads(self):
        def boom(source_id):
            raise RuntimeError("pool module exploded")

        storage_router.register(boom)
        # 解析器炸了只应退化，不应把整个读路径带崩。
        self.assertIsNone(storage_router.backend_for(Mock(storage_source_id="abc")))

    def test_duplicate_register_is_ignored(self):
        def resolver(source_id):
            return None

        storage_router.register(resolver)
        storage_router.register(resolver)
        self.assertEqual(1, len(storage_router._resolvers))


class FileRoutingTestCase(MoeTestCase):
    """接上注册器后，读写是否真的跟着项目指针走。"""

    def setUp(self):
        super().setUp()
        storage_router.clear()
        self.pool_backend = Mock(name="pool-backend")
        self.pool_backend.sign_url.return_value = "https://pool.test/files/a.png"

    def tearDown(self):
        storage_router.clear()
        super().tearDown()

    def _point_project_at_pool(self, project):
        # 直接写库，模拟存储池模块切换指针后的状态。
        # storage_source_id 是裸 ObjectIdField，给个真 ObjectId。
        self.pool_source_id = ObjectId()
        Project.objects(id=project.id).update(storage_source_id=self.pool_source_id)
        project.reload()
        storage_router.register(
            lambda source_id: (
                self.pool_backend
                if str(source_id) == str(self.pool_source_id)
                else None
            )
        )

    def test_file_url_uses_pool_backend_when_project_is_migrated(self):
        project = self.create_project("routed-url")
        image = project.create_file("a.png")
        image.update(save_name="a.png")
        image.reload()

        # 指针未设：仍走默认存储（与改造前一致）
        with patch("app.models.file.oss.sign_url", return_value="default-url") as dflt:
            self.assertEqual("default-url", image.url)
        dflt.assert_called_once()

        self._point_project_at_pool(project)
        self.assertEqual("https://pool.test/files/a.png", image.url)
        self.pool_backend.sign_url.assert_called_once()

    def test_derived_images_stay_on_default_storage(self):
        """派生图永远在默认存储 —— 误路由会让所有缩略图 404。"""
        project = self.create_project("routed-derived")
        image = project.create_file("a.png")
        image.update(save_name="a.png", thumbnail_status=ThumbnailStatus.SUCCEEDED)
        image.reload()

        self._point_project_at_pool(project)
        with patch(
            "app.models.file.oss.sign_url", return_value="default-cover-url"
        ) as dflt:
            self.assertEqual("default-cover-url", image.cover_url)
        dflt.assert_called_once()
        self.pool_backend.sign_url.assert_not_called()

    def test_download_real_file_uses_pool_backend(self):
        project = self.create_project("routed-download")
        image = project.create_file("a.png")
        image.update(save_name="a.png")
        image.reload()

        self._point_project_at_pool(project)
        image.download_real_file()
        self.pool_backend.download.assert_called_once()

    def test_url_does_not_add_a_query_per_file(self):
        """N+1 回归：一页文件只应解析一次项目。"""
        project = self.create_project("routed-n+1")
        files = []
        for index in range(5):
            image = project.create_file(f"{index}.png")
            image.update(save_name=f"{index}.png")
            image.reload()
            files.append(image)

        self._point_project_at_pool(project)

        original_manager = Project.objects
        calls = {"count": 0}

        def counting_manager(*args, **kwargs):
            calls["count"] += 1
            return original_manager(*args, **kwargs)

        with patch.object(Project, "objects", counting_manager):
            with self.app.test_request_context():
                urls = [file.url for file in files]

        self.assertEqual(5, len(urls))
        self.assertEqual(1, calls["count"], "同一页文件应只解析一次项目")

    def test_backend_is_ignored_when_project_has_no_pointer(self):
        project = self.create_project("routed-plain")
        image = project.create_file("a.png")
        image.update(save_name="a.png")
        image.reload()
        storage_router.register(lambda source_id: self.pool_backend)

        with patch("app.models.file.oss.sign_url", return_value="default-url"):
            self.assertEqual("default-url", image.url)
        self.pool_backend.sign_url.assert_not_called()
