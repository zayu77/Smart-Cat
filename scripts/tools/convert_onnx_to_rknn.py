"""Convert an ONNX detection model to RKNN for Rockchip NPU runtime."""

from __future__ import annotations

import argparse
from pathlib import Path


# 通过 /proc/device-tree/compatible 自动检测当前 Rockchip 平台型号,非 Linux 开发机返回 None
def detect_target_platform() -> str | None:
    compatible = Path("/proc/device-tree/compatible")
    if not compatible.exists():
        return None
    text = compatible.read_bytes().replace(b"\x00", b"\n").decode("utf-8", errors="ignore").lower()
    for platform in ("rk3576", "rk3588", "rk3568", "rk3566", "rk3562"):
        if platform in text:
            return platform
    return None


# argparse 的 type 回调:把 "0,0,0" 这种三元素逗号串解析成 [float, float, float]
def parse_triplet(value: str) -> list[float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Expected three comma-separated numbers, for example: 0,0,0")
    return [float(part) for part in parts]


# 解析 CLI → 自动检测平台 → RKNN config/load_onnx/build/export_rknn;失败时逐阶段抛 RuntimeError
def main() -> int:
    parser = argparse.ArgumentParser(description="Convert ONNX to RKNN.")
    parser.add_argument("--onnx", default="models/yolo_product_detector.onnx", help="Input ONNX model.")
    parser.add_argument("--output", default="models/yolo_product_detector.rknn", help="Output RKNN model.")
    parser.add_argument(
        "--target-platform",
        default="auto",
        help="Rockchip target, for example rk3566/rk3568/rk3576/rk3588. Use auto on the board.",
    )
    parser.add_argument("--mean-values", type=parse_triplet, default=[0.0, 0.0, 0.0], help="Mean values, RGB order.")
    parser.add_argument("--std-values", type=parse_triplet, default=[255.0, 255.0, 255.0], help="Std values, RGB order.")
    parser.add_argument("--do-quantization", action="store_true", help="Build an int8 quantized RKNN model.")
    parser.add_argument("--dataset", default="yolov11model-train/rknn_quant_detector.txt", help="Quantization image list.")
    args = parser.parse_args()

    try:
        from rknn.api import RKNN
    except ImportError as exc:
        raise SystemExit(
            "rknn-toolkit2 is not installed. Run conversion in a Linux/WSL environment with RKNN Toolkit2."
        ) from exc

    onnx_path = Path(args.onnx)
    if not onnx_path.exists():
        raise FileNotFoundError(f"ONNX model not found: {onnx_path}")

    target_platform = args.target_platform
    if target_platform == "auto":
        detected = detect_target_platform()
        if detected is None:
            raise SystemExit("Could not auto-detect target platform. Pass --target-platform, such as rk3566 or rk3576.")
        target_platform = detected

    dataset = None
    if args.do_quantization:
        dataset_path = Path(args.dataset)
        if not dataset_path.exists():
            raise FileNotFoundError(f"Quantization dataset not found: {dataset_path}")
        dataset = str(dataset_path)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    rknn = RKNN(verbose=True)
    try:
        print(f"Config target platform: {target_platform}")
        ret = rknn.config(
            mean_values=[args.mean_values],
            std_values=[args.std_values],
            target_platform=target_platform,
        )
        if ret != 0:
            raise RuntimeError(f"RKNN config failed: {ret}")

        ret = rknn.load_onnx(model=str(onnx_path))
        if ret != 0:
            raise RuntimeError(f"RKNN load_onnx failed: {ret}")

        ret = rknn.build(do_quantization=args.do_quantization, dataset=dataset)
        if ret != 0:
            raise RuntimeError(f"RKNN build failed: {ret}")

        ret = rknn.export_rknn(str(output))
        if ret != 0:
            raise RuntimeError(f"RKNN export failed: {ret}")
    finally:
        rknn.release()

    print(f"RKNN model saved to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
