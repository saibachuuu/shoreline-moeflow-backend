"""为校对稿邮件生成带标签定位标记的页面图。

邮件客户端（Gmail 等）会剥离 position:absolute 等定位 CSS，无法在
HTML 里把标记叠加到图片上，因此标记在发送前直接烤进图片副本：
按每个标签的 x/y 百分比坐标画圆点（颜色区分框内/框外），存为
`proofread-{指纹}-{原名}.webp`。指纹来自标签坐标集合，坐标变化后
旧烤图自然失效，下一次发送重新生成。
"""

import hashlib
import io

from PIL import Image, ImageDraw, ImageFont

# 与前端编辑器标记配色一致（style.ts primaryColor / Label.tsx 框外黄色）
IN_COLOR = (255, 101, 124, 235)
OUT_COLOR = (255, 213, 131, 235)
IN_TEXT_COLOR = (255, 255, 255, 255)
OUT_TEXT_COLOR = (102, 77, 3, 255)
MARKER_RATIO = 0.05  # 圆点直径 / 图宽
MARKER_MIN_RADIUS = 9  # 像素，小图下限
TARGET_WIDTH = 640  # 烤图统一宽度（邮件列宽 ~280 的 2x 余量）


def marker_fingerprint(labels):
    """标签坐标指纹，用于缓存失效。"""
    raw = repr(
        [(l["x"], l["y"], l["position_type"], l["label_num"]) for l in labels]
    ).encode()
    return hashlib.md5(raw).hexdigest()[:8]


def marked_image_url(file, labels):
    """返回带标记烤图的签名 URL；任何失败都降级为原图 URL。"""
    from flask import current_app

    from app import oss

    if not labels or not file.save_name:
        return file.url
    processed_name = (
        f"proofread-{marker_fingerprint(labels)}-{file.save_name.rsplit('.', 1)[0]}.webp"
    )
    file_prefix = current_app.config["OSS_FILE_PREFIX"]
    try:
        if oss.is_exist(file_prefix, processed_name):
            return oss.sign_url(file_prefix, processed_name)
        image = _render(file, labels)
        if image is None:
            return file.url
        buf = io.BytesIO()
        image.save(buf, format="WEBP", quality=85)
        buf.seek(0)
        oss.upload(file_prefix, processed_name, io.BufferedReader(buf))
        return oss.sign_url(file_prefix, processed_name)
    except Exception:
        # 标记是增强体验，任何存储/绘图失败都不应阻塞校对稿发送
        return file.url


def _render(file, labels):
    """在页面图上画定位圆点，返回 RGBA Image；底图不可用时返回 None。"""
    image = _load_base_image(file)
    if image is None:
        return None
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    if image.width > TARGET_WIDTH:
        ratio = TARGET_WIDTH / image.width
        image = image.resize(
            (TARGET_WIDTH, round(image.height * ratio)), Image.LANCZOS
        )
    draw = ImageDraw.Draw(image)
    radius = max(MARKER_MIN_RADIUS, round(image.width * MARKER_RATIO / 2))
    font = ImageFont.load_default(size=max(9, radius))
    for label in labels:
        if not (label["x"] or label["y"]):
            continue  # 未标记坐标的条目不画
        center_x = image.width * label["x"]
        center_y = image.height * label["y"]
        out = label["position_type"] == 2
        fill = OUT_COLOR if out else IN_COLOR
        text_fill = OUT_TEXT_COLOR if out else IN_TEXT_COLOR
        draw.ellipse(
            (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
            fill=fill,
        )
        text = str(label["label_num"])
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            (
                center_x - text_width / 2 - bbox[0],
                center_y - text_height / 2 - bbox[1],
            ),
            text,
            font=font,
            fill=text_fill,
        )
    return image


def _load_base_image(file):
    """读底图字节：优先缩略图（小），失败降级原图。"""
    from flask import current_app

    from app import oss
    from app.constants.file import ThumbnailStatus

    file_prefix = current_app.config["OSS_FILE_PREFIX"]
    if file.save_name:
        save_name_prefix = file.save_name.rsplit(".", 1)[0]
        if file.thumbnail_status == ThumbnailStatus.SUCCEEDED:
            try:
                buf = oss.download(file_prefix, f"resample-{save_name_prefix}.webp")
                return Image.open(io.BytesIO(buf.read()))
            except Exception:
                pass
    try:
        raw = file.download_real_file()
        return Image.open(io.BytesIO(raw.read()))
    except Exception:
        return None
