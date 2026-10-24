"""校对稿烤图（标签定位标记）单元测试。"""

from unittest.mock import MagicMock, patch

from PIL import Image

from app.services import proofread_image
from tests import MoeAPITestCase


class MarkedProofreadImageTestCase(MoeAPITestCase):
    def test_marker_fingerprint(self):
        """指纹对相同标签集合稳定，坐标变化后改变。"""
        labels = [{"x": 0.5, "y": 0.3, "position_type": 1, "label_num": 1}]
        fingerprint = proofread_image.marker_fingerprint(labels)
        self.assertEqual(fingerprint, proofread_image.marker_fingerprint(labels.copy()))
        moved = [{"x": 0.5, "y": 0.4, "position_type": 1, "label_num": 1}]
        self.assertNotEqual(fingerprint, proofread_image.marker_fingerprint(moved))

    def test_render_draws_in_and_out_markers(self):
        """框内画粉点、框外画黄点、无坐标条目跳过；小图不放大、大图缩宽。"""
        base = Image.new("RGB", (200, 400), (255, 255, 255))
        labels = [
            {"x": 0.5, "y": 0.25, "position_type": 1, "label_num": 1},
            {"x": 0.25, "y": 0.75, "position_type": 2, "label_num": 2},
            {"x": 0, "y": 0, "position_type": 1, "label_num": 3},
        ]
        with patch.object(proofread_image, "_load_base_image", return_value=base):
            out = proofread_image._render(MagicMock(), labels)
        self.assertEqual(out.width, 200)  # 小于目标宽度时不放大
        self.assertEqual(out.mode, "RGBA")
        # 半径 = max(9, 宽*0.05/2)，向右偏 radius-2 取样：仍在圆内且避开居中数字
        self.assertEqual(
            out.getpixel((100 + 7, 100))[:3], proofread_image.IN_COLOR[:3]
        )
        self.assertEqual(
            out.getpixel((50 + 7, 300))[:3], proofread_image.OUT_COLOR[:3]
        )
        # 第 3 个条目无坐标，保持底色
        self.assertEqual(out.getpixel((10, 380)), (255, 255, 255, 255))

        big = Image.new("RGB", (1280, 2560), (255, 255, 255))
        with patch.object(proofread_image, "_load_base_image", return_value=big):
            out = proofread_image._render(MagicMock(), labels)
        self.assertEqual(out.width, proofread_image.TARGET_WIDTH)

    def test_marked_image_url_reuses_existing(self):
        """烤图已存在时直接签名复用，不重新生成。"""
        file = MagicMock()
        file.save_name = "abc.jpg"
        file.url = "http://orig"
        labels = [{"x": 0.5, "y": 0.3, "position_type": 1, "label_num": 1}]
        with patch("app.oss") as mock_oss, patch(
            "flask.current_app"
        ) as mock_app:
            mock_app.config = {"OSS_FILE_PREFIX": "file/"}
            mock_oss.is_exist.return_value = True
            mock_oss.sign_url.return_value = "http://signed/marked.webp"
            url = proofread_image.marked_image_url(file, labels)
        self.assertEqual(url, "http://signed/marked.webp")
        mock_oss.upload.assert_not_called()

    def test_marked_image_url_generates_and_uploads(self):
        """不存在时生成 webp 并上传。"""
        file = MagicMock()
        file.save_name = "abc.jpg"
        file.url = "http://orig"
        labels = [{"x": 0.5, "y": 0.3, "position_type": 1, "label_num": 1}]
        base = Image.new("RGB", (100, 200), (255, 255, 255))
        with patch("app.oss") as mock_oss, patch(
            "flask.current_app"
        ) as mock_app, patch.object(
            proofread_image, "_load_base_image", return_value=base
        ):
            mock_app.config = {"OSS_FILE_PREFIX": "file/"}
            mock_oss.is_exist.return_value = False
            mock_oss.sign_url.return_value = "http://signed/new.webp"
            url = proofread_image.marked_image_url(file, labels)
        self.assertEqual(url, "http://signed/new.webp")
        mock_oss.upload.assert_called_once()
        uploaded = mock_oss.upload.call_args[0][2]
        image = Image.open(uploaded)
        self.assertEqual(image.format, "WEBP")

    def test_marked_image_url_falls_back_when_render_fails(self):
        """底图不可用时降级为原图 URL。"""
        file = MagicMock()
        file.save_name = "abc.jpg"
        file.url = "http://orig"
        labels = [{"x": 0.5, "y": 0.3, "position_type": 1, "label_num": 1}]
        with patch("app.oss") as mock_oss, patch(
            "flask.current_app"
        ) as mock_app, patch.object(
            proofread_image, "_load_base_image", return_value=None
        ):
            mock_app.config = {"OSS_FILE_PREFIX": "file/"}
            mock_oss.is_exist.return_value = False
            url = proofread_image.marked_image_url(file, labels)
        self.assertEqual(url, "http://orig")

    def test_marked_image_url_skips_empty_labels(self):
        """无标签（纯文本文件等）直接返回原图 URL，不触碰存储。"""
        file = MagicMock()
        file.save_name = "abc.jpg"
        file.url = "http://orig"
        with patch("app.oss") as mock_oss:
            url = proofread_image.marked_image_url(file, [])
        self.assertEqual(url, "http://orig")
        mock_oss.is_exist.assert_not_called()
