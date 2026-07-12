"""Capture detection images and auto-generate YOLO box labels by background subtraction."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


DEFAULT_CLASSES = ["apple", "banana", "orange", "pear", "tomato"]


# 把 --device 字符串解析为整数索引或字符串路径
def parse_device(value: str) -> int | str:
    return int(value) if value.isdigit() else value


# 加载类别文件(JSON),支持多种结构;没传则用默认类别列表
def load_classes(path: Path | None) -> list[str]:
    if path is None:
        return DEFAULT_CLASSES
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if isinstance(data, dict):
        names = data.get("names", data)
        if isinstance(names, dict):
            return [str(names[index]) for index in sorted(names, key=lambda key: int(key))]
        if isinstance(names, list):
            return [str(name) for name in names]
        return [str(key) for key in data.keys()]
    if isinstance(data, list):
        return [str(name) for name in data]
    raise ValueError(f"Unsupported classes file format: {path}")


# 打开相机并设置分辨率,失败抛 RuntimeError
def open_camera(device: str, width: int, height: int):
    camera = cv2.VideoCapture(parse_device(device))
    if not camera.isOpened():
        raise RuntimeError(f"Could not open camera: {device}")
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return camera


# 相机预热:连续读帧若干秒,让自动曝光/白平衡稳定
def warmup_camera(camera, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        camera.read()
        time.sleep(0.03)


# 读一帧,失败抛 RuntimeError
def read_frame(camera):
    ok, frame = camera.read()
    if not ok or frame is None:
        raise RuntimeError("Failed to read camera frame.")
    return frame


# 单独跑一次:拍一张空秤背景图保存后退出(用作背景差分基图)
def save_background(args) -> int:
    path = Path(args.save_background)
    path.parent.mkdir(parents=True, exist_ok=True)
    camera = open_camera(args.device, args.width, args.height)
    try:
        warmup_camera(camera, args.warmup)
        frame = read_frame(camera)
        if not cv2.imwrite(str(path), frame):
            raise RuntimeError(f"Failed to save background: {path}")
    finally:
        camera.release()
    print(f"Saved empty-scale background: {path}")
    return 0


# 自动生成单张目标框:背景差分 → 灰度+高斯模糊 → 阈值分割 → 开闭运算 → 合并外接矩形 → 边界扩展;找不到前景返回 None
def auto_box(
    frame,
    background,
    threshold: int,
    min_area: int,
    pad_ratio: float,
) -> tuple[int, int, int, int] | None:
    if frame.shape != background.shape:
        background = cv2.resize(background, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_LINEAR)

    diff = cv2.absdiff(frame, background)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        boxes.append((x, y, x + w, y + h))

    if not boxes:
        return None

    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[2] for box in boxes)
    y2 = max(box[3] for box in boxes)

    width = frame.shape[1]
    height = frame.shape[0]
    pad = int(max(x2 - x1, y2 - y1) * pad_ratio)
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(width - 1, x2 + pad)
    y2 = min(height - 1, y2 + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


# 把像素坐标的 (x1, y1, x2, y2) 转换为 YOLO 格式:类别 + 归一化的中心点 + 宽高
def yolo_line(class_id: int, box: tuple[int, int, int, int], image_width: int, image_height: int) -> str:
    x1, y1, x2, y2 = box
    cx = ((x1 + x2) / 2.0) / image_width
    cy = ((y1 + y2) / 2.0) / image_height
    bw = (x2 - x1) / image_width
    bh = (y2 - y1) / image_height
    return f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


# 在原图上画矩形和类别名,返回标注后的预览图(box 为 None 时只返回原图副本)
def draw_preview(frame, box: tuple[int, int, int, int] | None, label: str):
    preview = frame.copy()
    if box is not None:
        x1, y1, x2, y2 = box
        cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 255, 0), 3)
        cv2.putText(preview, label, (x1, max(24, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    return preview


# 主采集流程:循环拍 --count 张图,每张用 auto_box 自动标框,落盘到 images/labels/(可选)previews
def capture_dataset(args) -> int:
    classes = load_classes(Path(args.classes) if args.classes else None)
    if args.class_name not in classes:
        raise ValueError(f"Unknown class '{args.class_name}'. Available: {', '.join(classes)}")
    class_id = classes.index(args.class_name)

    background_path = Path(args.background)
    background = cv2.imread(str(background_path))
    if background is None:
        raise FileNotFoundError(f"Could not read background image: {background_path}")

    output_root = Path(args.output_root)
    images_dir = output_root / "images" / args.class_name
    labels_dir = output_root / "labels" / args.class_name
    previews_dir = output_root / "previews" / args.class_name
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)
    if args.save_preview:
        previews_dir.mkdir(parents=True, exist_ok=True)

    camera = open_camera(args.device, args.width, args.height)
    saved = 0
    skipped = 0
    try:
        warmup_camera(camera, args.warmup)
        print(f"Capturing detection dataset for class: {args.class_name} (id={class_id})")
        print(f"Output: {output_root}")
        print("Move or rotate the product between captures. Keep only one target product in view.")

        for index in range(args.count):
            if args.manual:
                input(f"[{index + 1}/{args.count}] Adjust product, then press Enter to capture...")
            else:
                time.sleep(args.interval)
            frame = read_frame(camera)
            box = auto_box(
                frame,
                background,
                threshold=args.threshold,
                min_area=args.min_area,
                pad_ratio=args.pad_ratio,
            )
            if box is None:
                skipped += 1
                print(f"[{index + 1}/{args.count}] skipped: no foreground box")
                continue

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            stem = f"{args.class_name}_{stamp}"
            image_path = images_dir / f"{stem}.jpg"
            label_path = labels_dir / f"{stem}.txt"
            preview_path = previews_dir / f"{stem}.jpg"

            if not cv2.imwrite(str(image_path), frame):
                skipped += 1
                print(f"[{index + 1}/{args.count}] skipped: failed to save {image_path}")
                continue
            label_path.write_text(
                yolo_line(class_id, box, frame.shape[1], frame.shape[0]) + "\n",
                encoding="utf-8",
            )
            if args.save_preview:
                cv2.imwrite(str(preview_path), draw_preview(frame, box, args.class_name))

            saved += 1
            print(f"[{saved}/{args.count}] saved {image_path.name} box={box}")
    finally:
        camera.release()

    print(f"Done. saved={saved}, skipped={skipped}")
    return 0


# 解析 CLI 参数;传了 --save-background 就只拍背景图退出,否则进入 capture_dataset 正常采集
def main() -> int:
    parser = argparse.ArgumentParser(description="Capture detection images and auto-label one product box.")
    parser.add_argument("--class-name", required=False, help="Dataset class name, e.g. apple.")
    parser.add_argument("--classes", default="", help="Optional JSON labels/classes file. Defaults to project product classes.")
    parser.add_argument("--device", default="0", help="Camera index or device path.")
    parser.add_argument("--count", type=int, default=50, help="Number of labeled images to capture.")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between captures.")
    parser.add_argument("--output-root", default="yolov11model-train/dataset_det", help="Detection dataset output root.")
    parser.add_argument("--width", type=int, default=1920, help="Requested capture width.")
    parser.add_argument("--height", type=int, default=1080, help="Requested capture height.")
    parser.add_argument("--warmup", type=float, default=1.0, help="Camera warmup seconds.")
    parser.add_argument("--background", default="outputs/empty_scale_background.jpg", help="Empty-scale background image.")
    parser.add_argument("--save-background", default="", help="Capture an empty-scale background image to this path and exit.")
    parser.add_argument("--threshold", type=int, default=35, help="Foreground threshold for background subtraction.")
    parser.add_argument("--min-area", type=int, default=2500, help="Minimum contour area kept as foreground.")
    parser.add_argument("--pad-ratio", type=float, default=0.06, help="Padding around generated box.")
    parser.add_argument("--save-preview", action="store_true", help="Save preview images with generated boxes.")
    parser.add_argument("--manual", action="store_true", help="Press Enter to capture each image instead of using --interval.")
    args = parser.parse_args()

    if args.save_background:
        return save_background(args)
    if not args.class_name:
        raise SystemExit("--class-name is required unless --save-background is used")
    return capture_dataset(args)


if __name__ == "__main__":
    raise SystemExit(main())
