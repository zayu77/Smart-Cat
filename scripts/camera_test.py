"""跑一次，拍张照片存到 outputs/camera_test.jpg,偏向于测试摄像头"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2


# Linux 下摄像头既可用索引（0/1/...）也可用设备路径（/dev/video0），按需归一化
def parse_device(value: str) -> int | str:
    return int(value) if value.isdigit() else value


# CLI 入口：解析参数 → 打开摄像头 → 暖机若干秒 → 拍一帧 → 存盘 → 释放
def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one test image from a camera.")
    parser.add_argument("--device", default="0", help="Camera index or device path, e.g. 0 or /dev/video0.")
    parser.add_argument("--output", default="outputs/camera_test.jpg", help="Output image path.")
    parser.add_argument("--width", type=int, default=1920, help="Requested capture width.")
    parser.add_argument("--height", type=int, default=1080, help="Requested capture height.")
    parser.add_argument("--warmup", type=float, default=1.0, help="Seconds to wait before capturing.")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    camera = cv2.VideoCapture(parse_device(args.device))
    if not camera.isOpened():
        raise RuntimeError(f"Could not open camera: {args.device}")

    camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    deadline = time.time() + args.warmup
    frame = None
    while time.time() < deadline:
        ok, frame = camera.read()
        if not ok:
            frame = None
        time.sleep(0.05)

    ok, frame = camera.read()
    camera.release()
    if not ok or frame is None:
        raise RuntimeError("Failed to read a frame from the camera.")

    if not cv2.imwrite(str(output), frame):
        raise RuntimeError(f"Failed to save image: {output}")

    print(f"Saved camera image: {output}")
    print(f"Requested size: {args.width}x{args.height}")
    print(f"Actual frame shape: {frame.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
