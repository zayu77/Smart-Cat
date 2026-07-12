"""End-to-end smart scale demo: camera + HX711 + recognition + checkout record."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from camera_test import parse_device
from transaction_utils import append_jsonl, build_transaction, print_receipt
from hx711_reader import HX711, filtered_raw, grams_from_raw, load_config
from mqtt_publisher import load_mqtt_config, publish_device_event, publish_record
from predict_rknn_detector import save_preview
from recognize_product_detector_rknn import recognize_product_detector_rknn
from tts_output import speak_text
from device_policy import apply_policy_to_record, append_device_event, build_device_event, load_policy


# 加载运行时配置文件,文件不存在返回空 dict(允许首次启动时无配置)
def load_runtime_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Runtime config must be a JSON object: {path}")
    return data


# 安全的嵌套 dict 取值:沿 path 逐层下钻,任一层不是 dict 或 key 不存在就返回 default
def nested_get(data: dict, path: tuple[str, ...], default):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


# 从 labels JSON 里读 imgsz 字段;文件缺失、解析失败、字段不是整数都回退到 default
def labels_imgsz(path: Path, default: int) -> int:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return default
    try:
        return int(data.get("imgsz", default))
    except (TypeError, ValueError):
        return default


# 把嵌套的 runtime config 展平为一组带类型的默认值,供 build_arg_parser 直接当 default 用
def runtime_defaults(config: dict) -> dict:
    return {
        "device": str(nested_get(config, ("camera", "device"), "0")),
        "image_output": str(nested_get(config, ("camera", "image_output"), "outputs/current.jpg")),
        "width": int(nested_get(config, ("camera", "width"), 1920)),
        "height": int(nested_get(config, ("camera", "height"), 1080)),
        "warmup": float(nested_get(config, ("camera", "warmup"), 1.0)),
        "backend": "rknn-det",
        "hx711_config": str(nested_get(config, ("weight", "hx711_config"), "config/hx711_scale.json")),
        "gpio_backend": str(nested_get(config, ("weight", "gpio_backend"), "gpiod")),
        "samples": int(nested_get(config, ("weight", "samples"), 20)),
        "max_deviation": float(nested_get(config, ("weight", "max_deviation"), 5000.0)),
        "device_id": str(config.get("device_id", "lubancat3_demo_001")),
        "accept_confidence": float(nested_get(config, ("recognition", "accept_confidence"), 0.75)),
        "confirm_gap": float(nested_get(config, ("recognition", "confirm_gap"), 0.15)),
        "topk": int(nested_get(config, ("recognition", "topk"), 3)),
        "rknn_imgsz": int(nested_get(config, ("recognition", "rknn_imgsz"), 640)),
        "rknn_layout": str(nested_get(config, ("recognition", "rknn_layout"), "nhwc")),
        "rknn_float_input": bool(nested_get(config, ("recognition", "rknn_float_input"), False)),
        "det_conf": float(nested_get(config, ("recognition", "det_conf"), 0.25)),
        "det_iou": float(nested_get(config, ("recognition", "det_iou"), 0.45)),
        "det_max": int(nested_get(config, ("recognition", "det_max"), 20)),
        "det_score_sigmoid": bool(nested_get(config, ("recognition", "det_score_sigmoid"), False)),
        "tts": str(nested_get(config, ("tts", "backend"), "none")),
        "tts_port": str(nested_get(config, ("tts", "port"), "/dev/ttyS1")),
        "tts_baudrate": int(nested_get(config, ("tts", "baudrate"), 9600)),
        "tts_encoding": str(nested_get(config, ("tts", "encoding"), "gb2312")),
        "tts_music": int(nested_get(config, ("tts", "music"), 0)),
        "tts_volume": int(nested_get(config, ("tts", "volume"), 4)),
        "tts_music_volume": int(nested_get(config, ("tts", "music_volume"), 0)),
        "tts_speed": int(nested_get(config, ("tts", "speed"), 5)),
        "mqtt": bool(nested_get(config, ("mqtt", "enabled"), False)),
        "mqtt_config": str(nested_get(config, ("mqtt", "config"), "config/mqtt.json")),
        "mqtt_optional": bool(nested_get(config, ("mqtt", "optional"), False)),
    }


# 相机采集的薄封装:开 → 排空 V4L2 buffer → 读一帧 → 落盘 → 关,with 语法自动释放
class CameraCapture:
    FRESH_CAPTURE_SECONDS = 0.35
    FRESH_CAPTURE_MIN_READS = 8

    # 打开相机并设置 buffer=1(避免拿到陈旧帧)+ 目标分辨率;warmed 标记用于只预热一次
    def __init__(self, device: str, width: int, height: int) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.camera = cv2.VideoCapture(parse_device(device))
        if not self.camera.isOpened():
            raise RuntimeError(f"Could not open camera: {device}")
        self.camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.warmed = False

    # 排空 V4L2 内核缓冲:连续读帧直到 min_reads 帧和 seconds 截止时间两个条件都满足,确保下一帧是最新的
    def drain_buffer(self, seconds: float, min_reads: int) -> None:
        deadline = time.time() + max(0.0, seconds)
        reads = 0
        while reads < min_reads or time.time() < deadline:
            self.camera.read()
            reads += 1
            time.sleep(0.03)

    # 抓拍一张图:首次调用按 warmup 长时间排空(让自动曝光稳定),之后按短时长快速排空
    def capture(self, output: Path, warmup: float) -> tuple[int, int, int]:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not self.warmed and warmup > 0:
            self.drain_buffer(seconds=warmup, min_reads=self.FRESH_CAPTURE_MIN_READS)
            self.warmed = True
        else:
            self.drain_buffer(seconds=self.FRESH_CAPTURE_SECONDS, min_reads=self.FRESH_CAPTURE_MIN_READS)

        ok, frame = self.camera.read()
        if not ok or frame is None:
            raise RuntimeError("Failed to capture image from camera.")
        if not cv2.imwrite(str(output), frame):
            raise RuntimeError(f"Failed to save image: {output}")
        return frame.shape

    # 释放相机设备
    def close(self) -> None:
        self.camera.release()

    # 上下文管理器入口:返回自身以便 with 语句使用
    def __enter__(self):
        return self

    # 上下文管理器退出:无论是否异常都释放相机
    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


# capture_image:用 with 语法包一层 CameraCapture 的快捷函数,处理完自动释放
def capture_image(device: str, output: Path, width: int, height: int, warmup: float) -> tuple[int, int, int]:
    with CameraCapture(device, width, height) as camera:
        return camera.capture(output, warmup)


# 读一次 HX711 重量:加载配置 → 初始化 HX711 → 采 samples 个原始值 → 中值滤波 → 转换成克
def read_hx711_weight(config_path: Path, samples: int, max_deviation: float, gpio_backend: str) -> tuple[float, float, list[int]]:
    config = load_config(config_path, dout_gpio=134, sck_gpio=132)
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
    return raw, grams_from_raw(raw, config), values


# 构造完整 CLI parser:从 defaults 拿各参数默认值,backend 强制 rknn-det
def build_arg_parser(defaults: dict, pre_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one complete smart scale transaction.", parents=[pre_parser])
    parser.set_defaults(backend="rknn-det")
    parser.add_argument("--device", default=defaults["device"], help="Camera index or device path.")
    parser.add_argument("--image-output", default=defaults["image_output"], help="Captured image path.")
    parser.add_argument("--width", type=int, default=defaults["width"], help="Camera capture width.")
    parser.add_argument("--height", type=int, default=defaults["height"], help="Camera capture height.")
    parser.add_argument("--warmup", type=float, default=defaults["warmup"], help="Camera warmup seconds.")
    parser.add_argument("--model", default=None, help="RKNN detector model path.")
    parser.add_argument("--labels", default=None, help="Labels JSON path for RKNN.")
    parser.add_argument("--products", default="config/products.json", help="Product table JSON path.")
    parser.add_argument("--hx711-config", default=defaults["hx711_config"], help="HX711 calibration config path.")
    parser.add_argument("--gpio-backend", choices=["gpiod", "sysfs"], default=defaults["gpio_backend"], help="GPIO backend.")
    parser.add_argument("--samples", type=int, default=defaults["samples"], help="HX711 samples per transaction.")
    parser.add_argument("--max-deviation", type=float, default=defaults["max_deviation"], help="Raw-count deviation used to filter outliers.")
    parser.add_argument("--weight-g", type=float, default=None, help="Use this measured weight and skip HX711 reading.")
    parser.add_argument("--weight-raw", type=float, default=None, help="Raw HX711 value paired with --weight-g.")
    parser.add_argument("--weight-source", default="hx711", help="Weight source label written to transaction records.")
    parser.add_argument("--records", default="records/transactions.jsonl", help="Transaction JSONL path.")
    parser.add_argument("--device-id", default=defaults["device_id"], help="Device ID written to transaction records.")
    parser.add_argument("--accept-confidence", type=float, default=defaults["accept_confidence"], help="Minimum confidence for auto accept.")
    parser.add_argument("--confirm-gap", type=float, default=defaults["confirm_gap"], help="Ask confirmation if Top1-Top2 gap is smaller.")
    parser.add_argument("--topk", type=int, default=defaults["topk"], help="Number of predictions to include.")
    parser.add_argument("--rknn-imgsz", type=int, default=defaults["rknn_imgsz"], help="RKNN input image size.")
    parser.add_argument("--rknn-layout", choices=["nhwc", "nchw"], default=defaults["rknn_layout"], help="RKNN input layout.")
    parser.add_argument("--rknn-float-input", action="store_true", default=defaults["rknn_float_input"], help="Send float32 0-1 input to RKNN.")
    parser.add_argument("--det-conf", type=float, default=defaults["det_conf"], help="Detection confidence threshold for rknn-det.")
    parser.add_argument("--det-iou", type=float, default=defaults["det_iou"], help="Detection NMS IoU threshold for rknn-det.")
    parser.add_argument("--det-max", type=int, default=defaults["det_max"], help="Maximum detections for rknn-det.")
    parser.add_argument("--det-score-sigmoid", action="store_true", default=defaults["det_score_sigmoid"], help="Apply sigmoid to detector scores.")
    parser.add_argument("--tts", choices=["none", "mock", "syn6288"], default=defaults["tts"], help="TTS output backend.")
    parser.add_argument("--tts-port", default=defaults["tts_port"], help="UART device path for SYN6288.")
    parser.add_argument("--tts-baudrate", type=int, default=defaults["tts_baudrate"], help="UART baudrate for SYN6288.")
    parser.add_argument("--tts-encoding", choices=["gb2312", "gbk", "big5", "unicode"], default=defaults["tts_encoding"], help="SYN6288 text encoding.")
    parser.add_argument("--tts-music", type=int, default=defaults["tts_music"], help="SYN6288 background music index, 0 disables music.")
    parser.add_argument("--tts-volume", type=int, default=defaults["tts_volume"], help="SYN6288 speech volume, 0-16.")
    parser.add_argument("--tts-music-volume", type=int, default=defaults["tts_music_volume"], help="SYN6288 background music volume, 0-16.")
    parser.add_argument("--tts-speed", type=int, default=defaults["tts_speed"], help="SYN6288 speech speed, 0-5.")
    parser.add_argument("--mqtt", action="store_true", default=defaults["mqtt"], help="Publish the transaction record to MQTT.")
    parser.add_argument("--mqtt-config", default=defaults["mqtt_config"], help="MQTT config JSON path.")
    parser.add_argument("--mqtt-optional", action="store_true", default=defaults["mqtt_optional"], help="Do not fail the transaction if MQTT publish fails.")
    parser.add_argument("--no-record", action="store_true", help="Do not append to transaction record file.")
    parser.add_argument("--json", action="store_true", help="Print transaction JSON only.")
    return parser


# 两阶段参数解析:先解析 --runtime-config/--device-policy 拿到默认值,再构造完整 parser 解析剩余参数
def parse_transaction_args(argv: list[str] | None = None):
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--runtime-config", default="config/device_runtime.json", help="Runtime config JSON path.")
    pre_parser.add_argument("--device-policy", default="config/device_policy.json", help="Device policy JSON path.")
    pre_args, _ = pre_parser.parse_known_args(argv)
    runtime_path = Path(pre_args.runtime_config)
    runtime_config = load_runtime_config(runtime_path)
    policy_path = Path(pre_args.device_policy)
    device_policy = load_policy(policy_path)
    defaults = runtime_defaults(runtime_config)
    parser = build_arg_parser(defaults, pre_parser)
    args = parser.parse_args(argv)
    if args.labels is None:
        args.rknn_imgsz = labels_imgsz(Path("models/yolo_product_detector.labels.json"), args.rknn_imgsz)
    return args, runtime_path, runtime_config, policy_path, device_policy


# 单次完整交易:抓图 → 称重 → RKNN 识别 → build_transaction → 应用策略 → TTS → 落盘 → MQTT 广播
def run_transaction(
    args,
    runtime_path: Path,
    runtime_config: dict,
    policy_path: Path,
    device_policy: dict,
    rknn_predictor=None,
    camera_capture: CameraCapture | None = None,
) -> dict:

    image_path = Path(args.image_output)
    if camera_capture is None:
        frame_shape = capture_image(
            device=args.device,
            output=image_path,
            width=args.width,
            height=args.height,
            warmup=args.warmup,
        )
    else:
        frame_shape = camera_capture.capture(image_path, args.warmup)

    if args.weight_g is None:
        raw, weight_g, raw_values = read_hx711_weight(
            config_path=Path(args.hx711_config),
            samples=args.samples,
            max_deviation=args.max_deviation,
            gpio_backend=args.gpio_backend,
        )
        weight_source = {
            "type": args.weight_source,
            "raw_filtered": round(raw, 2),
            "raw_mean": round(sum(raw_values) / len(raw_values), 2),
            "sample_count": len(raw_values),
            "config": args.hx711_config,
        }
    else:
        weight_g = args.weight_g
        raw = args.weight_raw if args.weight_raw is not None else 0.0
        weight_source = {
            "type": args.weight_source,
            "weight_g": round(weight_g, 2),
            "raw_filtered": round(raw, 2) if args.weight_raw is not None else None,
            "sample_count": 0,
            "config": args.hx711_config,
        }

    model_path = Path(args.model) if args.model else Path("models/yolo_product_detector.rknn")
    labels_path = Path(args.labels) if args.labels else Path("models/yolo_product_detector.labels.json")
    recognition = recognize_product_detector_rknn(
        image_path=image_path,
        model_path=model_path,
        products_path=Path(args.products),
        labels_path=labels_path,
        weight_g=weight_g,
        accept_confidence=args.accept_confidence,
        confirm_gap=args.confirm_gap,
        topk=args.topk,
        imgsz=args.rknn_imgsz,
        layout=args.rknn_layout,
        float_input=args.rknn_float_input,
        conf_threshold=args.det_conf,
        iou_threshold=args.det_iou,
        max_det=args.det_max,
        score_sigmoid=args.det_score_sigmoid,
        detector=rknn_predictor,
    )

    record = build_transaction(recognition, image_path=image_path, device_id=args.device_id)
    apply_policy_to_record(record, device_policy)
    record["camera"] = {
        "device": args.device,
        "requested_size": [args.width, args.height],
        "frame_shape": list(frame_shape),
    }
    record["weight_source"] = weight_source
    if record.get("detections") is not None:
        preview_path = Path("outputs/detections") / f"{record['transaction_id']}.jpg"
        try:
            save_preview(image_path, record.get("detections", []), preview_path)
            record["detection_preview_image"] = str(preview_path)
        except Exception as exc:
            record["detection_preview_error"] = str(exc)

    if runtime_config:
        record["runtime_config"] = {
            "path": str(runtime_path),
            "version": runtime_config.get("version", ""),
            "accept_confidence": args.accept_confidence,
            "tts_volume": args.tts_volume,
        }
    record["policy"]["path"] = str(policy_path)

    tts_result = speak_text(
        record["voice_text"],
        backend=args.tts,
        port=args.tts_port,
        baudrate=args.tts_baudrate,
        encoding=args.tts_encoding,
        music=args.tts_music,
        volume=args.tts_volume,
        music_volume=args.tts_music_volume,
        speed=args.tts_speed,
    )
    if tts_result is not None:
        record["tts"] = {
            "backend": args.tts,
            "port": args.tts_port,
            "baudrate": args.tts_baudrate,
            "encoding": args.tts_encoding,
            "music": args.tts_music,
            "volume": args.tts_volume,
            "music_volume": args.tts_music_volume,
            "speed": args.tts_speed,
            "frame_hex": tts_result["frame"].hex(" "),
            "response_hex": tts_result["response"].hex(" ") if tts_result["response"] else "",
        }

    record_path = None if args.no_record else Path(args.records)
    if record_path is not None:
        append_jsonl(record_path, record)

    event = build_device_event(
        "policy_applied",
        args.device_id,
        device_policy,
        message=f"Applied policy {device_policy.get('policy_version')} to transaction {record['transaction_id']}",
        extra={
            "transaction_id": record["transaction_id"],
            "record_status": record["status"],
            "pricing_mode": record.get("pricing", {}).get("mode", ""),
        },
    )
    append_device_event(Path("records/device_events.jsonl"), event)

    if args.mqtt:
        try:
            mqtt_config = load_mqtt_config(Path(args.mqtt_config))
            record["mqtt"] = publish_record(record, mqtt_config)
        except Exception as exc:
            record["mqtt"] = {"error": str(exc)}
            if not args.mqtt_optional:
                raise
        try:
            record["device_event"] = publish_device_event(event, mqtt_config)
        except Exception as exc:
            record["device_event"] = {"error": str(exc)}
            if not args.mqtt_optional:
                raise

    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(f"Captured image: {image_path}")
        if args.weight_g is None:
            print(f"HX711 raw filtered: {raw:.2f}")
        else:
            print(f"Weight provided by {args.weight_source}: {weight_g:.2f} g")
        print_receipt(record, record_path)
    return record


# 入口:解析 CLI 后调一次 run_transaction
def main() -> int:
    args, runtime_path, runtime_config, policy_path, device_policy = parse_transaction_args()
    run_transaction(args, runtime_path, runtime_config, policy_path, device_policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
