"""Train an Ultralytics YOLO detection model for product detection."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


# 优先用 Ultralytics 返回的 save_dir,再回退到 project/name,最后全局搜最近修改的 best.pt
def find_best_model(project: Path, name: str, train_result: object | None = None) -> Path:
    save_dir = getattr(train_result, "save_dir", None)
    if save_dir is not None:
        candidate = Path(save_dir) / "weights" / "best.pt"
        if candidate.exists():
            return candidate

    direct = project / name / "weights" / "best.pt"
    if direct.exists():
        return direct

    matches = sorted(project.rglob("weights/best.pt"), key=lambda path: path.stat().st_mtime, reverse=True)
    if matches:
        return matches[0]

    raise FileNotFoundError(f"Training finished, but best.pt was not found under {project}")


# 把 Ultralytics 风格的 data.yaml 拷贝成 .ultralytics.yaml 并把 path 解析为绝对路径,避免训练时找不到数据
def prepare_ultralytics_data_yaml(data_yaml: Path) -> Path:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is not installed. Run: pip install PyYAML") from exc

    with data_yaml.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Dataset YAML must be an object: {data_yaml}")

    raw_root = Path(str(data.get("path", ".")))
    root = raw_root if raw_root.is_absolute() else (data_yaml.parent / raw_root)
    data["path"] = str(root.resolve()).replace("\\", "/")

    output = data_yaml.with_name(f"{data_yaml.stem}.ultralytics.yaml")
    with output.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
    return output


# 解析 CLI → 准备 data.yaml → Ultralytics train → 找 best.pt → 拷贝到 --output 路径
def main() -> int:
    parser = argparse.ArgumentParser(description="Train a YOLO detection model.")
    parser.add_argument("--data", default="yolov11model-train/product_yolov11/data.yaml", help="YOLO detection dataset YAML.")
    parser.add_argument("--model", default="yolo11n.pt", help="Pretrained YOLO detection model.")
    parser.add_argument("--epochs", type=int, default=80, help="Training epochs.")
    parser.add_argument("--imgsz", type=int, default=640, help="Training image size.")
    parser.add_argument("--batch", type=int, default=16, help="Batch size.")
    parser.add_argument("--device", default=None, help="Training device, e.g. cpu, 0, cuda:0.")
    parser.add_argument("--project", default="runs/detect", help="Ultralytics output project folder.")
    parser.add_argument("--name", default="product_yolo11_det", help="Ultralytics run name.")
    parser.add_argument("--output", default="models/yolo_product_detector.pt", help="Copied best model path.")
    args = parser.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("ultralytics is not installed. Run: pip install ultralytics") from exc

    data_yaml = Path(args.data)
    if not data_yaml.exists():
        raise FileNotFoundError(f"Dataset YAML not found: {data_yaml}")
    train_data_yaml = prepare_ultralytics_data_yaml(data_yaml)

    model = YOLO(args.model)
    train_kwargs = {
        "data": str(train_data_yaml),
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "project": args.project,
        "name": args.name,
        "exist_ok": True,
    }
    if args.device is not None:
        train_kwargs["device"] = args.device

    train_result = model.train(**train_kwargs)
    best_model = find_best_model(Path(args.project), args.name, train_result)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best_model, output)
    print(f"Best model found at: {best_model}")
    print(f"Best model copied to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
