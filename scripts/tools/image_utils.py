"""Small image-file helpers used by dataset tools."""

from __future__ import annotations

from pathlib import Path

import cv2


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# 判断文件后缀是否在支持的图片扩展名集合内
def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


# 用 cv2 读图,失败(路径不存在/格式不支持)时抛 ValueError 而不是默默返回 None
def read_image(path: Path):
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"Could not read image: {path}")
    return image
