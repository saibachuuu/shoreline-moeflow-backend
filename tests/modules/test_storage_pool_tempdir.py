"""存储池暂存目录（``POOL_TRANSFER_TEMP_DIR``）的行为测试。

覆盖三件事：

1. 未配置 / 空白配置 → 跟随系统临时目录（零回归）；
2. 配置非法 → 明确报错，**不静默降级**，也不自动创建目录；
3. 配置合法 → 流式搬运真的落在该目录，成功与异常两条路径都清理干净。
"""

import os
import shutil
import tempfile
from unittest.mock import patch

from tests import MoeTestCase
from tests.modules import requires_module

requires_module("storage_pool")

from app.modules.storage_pool import backend as backend_module
from app.modules.storage_pool.backend import S3Transfer, _transfer_temp_dir


class RecordingBackend:
    """最小假后端：只实现流式搬运用到的方法。

    没有 ``client`` / ``bucket_name``，``S3Transfer`` 会判定为不同 endpoint，
    从而走 ``_stream_transfer`` —— 正是本文件要观察的分支。
    """

    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.downloads = []

    def download(self, path, filename, /, *, local_path=None):
        self.downloads.append(local_path)
        with open(local_path, "wb") as file:
            file.write(self.store.get(path + filename, b""))

    def upload(self, path, filename, file):
        self.store[path + filename] = file.read()

    def head_object(self, key):
        data = self.store.get(key)
        if data is None:
            return None
        return {"key": key, "size": len(data), "etag": ""}


class BrokenDownloadBackend(RecordingBackend):
    """下载阶段直接失败，用来验证 finally 仍然清理临时文件。"""

    def download(self, path, filename, /, *, local_path=None):
        self.downloads.append(local_path)
        raise OSError(28, "No space left on device")


class TransferTempDirTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        # 校验缓存是进程级的：上一个用例校验过的路径会让「目录不存在」的
        # 用例绕过检查。每个用例都从干净状态开始。
        backend_module._VALIDATED_TEMP_DIRS.clear()
        self.tmp_root = tempfile.mkdtemp(prefix="pool-tempdir-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, ignore_errors=True)

    def _configure(self, value):
        self.app.config["POOL_TRANSFER_TEMP_DIR"] = value
        return _transfer_temp_dir()

    # ---- 未配置：行为与改动前一致 ------------------------------------
    def test_unset_falls_back_to_system_temp(self):
        self.assertIsNone(self._configure(None))

    def test_blank_value_falls_back_to_system_temp(self):
        self.assertIsNone(self._configure("   "))

    def test_without_app_context_returns_none(self):
        self.app_context.pop()
        try:
            self.assertIsNone(_transfer_temp_dir())
        finally:
            self.app_context.push()

    # ---- 合法配置 ----------------------------------------------------
    def test_valid_dir_is_returned_and_probe_is_cleaned(self):
        path = self._configure(self.tmp_root)
        self.assertEqual(path, self.tmp_root)
        self.assertEqual(os.listdir(self.tmp_root), [])

    # ---- 非法配置：必须响亮地失败 ------------------------------------
    def test_relative_path_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._configure("relative/tmp")
        self.assertIn("绝对路径", str(ctx.exception))

    def test_missing_dir_is_rejected_and_not_created(self):
        missing = os.path.join(self.tmp_root, "nope")
        with self.assertRaises(RuntimeError) as ctx:
            self._configure(missing)
        self.assertIn("不存在", str(ctx.exception))
        self.assertFalse(os.path.exists(missing))

    def test_file_instead_of_dir_is_rejected(self):
        file_path = os.path.join(self.tmp_root, "afile")
        with open(file_path, "wb"):
            pass
        with self.assertRaises(RuntimeError) as ctx:
            self._configure(file_path)
        self.assertIn("不是目录", str(ctx.exception))

    def test_unwritable_dir_is_rejected(self):
        # root 下 os.access 恒为 True，只读目录也照样能写，所以直接让探针
        # 失败来模拟「目录不可写」，跨平台且不依赖运行用户。
        with (
            patch.object(
                backend_module.tempfile,
                "mkstemp",
                side_effect=PermissionError(13, "Permission denied"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            self._configure(self.tmp_root)
        self.assertIn("不可写", str(ctx.exception))

    # ---- 真正驱动一次流式搬运 ----------------------------------------
    def test_stream_transfer_writes_inside_configured_dir(self):
        self._configure(self.tmp_root)
        src = RecordingBackend({"files/a.png": b"x" * 64})
        dst = RecordingBackend()

        size, _ = S3Transfer(src, dst).transfer("files/a.png", size=64)

        self.assertEqual(size, 64)
        self.assertEqual(dst.store["files/a.png"], b"x" * 64)
        self.assertEqual(len(src.downloads), 1)
        self.assertEqual(os.path.dirname(src.downloads[0]), self.tmp_root)
        self.assertTrue(os.path.basename(src.downloads[0]).startswith("moeflow-pool-"))
        self.assertEqual(os.listdir(self.tmp_root), [])

    def test_stream_transfer_cleans_up_on_failure(self):
        self._configure(self.tmp_root)
        src = BrokenDownloadBackend({"files/a.png": b"x"})
        dst = RecordingBackend()

        with self.assertRaises(OSError):
            S3Transfer(src, dst).transfer("files/a.png", size=1)

        self.assertEqual(os.listdir(self.tmp_root), [])

    def test_invalid_config_never_falls_back_to_system_temp(self):
        # 关键回归：配置写错时绝不能悄悄改用 /tmp，否则用户以为在数据盘
        # 搬运，实际写的是根分区。
        self.app.config["POOL_TRANSFER_TEMP_DIR"] = "relative/path"
        src = RecordingBackend({"files/a.png": b"x"})
        dst = RecordingBackend()

        with self.assertRaises(RuntimeError):
            S3Transfer(src, dst).transfer("files/a.png", size=1)

        self.assertEqual(src.downloads, [])
        self.assertEqual(dst.store, {})
