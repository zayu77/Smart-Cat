"""Report YOLO detection dataset image/label pairing and class counts."""

from __future__ import annotations

import argparse
import yaml
from collections import Counter
from pathlib import Path

from image_utils import is_image_path


# 加载并解析 data.yaml,返回原始字典
def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


# 把 data.yaml 里的 names 字段(字典或列表)统一转成按 id 排序的类别名列表
def names_from_yaml(data: dict) -> list[str]:
    names = data.get("names", [])
    if isinstance(names, dict):
        return [str(names[index]) for index in sorted(names, key=lambda key: int(key))]
    return [str(name) for name in names]


# 把 yaml 里的相对路径解析为绝对路径,相对路径则拼接在数据集根目录下
def resolve_split(root: Path, split_value: str) -> Path:
    path = Path(split_value)
    return path if path.is_absolute() else root / path


# 统计某个 split( train/val )下的图片/标签配对情况、缺标/空标/格式错误,并按类别统计 box 数量
def report_split(root: Path, split_name: str, image_dir: Path, names: list[str]) -> None:
    label_dir = root / "labels" / split_name
    images = sorted(path for path in image_dir.rglob("*") if path.is_file() and is_image_path(path))
    missing_labels = []
    empty_labels = []
    class_counts: Counter[int] = Counter()
    bad_lines = []

    for image_path in images:
        label_path = label_dir / image_path.relative_to(image_dir).with_suffix(".txt")
        if not label_path.exists():
            missing_labels.append(image_path)
            continue
        lines = [line.strip() for line in label_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            empty_labels.append(label_path)
            continue
        for line in lines:
            parts = line.split()
            if len(parts) != 5:
                bad_lines.append((label_path, line))
                continue
            try:
                class_id = int(parts[0])
                values = [float(value) for value in parts[1:]]
            except ValueError:
                bad_lines.append((label_path, line))
                continue
            if class_id < 0 or class_id >= len(names) or any(value < 0 or value > 1 for value in values):
                bad_lines.append((label_path, line))
                continue
            class_counts[class_id] += 1

    print(f"{split_name}: images={len(images)} missing_labels={len(missing_labels)} empty_labels={len(empty_labels)} bad_lines={len(bad_lines)}")
    for class_id, name in enumerate(names):
        print(f"  {class_id} {name}: boxes={class_counts[class_id]}")
    if missing_labels[:5]:
        print("  missing label examples:")
        for path in missing_labels[:5]:
            print(f"    {path}")
    if bad_lines[:5]:
        print("  bad label examples:")
        for path, line in bad_lines[:5]:
            print(f"    {path}: {line}")


# 解析命令行参数,加载 data.yaml,对 train/val 两个 split 各跑一次 report_split
def main() -> int:
    parser = argparse.ArgumentParser(description="Check a YOLO detection dataset.")
    parser.add_argument("--data", default="yolov11model-train/product_yolov11/data.yaml", help="YOLO detection dataset YAML.")
    args = parser.parse_args()

    data_path = Path(args.data)
    data = load_yaml(data_path)
    root = Path(data.get("path", data_path.parent))
    if not root.is_absolute():
        root = (data_path.parent / root).resolve()
    names = names_from_yaml(data)
    if not names:
        raise ValueError(f"No class names found in {data_path}")

    print(f"Dataset: {data_path}")
    print(f"Root: {root}")
    print(f"Classes: {', '.join(names)}")
    report_split(root, "train", resolve_split(root, str(data["train"])), names)
    report_split(root, "val", resolve_split(root, str(data["val"])), names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
