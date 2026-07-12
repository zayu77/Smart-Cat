"""Project smoke checks for Smart-Cat.

This script is intended to support test planning and regression reporting.
It validates core pure-Python helpers by default, and can optionally probe
camera / HX711 / RKNN paths when the corresponding hardware is available.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


from device_policy import apply_policy_to_record, load_policy, normalize_policy, calculate_policy_price, render_template
from mqtt_publisher import (
    derive_device_event_topic,
    derive_device_policy_topic,
    derive_runtime_config_topic,
    derive_voice_command_topic,
    load_mqtt_config,
)
from product_business import build_status, format_money, load_products
from transaction_utils import append_jsonl, build_transaction


@dataclass
class CheckResult:
    name: str
    status: str
    message: str = ""
    duration_ms: float = 0.0
    details: dict[str, Any] | None = None


def _ok(name: str, message: str = "", start: float | None = None, details: dict[str, Any] | None = None) -> CheckResult:
    duration_ms = (time.perf_counter() - start) * 1000.0 if start is not None else 0.0
    return CheckResult(name=name, status="pass", message=message, duration_ms=duration_ms, details=details)


def _skip(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status="skip", message=message)


def _fail(name: str, message: str, start: float | None = None) -> CheckResult:
    duration_ms = (time.perf_counter() - start) * 1000.0 if start is not None else 0.0
    return CheckResult(name=name, status="fail", message=message, duration_ms=duration_ms)


def _check(name: str, func: Callable[[], dict[str, Any] | None]) -> CheckResult:
    start = time.perf_counter()
    try:
        details = func()
        message = "ok"
        return _ok(name, message, start, details)
    except AssertionError as exc:
        return _fail(name, f"assertion failed: {exc}", start)
    except Exception as exc:
        return _fail(name, f"{type(exc).__name__}: {exc}", start)


def check_core_config() -> dict[str, Any]:
    mqtt_config = load_mqtt_config(ROOT / "config/mqtt.json")
    runtime_config_path = ROOT / "config/device_runtime.json"
    policy_path = ROOT / "config/device_policy.json"
    products_path = ROOT / "config/products.json"

    assert isinstance(mqtt_config, dict)
    assert mqtt_config["host"]
    assert mqtt_config["topic"]
    assert runtime_config_path.exists(), "config/device_runtime.json missing"
    assert policy_path.exists(), "config/device_policy.json missing"
    assert products_path.exists(), "config/products.json missing"

    products = load_products(products_path)
    policy = load_policy(policy_path)
    normalized = normalize_policy(policy)

    return {
        "mqtt_topic": mqtt_config["topic"],
        "runtime_config_topic": derive_runtime_config_topic(mqtt_config),
        "device_policy_topic": derive_device_policy_topic(mqtt_config),
        "device_event_topic": derive_device_event_topic(mqtt_config),
        "voice_command_topic": derive_voice_command_topic(mqtt_config),
        "product_count": len(products),
        "policy_version": normalized["policy_version"],
    }


def check_business_rules() -> dict[str, Any]:
    assert build_status([], 0.75, 0.15) == "unknown"
    assert build_status([("apple", 0.74)], 0.75, 0.15) == "low_confidence"
    assert build_status([("apple", 0.91), ("banana", 0.80)], 0.75, 0.15) == "needs_confirm"
    assert build_status([("apple", 0.91), ("banana", 0.70)], 0.75, 0.15) == "accepted"
    assert format_money(5.0) == "5"
    assert format_money(5.50) == "5.5"

    total, pricing = calculate_policy_price(1000, 10.0, "斤", "discount_10_over_1000g")
    assert total == 18.0
    assert pricing["discount"] == 2.0

    record = {
        "product_id": "apple",
        "product_name": "苹果",
        "weight_g": 500,
        "unit": "斤",
        "unit_price": 9.9,
        "total_price": 9.9,
        "confidence": 0.94,
    }
    voice_text = render_template("{product_name} {weight_g}g {unit_price} {total_price}", record)
    assert "苹果" in voice_text
    assert "500" in voice_text

    return {
        "status_cases": ["unknown", "low_confidence", "needs_confirm", "accepted"],
        "discount_price": total,
        "voice_text": voice_text,
    }


def check_transaction_helpers() -> dict[str, Any]:
    recognition = {
        "status": "accepted",
        "product_id": "apple",
        "product_name": "苹果",
        "confidence": 0.93,
        "confidence_gap": 0.21,
        "weight_g": 500.0,
        "unit": "斤",
        "unit_price": 9.9,
        "total_price": 9.9,
        "voice_text": "苹果，净重500克，单价9.9元每斤，总价9.9元。",
        "top_predictions": [{"product_id": "apple", "confidence": 0.93}],
        "detections": [{"product_id": "apple", "confidence": 0.93, "bbox_xyxy": [1, 2, 3, 4]}],
        "detector": {"has_box": True, "raw_detection_count": 1},
    }
    record = build_transaction(recognition, image_path=Path("outputs/current.jpg"), device_id="demo-device")
    assert record["transaction_id"]
    assert record["status"] == "accepted"
    assert record["product_id"] == "apple"

    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "records.jsonl"
        append_jsonl(target, record)
        content = target.read_text(encoding="utf-8").strip()
        assert content

    return {
        "transaction_id_prefix": record["transaction_id"][:8],
        "record_keys": sorted(record.keys()),
    }


def check_policy_application() -> dict[str, Any]:
    base_record = {
        "status": "accepted",
        "product_id": "apple",
        "product_name": "苹果",
        "weight_g": 500.0,
        "unit": "斤",
        "unit_price": 9.9,
        "total_price": 9.9,
        "voice_text": "苹果，净重500克，单价9.9元每斤，总价9.9元。",
    }
    policy = load_policy(ROOT / "config/device_policy.json")
    applied = apply_policy_to_record(dict(base_record), policy)
    assert applied["policy"]["policy_version"]
    assert applied["voice_text"]

    low_conf = dict(base_record)
    low_conf["status"] = "low_confidence"
    low_applied = apply_policy_to_record(low_conf, policy)
    assert low_applied["status"] in {"needs_confirm", "low_confidence", "rejected"}

    return {
        "selected_action": applied["policy"]["selected_action"],
        "pricing_mode": applied["policy"]["pricing_mode"],
        "voice_text": applied["voice_text"],
    }


def check_optional_camera(device: str, width: int, height: int, warmup: float, output: Path) -> dict[str, Any]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(f"OpenCV unavailable: {exc}") from exc

    from camera_test import parse_device

    camera = cv2.VideoCapture(parse_device(device))
    if not camera.isOpened():
        raise RuntimeError(f"Could not open camera: {device}")
    try:
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        deadline = time.time() + max(0.0, warmup)
        frame = None
        while time.time() < deadline:
            ok, frame = camera.read()
            if not ok:
                frame = None
            time.sleep(0.05)
        ok, frame = camera.read()
        if not ok or frame is None:
            raise RuntimeError("Failed to read a frame from the camera.")
        output.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(output), frame):
            raise RuntimeError(f"Failed to save image: {output}")
        return {"output": str(output), "frame_shape": list(frame.shape)}
    finally:
        camera.release()


def check_optional_hx711(samples: int, max_deviation: float, gpio_backend: str) -> dict[str, Any]:
    from hx711_reader import HX711, filtered_raw, grams_from_raw, load_config

    config = load_config(ROOT / "config/hx711_scale.json", dout_gpio=134, sck_gpio=132)
    hx711 = HX711(
        config.dout_gpio,
        config.sck_gpio,
        backend=gpio_backend,
        dout_chip=config.dout_chip,
        dout_line=config.dout_line,
        sck_chip=config.sck_chip,
        sck_line=config.sck_line,
    )
    values = hx711.read_values(samples=samples)
    raw = filtered_raw(values, max_deviation=max_deviation)
    grams = grams_from_raw(raw, config)
    return {"samples": len(values), "raw_filtered": round(raw, 2), "weight_g": round(grams, 2)}


def check_optional_rknn(image: Path, model: Path, labels: Path, preview: Path | None) -> dict[str, Any]:
    from predict_rknn_detector import RKNNDetector, save_preview

    if not image.exists():
        raise FileNotFoundError(f"Input image not found: {image}")
    if not model.exists():
        raise FileNotFoundError(f"RKNN model not found: {model}")
    if not labels.exists():
        raise FileNotFoundError(f"Labels file not found: {labels}")

    start = time.perf_counter()
    with RKNNDetector(model_path=model, labels_path=labels) as detector:
        detections = detector.predict_image(image)
        if preview is not None:
            preview.parent.mkdir(parents=True, exist_ok=True)
            save_preview(image, detections, preview)
    duration_ms = (time.perf_counter() - start) * 1000.0
    return {
        "image": str(image),
        "model": str(model),
        "detections": len(detections),
        "duration_ms": round(duration_ms, 2),
        "preview": str(preview) if preview is not None else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Smart-Cat smoke checks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--output", default="outputs/test_smoke_report.json", help="JSON report output path.")
    parser.add_argument("--check-camera", action="store_true", help="Run an optional camera capture smoke check.")
    parser.add_argument("--camera-device", default="0", help="Camera device index or path.")
    parser.add_argument("--camera-width", type=int, default=1920, help="Requested camera width.")
    parser.add_argument("--camera-height", type=int, default=1080, help="Requested camera height.")
    parser.add_argument("--camera-warmup", type=float, default=1.0, help="Camera warmup seconds.")
    parser.add_argument("--camera-output", default="outputs/test_smoke_camera.jpg", help="Camera capture output path.")
    parser.add_argument("--check-hx711", action="store_true", help="Run an optional HX711 smoke check.")
    parser.add_argument("--hx711-samples", type=int, default=5, help="HX711 sample count.")
    parser.add_argument("--hx711-max-deviation", type=float, default=5000.0, help="HX711 raw deviation filter.")
    parser.add_argument("--gpio-backend", choices=["gpiod", "sysfs"], default="gpiod", help="GPIO backend for HX711.")
    parser.add_argument("--check-rknn", action="store_true", help="Run an optional RKNN inference smoke check.")
    parser.add_argument("--rknn-image", default="outputs/current.jpg", help="Input image for RKNN smoke check.")
    parser.add_argument("--rknn-model", default="models/yolo_product_detector.rknn", help="RKNN model path.")
    parser.add_argument("--rknn-labels", default="models/yolo_product_detector.labels.json", help="RKNN labels path.")
    parser.add_argument("--rknn-preview", default="", help="Optional preview output path.")
    args = parser.parse_args()

    checks: list[CheckResult] = []
    checks.append(_check("core-config", check_core_config))
    checks.append(_check("business-rules", check_business_rules))
    checks.append(_check("transaction-helpers", check_transaction_helpers))
    checks.append(_check("policy-application", check_policy_application))

    if args.check_camera:
        start = time.perf_counter()
        try:
            details = check_optional_camera(
                device=args.camera_device,
                width=args.camera_width,
                height=args.camera_height,
                warmup=args.camera_warmup,
                output=Path(args.camera_output),
            )
            checks.append(_ok("camera-smoke", "ok", start, details))
        except Exception as exc:
            checks.append(_fail("camera-smoke", f"{type(exc).__name__}: {exc}", start))
    else:
        checks.append(_skip("camera-smoke", "not requested"))

    if args.check_hx711:
        start = time.perf_counter()
        try:
            details = check_optional_hx711(
                samples=args.hx711_samples,
                max_deviation=args.hx711_max_deviation,
                gpio_backend=args.gpio_backend,
            )
            checks.append(_ok("hx711-smoke", "ok", start, details))
        except Exception as exc:
            checks.append(_fail("hx711-smoke", f"{type(exc).__name__}: {exc}", start))
    else:
        checks.append(_skip("hx711-smoke", "not requested"))

    if args.check_rknn:
        start = time.perf_counter()
        try:
            preview = Path(args.rknn_preview) if args.rknn_preview else None
            details = check_optional_rknn(
                image=Path(args.rknn_image),
                model=Path(args.rknn_model),
                labels=Path(args.rknn_labels),
                preview=preview,
            )
            checks.append(_ok("rknn-smoke", "ok", start, details))
        except Exception as exc:
            checks.append(_fail("rknn-smoke", f"{type(exc).__name__}: {exc}", start))
    else:
        checks.append(_skip("rknn-smoke", "not requested"))

    summary = {
        "root": str(ROOT),
        "passed": sum(1 for item in checks if item.status == "pass"),
        "failed": sum(1 for item in checks if item.status == "fail"),
        "skipped": sum(1 for item in checks if item.status == "skip"),
        "checks": [asdict(item) for item in checks],
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("Smart-Cat smoke checks")
        print(f"Passed: {summary['passed']}, Failed: {summary['failed']}, Skipped: {summary['skipped']}")
        for item in checks:
            prefix = "PASS" if item.status == "pass" else "FAIL" if item.status == "fail" else "SKIP"
            line = f"[{prefix}] {item.name}"
            if item.message:
                line += f" - {item.message}"
            if item.duration_ms:
                line += f" ({item.duration_ms:.1f} ms)"
            print(line)
        print(f"Report written to: {output}")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
