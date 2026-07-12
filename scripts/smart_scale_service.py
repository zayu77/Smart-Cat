"""Long-running Smart-Cat service that triggers transactions by weight."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from device_policy import append_device_event, build_device_event, load_policy
from hx711_reader import HX711, HX711Config, filtered_raw, grams_from_raw, load_config
from mqtt_publisher import load_mqtt_config, publish_device_event
from predict_rknn_detector import RKNNDetector
from smart_scale_demo import CameraCapture, parse_transaction_args, run_transaction as run_smart_scale_transaction


ROOT = Path(__file__).resolve().parents[1]


# 临时把 stdout/stderr 重定向到 /dev/null 的上下文管理器,用于屏蔽 RKNN/相机初始化时刷屏的 C++ 日志
@contextlib.contextmanager
def quiet_stdio(enabled: bool):
    if not enabled:
        yield
        return

    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)
            os.close(old_stdout)
            os.close(old_stderr)


# 长寿命的 HX711 读取器:服务进程里只初始化一次,反复调用 read_once 拿重量
class WeightReader:
    def __init__(self, config_path: Path, gpio_backend: str) -> None:
        self.config_path = config_path
        self.config: HX711Config = load_config(config_path, dout_gpio=134, sck_gpio=132)
        self.hx711 = HX711(
            self.config.dout_gpio,
            self.config.sck_gpio,
            backend=gpio_backend,
            dout_chip=self.config.dout_chip,
            dout_line=self.config.dout_line,
            sck_chip=self.config.sck_chip,
            sck_line=self.config.sck_line,
        )

    # 一次重量读取:采 samples 个原始值 → 中值滤波 → 换算成克,返回 (raw, grams)
    def read_once(self, samples: int, interval: float, max_deviation: float) -> tuple[float, float]:
        values = self.hx711.read_values(samples=samples, interval=interval)
        raw = filtered_raw(values, max_deviation=max_deviation)
        return raw, grams_from_raw(raw, self.config)


# 启动 mqtt_runtime_config.py 子进程从 broker 拉最新配置写到本地,返回是否成功;--verbose 时显示子进程输出
def sync_remote_config(args) -> bool:
    if args.no_sync:
        return False
    command = [
        sys.executable,
        str(ROOT / "scripts" / "mqtt_runtime_config.py"),
        "--mqtt-config",
        args.mqtt_config,
        "--output",
        args.runtime_config,
        "--policy-output",
        args.device_policy,
        "--optional",
        "--timeout",
        str(args.sync_timeout),
    ]
    output = None if args.verbose else subprocess.DEVNULL
    completed = subprocess.run(command, cwd=ROOT, check=False, stdout=output, stderr=output)
    return completed.returncode == 0


# 用子进程跑一次 smart_scale_demo.py(--subprocess-transaction 模式),把已读到的重量和原始值注入,返回退出码
def run_transaction_subprocess(args, weight_g: float, raw: float) -> int:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "smart_scale_demo.py"),
        "--runtime-config",
        args.runtime_config,
        "--device-policy",
        args.device_policy,
        "--weight-g",
        f"{weight_g:.2f}",
        "--weight-raw",
        f"{raw:.2f}",
        "--weight-source",
        "service_trigger",
    ]
    command.extend(args.transaction_args)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode


# 把相对路径补全为相对于项目根 ROOT 的绝对路径,绝对路径原样返回
def relative_path(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


# 长寿命的交易执行器:缓存 RKNN 推理器和相机(避免每笔交易重新加载),并复用服务进程的 CLI 配置
class TransactionRunner:
    # 持有服务进程的 argparse 命名空间 + RKNN 推理器和相机的懒加载缓存(key 用于检测参数变化)
    def __init__(self, service_args) -> None:
        self.service_args = service_args
        self.rknn_predictor: RKNNDetector | None = None
        self.rknn_key: tuple | None = None
        self.camera_capture: CameraCapture | None = None
        self.camera_key: tuple | None = None

    # 释放 RKNN 推理器(常驻内存较大,NPU 也独占)和相机硬件,清空 key
    def close_rknn(self) -> None:
        if self.rknn_predictor is not None:
            self.rknn_predictor.close()
            self.rknn_predictor = None
            self.rknn_key = None

    # 同时关闭 RKNN 和相机,用于服务优雅停机
    def close(self) -> None:
        self.close_rknn()
        self.close_camera()

    # 释放相机设备,清空 key
    def close_camera(self) -> None:
        if self.camera_capture is not None:
            self.camera_capture.close()
            self.camera_capture = None
            self.camera_key = None

    # 把服务侧参数 + 当前重量拼成 smart_scale_demo.py 的 argv 列表(包含 --runtime-config/--device-policy/--weight-g)
    def build_transaction_argv(self, weight_g: float, raw: float) -> list[str]:
        argv = [
            "--runtime-config",
            self.service_args.runtime_config,
            "--device-policy",
            self.service_args.device_policy,
            "--weight-g",
            f"{weight_g:.2f}",
            "--weight-raw",
            f"{raw:.2f}",
            "--weight-source",
            "service_trigger",
        ]
        argv.extend(self.service_args.transaction_args)
        return argv

    # 算出当前交易参数对应的 RKNN 缓存 key,只有模型/标签/imgsz/layout/float_input/sigmoid 完全一致才复用同一个推理器
    def predictor_key(self, transaction_args) -> tuple:
        model_path = Path(transaction_args.model) if transaction_args.model else Path("models/yolo_product_detector.rknn")
        labels_path = Path(transaction_args.labels) if transaction_args.labels else Path("models/yolo_product_detector.labels.json")
        return (
            str(relative_path(model_path)),
            str(relative_path(labels_path)),
            int(transaction_args.rknn_imgsz),
            str(transaction_args.rknn_layout),
            bool(transaction_args.rknn_float_input),
            bool(getattr(transaction_args, "det_score_sigmoid", False)),
        )

    # 懒加载 RKNN 推理器:参数和缓存一致直接复用,否则先关旧的再加载新的;verbose 时打印,否则用 quiet_stdio 屏蔽 C++ 日志
    def load_predictor(self, transaction_args, reason: str) -> RKNNDetector:
        model_path = Path(transaction_args.model) if transaction_args.model else Path("models/yolo_product_detector.rknn")
        labels_path = Path(transaction_args.labels) if transaction_args.labels else Path("models/yolo_product_detector.labels.json")
        key = self.predictor_key(transaction_args)
        if self.rknn_predictor is not None and self.rknn_key == key:
            return self.rknn_predictor

        self.close_rknn()
        if self.service_args.verbose:
            print(f"[RKNN] loading {reason}: {model_path}")
        with quiet_stdio(not self.service_args.verbose):
            self.rknn_predictor = RKNNDetector(
                model_path=relative_path(model_path),
                labels_path=relative_path(labels_path),
                imgsz=int(transaction_args.rknn_imgsz),
                layout=str(transaction_args.rknn_layout),
                float_input=bool(transaction_args.rknn_float_input),
                score_sigmoid=bool(transaction_args.det_score_sigmoid),
            )
        self.rknn_key = key
        if self.service_args.verbose:
            print("[RKNN] ready for next transaction")
        return self.rknn_predictor

    # 空闲时预热:用一组占位 weight=0 跑一遍 argparse,只为提前把 RKNN 和相机加载好(子进程模式下不预热)
    def preload_idle(self) -> None:
        if self.service_args.subprocess_transaction:
            return
        transaction_args, _runtime_path, _runtime_config, _policy_path, _device_policy = parse_transaction_args(
            self.build_transaction_argv(0.0, 0.0)
        )
        self.load_predictor(transaction_args, "while idle")
        self.camera_for(transaction_args)

    # 拿(或加载)本次交易要用的 RKNN 推理器,并校验 key 没被并发改过
    def take_predictor_for_transaction(self, transaction_args) -> RKNNDetector:
        predictor = self.load_predictor(transaction_args, "for transaction")
        if self.rknn_key != self.predictor_key(transaction_args):
            raise RuntimeError("RKNN predictor key mismatch")
        return predictor

    # 懒加载相机:device/width/height 跟缓存一致就复用,否则先关旧的再打开新的(同样用 quiet_stdio 屏蔽 OpenCV 日志)
    def camera_for(self, transaction_args) -> CameraCapture:
        key = (
            str(transaction_args.device),
            int(transaction_args.width),
            int(transaction_args.height),
        )
        if self.camera_capture is not None and self.camera_key == key:
            return self.camera_capture

        self.close_camera()
        if self.service_args.verbose:
            print(f"[Camera] opening: {transaction_args.device} {transaction_args.width}x{transaction_args.height}")
        with quiet_stdio(not self.service_args.verbose):
            self.camera_capture = CameraCapture(
                device=str(transaction_args.device),
                width=int(transaction_args.width),
                height=int(transaction_args.height),
            )
        self.camera_key = key
        if self.service_args.verbose:
            print("[Camera] ready")
        return self.camera_capture

    # 执行一次交易:parse 参数 → 拿 RKNN → 拿相机 → 调 run_smart_scale_transaction,失败仅记日志并保证 finally 里关 RKNN
    def run(self, weight_g: float, raw: float) -> int:
        try:
            transaction_args, runtime_path, runtime_config, policy_path, device_policy = parse_transaction_args(
                self.build_transaction_argv(weight_g, raw)
            )
            predictor = self.take_predictor_for_transaction(transaction_args)
            camera_capture = self.camera_for(transaction_args)
            run_smart_scale_transaction(
                transaction_args,
                runtime_path,
                runtime_config,
                policy_path,
                device_policy,
                rknn_predictor=predictor,
                camera_capture=camera_capture,
            )
            return 0
        except Exception as exc:
            print(f"[ERROR] transaction failed: {exc}")
            return 1
        finally:
            self.close_rknn()


# 判稳定:窗口内最大-最小值 ≤ stable_delta_g 就认为秤已经稳定
def is_stable(weights: list[float], stable_delta_g: float) -> bool:
    if not weights:
        return False
    return max(weights) - min(weights) <= stable_delta_g


# 从 device_runtime.json 里读 device_id,任何异常(文件缺失/格式坏/字段为空)都兜底成默认 ID
def load_device_id(runtime_config_path: Path) -> str:
    if not runtime_config_path.exists():
        return "lubancat3_demo_001"
    try:
        data = json.loads(runtime_config_path.read_text(encoding="utf-8"))
    except Exception:
        return "lubancat3_demo_001"
    if not isinstance(data, dict):
        return "lubancat3_demo_001"
    return str(data.get("device_id") or "lubancat3_demo_001")


# 发服务级事件:写本地 status_file + 追加到 events.jsonl + 推 MQTT;heartbeat_interval<=0 时整个跳过
def publish_service_event(
    args,
    event_type: str,
    state: str,
    weight_g: float | None,
    transaction_count: int,
    message: str = "",
    extra: dict | None = None,
) -> None:
    if args.heartbeat_interval <= 0:
        return
    runtime_config_path = Path(args.runtime_config)
    policy_path = Path(args.device_policy)
    try:
        policy = load_policy(policy_path)
    except Exception:
        policy = {}
    payload = {
        "service_state": state,
        "transaction_count": transaction_count,
        "runtime_config": str(runtime_config_path),
        "device_policy": str(policy_path),
    }
    if weight_g is not None:
        payload["current_weight_g"] = round(weight_g, 2)
    if extra:
        payload.update(extra)
    event = build_device_event(
        event_type,
        load_device_id(runtime_config_path),
        policy,
        message=message,
        extra=payload,
    )
    status_path = Path(args.status_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "device_id": event.get("device_id", ""),
            "service_state": state,
            "current_weight_g": payload.get("current_weight_g"),
            "transaction_count": transaction_count,
            "event_type": event_type,
            "message": message,
            "event_id": event.get("event_id", ""),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    append_device_event(Path(args.events), event)
    try:
        mqtt_config = load_mqtt_config(Path(args.mqtt_config))
        publish_device_event(event, mqtt_config)
    except Exception as exc:
        print(f"[WARN] service event publish failed: {exc}")


# 长寿命服务主循环:解析 CLI → 打印启动信息 → 不停读重量 → 根据 IDLE/WAIT_STABLE/RUN_TRANSACTION/WAIT_REMOVE 状态机触发交易
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Smart-Cat as a long-running weight-triggered service.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--runtime-config", default="config/device_runtime.json", help="Runtime config JSON path.")
    parser.add_argument("--device-policy", default="config/device_policy.json", help="Device policy JSON path.")
    parser.add_argument("--mqtt-config", default="config/mqtt.json", help="MQTT config JSON path.")
    parser.add_argument("--hx711-config", default="config/hx711_scale.json", help="HX711 calibration config path.")
    parser.add_argument("--gpio-backend", choices=["gpiod", "sysfs"], default="gpiod", help="GPIO backend.")
    parser.add_argument("--samples", type=int, default=5, help="HX711 samples per polling read.")
    parser.add_argument("--sample-interval", type=float, default=0.02, help="Delay between HX711 samples.")
    parser.add_argument("--max-deviation", type=float, default=5000.0, help="Raw-count deviation used to filter outliers.")
    parser.add_argument("--weight-threshold-g", type=float, default=30.0, help="Weight above this value triggers stable detection.")
    parser.add_argument("--empty-threshold-g", type=float, default=20.0, help="Weight below this value means the scale is empty.")
    parser.add_argument("--stable-samples", type=int, default=5, help="Number of consecutive stable readings required.")
    parser.add_argument("--stable-delta-g", type=float, default=3.0, help="Maximum weight range across stable readings.")
    parser.add_argument("--poll-interval", type=float, default=0.3, help="Delay between service polling reads.")
    parser.add_argument("--cooldown", type=float, default=2.0, help="Delay after a transaction before waiting for removal.")
    parser.add_argument("--sync-timeout", type=float, default=3.0, help="MQTT retained config sync timeout.")
    parser.add_argument("--sync-interval", type=float, default=15.0, help="Seconds between remote config sync attempts while the scale is empty.")
    parser.add_argument("--heartbeat-interval", type=float, default=60.0, help="Seconds between service heartbeat events. Use 0 to disable.")
    parser.add_argument("--events", default="records/device_events.jsonl", help="Local device events JSONL path.")
    parser.add_argument("--status-file", default="records/service_status.json", help="Current service status JSON path.")
    parser.add_argument("--no-sync", action="store_true", help="Do not sync runtime config and device policy while idle.")
    parser.add_argument("--subprocess-transaction", action="store_true", help="Run each transaction through smart_scale_demo.py subprocess, useful for fallback debugging.")
    parser.add_argument("--once", action="store_true", help="Exit after the first triggered transaction.")
    parser.add_argument("--verbose", action="store_true", help="Show detailed MQTT sync and RKNN runtime logs.")
    parser.add_argument("transaction_args", nargs=argparse.REMAINDER, help="Extra args passed to smart_scale_demo.py after --.")
    args = parser.parse_args()
    if args.transaction_args and args.transaction_args[0] == "--":
        args.transaction_args = args.transaction_args[1:]

    sync_text = "disabled" if args.no_sync else f"{args.sync_interval:g}s"
    heartbeat_text = f"{args.heartbeat_interval:g}s" if args.heartbeat_interval > 0 else "disabled"
    print(
        "Smart-Cat service started: "
        f"trigger>{args.weight_threshold_g:g}g, empty<{args.empty_threshold_g:g}g, "
        f"stable={args.stable_samples}x/{args.stable_delta_g:g}g, "
        f"sync={sync_text}, heartbeat={heartbeat_text}. Ctrl+C to stop."
    )

    reader = WeightReader(Path(args.hx711_config), args.gpio_backend)
    transaction_runner = TransactionRunner(args)
    stable_window: list[tuple[float, float]] = []
    state = "IDLE"
    transaction_count = 0
    last_sync_at = 0.0
    last_heartbeat_at = 0.0
    publish_service_event(args, "service_state", state, None, transaction_count, "Smart-Cat service started.")

    try:
        while True:
            try:
                raw, weight_g = reader.read_once(
                    args.samples,
                    args.sample_interval,
                    args.max_deviation,
                )
            except Exception as exc:
                print(f"[ERROR] weight read failed: {exc}")
                time.sleep(max(args.poll_interval, 1.0))
                continue

            now = time.monotonic()
            if args.heartbeat_interval > 0 and now - last_heartbeat_at >= args.heartbeat_interval:
                publish_service_event(args, "service_heartbeat", state, weight_g, transaction_count)
                last_heartbeat_at = time.monotonic()

            if state == "IDLE":
                if weight_g >= args.weight_threshold_g:
                    stable_window = [(raw, weight_g)]
                    state = "WAIT_STABLE"
                    print(f"[WAIT_STABLE] detected {weight_g:.1f} g")
                    publish_service_event(
                        args,
                        "service_state",
                        state,
                        weight_g,
                        transaction_count,
                        "Object detected, waiting for stable weight.",
                    )
                elif not args.no_sync and weight_g < args.empty_threshold_g:
                    now = time.monotonic()
                    sync_status = "skip"
                    prepare_status = "ready"
                    did_prepare = False
                    if last_sync_at <= 0 or now - last_sync_at >= args.sync_interval:
                        sync_status = "ok" if sync_remote_config(args) else "failed"
                        last_sync_at = time.monotonic()
                        transaction_runner.close_rknn()
                        transaction_runner.close_camera()
                        did_prepare = True
                    if transaction_runner.rknn_predictor is None:
                        try:
                            transaction_runner.preload_idle()
                            prepare_status = "ready"
                            did_prepare = True
                        except Exception as exc:
                            prepare_status = "failed"
                            did_prepare = True
                            print(f"[WARN] idle prepare failed: {exc}")
                    if did_prepare:
                        print(f"[IDLE] ready: sync={sync_status}, rknn={prepare_status}, camera={prepare_status}, weight={weight_g:.1f} g")
                elif args.no_sync and weight_g < args.empty_threshold_g:
                    if transaction_runner.rknn_predictor is None:
                        try:
                            transaction_runner.preload_idle()
                            print(f"[IDLE] ready: sync=disabled, rknn=ready, camera=ready, weight={weight_g:.1f} g")
                        except Exception as exc:
                            print(f"[WARN] idle prepare failed: {exc}")
            elif state == "WAIT_STABLE":
                if weight_g < args.empty_threshold_g:
                    stable_window = []
                    state = "IDLE"
                    print("[IDLE] object removed before stable")
                    last_sync_at = 0.0
                    publish_service_event(
                        args,
                        "service_state",
                        state,
                        weight_g,
                        transaction_count,
                        "Object removed before stable weight.",
                    )
                else:
                    stable_window.append((raw, weight_g))
                    stable_window = stable_window[-args.stable_samples :]
                    weights = [item[1] for item in stable_window]
                    if len(stable_window) >= args.stable_samples and is_stable(weights, args.stable_delta_g):
                        stable_raw = sum(item[0] for item in stable_window) / len(stable_window)
                        stable_weight = sum(weights) / len(weights)
                        print(f"[RUN_TRANSACTION] stable weight {stable_weight:.1f} g")
                        state = "RUN_TRANSACTION"
                        publish_service_event(
                            args,
                            "service_state",
                            state,
                            stable_weight,
                            transaction_count,
                            "Stable weight reached, running transaction.",
                        )
                        if args.subprocess_transaction:
                            code = run_transaction_subprocess(args, stable_weight, stable_raw)
                        else:
                            code = transaction_runner.run(stable_weight, stable_raw)
                        transaction_count += 1
                        print(f"[WAIT_REMOVE] transaction #{transaction_count} exit={code}")
                        publish_service_event(
                            args,
                            "service_state",
                            "WAIT_REMOVE",
                            stable_weight,
                            transaction_count,
                            "Transaction finished, waiting for object removal.",
                            {"transaction_exit_code": code},
                        )
                        time.sleep(args.cooldown)
                        if args.once:
                            transaction_runner.close()
                            return code
                        state = "WAIT_REMOVE"
            elif state == "WAIT_REMOVE":
                if weight_g < args.empty_threshold_g:
                    stable_window = []
                    state = "IDLE"
                    last_sync_at = 0.0
                    publish_service_event(
                        args,
                        "service_state",
                        state,
                        weight_g,
                        transaction_count,
                        "Scale is empty.",
                    )

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        transaction_runner.close()
        publish_service_event(args, "service_state", state, None, transaction_count, "Smart-Cat service stopped.")
        print("\nSmart-Cat service stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
