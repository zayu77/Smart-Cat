"""Create an RKNN quantization dataset text file from product images."""

from __future__ import annotations

import argparse
import random
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# 递归收集目录下所有后缀在 IMAGE_EXTENSIONS 内的图片,按路径排序保证结果可复现
def find_images(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.suffix.lower() in IMAGE_EXTENSIONS)


# 解析 CLI → 找图 → 随机洗牌 → 取前 --limit 张 → 写出一行一个路径的文本清单(RKNN 量化用)
def main() -> int:
    parser = argparse.ArgumentParser(description="Create image list for RKNN quantization.")
    parser.add_argument("--source", default="yolov11model-train/product_yolov11/images/train", help="Image folder.")
    parser.add_argument("--output", default="yolov11model-train/rknn_quant_detector.txt", help="Output text file.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of images.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    args = parser.parse_args()

    source = Path(args.source)
    images = find_images(source)
    if not images:
        raise FileNotFoundError(f"No images found under {source}")

    random.seed(args.seed)
    random.shuffle(images)
    selected = images[: args.limit]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for image in selected:
            file.write(str(image.as_posix()) + "\n")

    print(f"Images found: {len(images)}")
    print(f"Images written: {len(selected)}")
    print(f"Dataset file: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
