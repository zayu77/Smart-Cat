"""Export a trained YOLO detection model to ONNX and save class labels."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


# 兼容 Ultralytics 的两种 names 格式(字典/列表),统一按类别 id 顺序返回字符串列表
def labels_from_names(names: dict | list | tuple) -> list[str]:
    if isinstance(names, dict):
        return [str(names[index]) for index in sorted(names)]
    return [str(name) for name in names]


# 解析 CLI → Ultralytics 导出 ONNX → 复制到目标路径 → 落盘 labels JSON(names/imgsz/task/源模型)
def main() -> int:
    parser = argparse.ArgumentParser(description="Export YOLO detection .pt to ONNX.")
    parser.add_argument("--model", default="models/yolo_product_detector.pt", help="Input YOLO .pt model.")
    parser.add_argument("--output", default="models/yolo_product_detector.onnx", help="Output ONNX path.")
    parser.add_argument(
        "--labels-output",
        default="models/yolo_product_detector.labels.json",
        help="Output labels JSON path.",
    )
    parser.add_argument("--imgsz", type=int, default=640, help="Export image size.")
    parser.add_argument("--opset", type=int, default=12, help="ONNX opset version.")
    parser.add_argument("--simplify", action="store_true", help="Ask Ultralytics to simplify the exported ONNX.")
    parser.add_argument("--dynamic", action="store_true", help="Export with dynamic input shape.")
    args = parser.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("ultralytics is not installed. Run this on the PC: pip install ultralytics") from exc

    model_path = Path(args.model)
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    model = YOLO(str(model_path))
    labels = labels_from_names(model.names)
    exported = model.export(
        format="onnx",
        imgsz=args.imgsz,
        opset=args.opset,
        simplify=args.simplify,
        dynamic=args.dynamic,
    )
    exported_path = Path(exported[0] if isinstance(exported, (list, tuple)) else exported)
    if not exported_path.exists():
        raise FileNotFoundError(f"Ultralytics export finished, but ONNX was not found: {exported_path}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if exported_path.resolve() != output.resolve():
        shutil.copy2(exported_path, output)

    labels_output = Path(args.labels_output)
    labels_output.parent.mkdir(parents=True, exist_ok=True)
    with labels_output.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "names": labels,
                "imgsz": args.imgsz,
                "task": "detect",
                "source_model": str(model_path),
                "source_onnx": str(exported_path),
            },
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"ONNX exported by Ultralytics: {exported_path}")
    print(f"ONNX copied to: {output}")
    print(f"Labels saved to: {labels_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
