"""Subscribe MQTT voice accessibility commands and execute them on the edge device."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from device_policy import append_device_event, build_device_event, load_policy
from mqtt_publisher import derive_voice_command_topic, load_mqtt_config, publish_device_event
from product_business import load_products
from product_memory import DEFAULT_MEMORY_PATH, add_product_memory
from transaction_utils import append_jsonl
from tts_output import speak_text as tts_speak_text
from voice_accessibility import runtime_tts_defaults


# 项目根路径常量：用于子进程 cwd 和脚本定位
ROOT = Path(__file__).resolve().parents[1]
# 板子端允许响应的命令白名单：查询命令 + 直说模式 + 商品记忆绑定
ALLOWED_COMMANDS = {"status", "weight", "latest", "price", "pending", "help", "speak_text", "bind_memory"}


# 规范化 MQTT payload 里的命令：去空格 + 小写 + 别名映射 + 白名单校验（不在白名单抛错）
def normalize_command(payload: dict) -> str:
    command = str(payload.get("command") or "").strip().lower()
    aliases = {
        "state": "status",
        "repeat": "latest",
        "last": "latest",
        "total": "price",
        "confirm": "pending",
    }
    command = aliases.get(command, command)
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"Unsupported voice command: {command or '<empty>'}")
    return command


# 子进程调 voice_accessibility.py：隔离 TTS 故障、不阻塞订阅主循环（child 崩了 parent 没事）
def run_voice_command(args, command: str) -> tuple[int, str]:
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "voice_accessibility.py"),
            command,
            "--service-status",
            args.service_status,
            "--records",
            args.records,
            "--runtime-config",
            args.runtime_config,
            "--command-log",
            args.command_log,
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = (process.stdout or "").strip()
    response = output.splitlines()[0] if output else ""
    return process.returncode, response


# 直接 TTS 播报 payload 里的 text：用于云端推送的广告/通知类消息（不走查询逻辑）
def run_direct_speech(args, payload: dict) -> tuple[int, str]:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("speak_text command requires non-empty text")
    tts = runtime_tts_defaults(Path(args.runtime_config))
    tts_speak_text(
        text,
        backend=tts["backend"],
        port=tts["port"],
        baudrate=tts["baudrate"],
        encoding=tts["encoding"],
        music=tts["music"],
        volume=tts["volume"],
        music_volume=tts["music_volume"],
        speed=tts["speed"],
    )
    append_jsonl(Path(args.command_log), {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "command": "speak_text",
        "normalized_command": "speak_text",
        "response": text,
        "source": payload.get("source", ""),
        "request_id": payload.get("request_id", ""),
        "tts_backend": tts["backend"],
    })
    return 0, text


def load_jsonl_records(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def save_jsonl_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_transaction(records: list[dict], transaction_id: str) -> dict | None:
    for record in reversed(records):
        if str(record.get("transaction_id") or "") == transaction_id:
            return record
    return None


# 板端商品记忆绑定：Web 后台只下发 transaction_id/product_id，真正读图和提特征在板端完成
def run_bind_memory(args, payload: dict) -> tuple[int, str, dict]:
    transaction_id = str(payload.get("transaction_id") or "").strip()
    product_id = str(payload.get("product_id") or "").strip()
    if not transaction_id:
        raise ValueError("bind_memory requires transaction_id")
    if not product_id:
        raise ValueError("bind_memory requires product_id")

    products = load_products(Path(args.products))
    if product_id not in products:
        raise ValueError(f"Unknown product_id for memory binding: {product_id}")

    records_path = Path(args.records)
    records = load_jsonl_records(records_path)
    record = find_transaction(records, transaction_id)
    if record is None:
        raise ValueError(f"Transaction not found on edge device: {transaction_id}")

    memory_item = add_product_memory(
        memory_path=Path(args.product_memory),
        record=record,
        product_id=product_id,
        product=dict(products[product_id]),
        root=ROOT,
    )
    record["product_memory"] = {
        "saved": True,
        "memory_id": memory_item.get("memory_id"),
        "feature_version": memory_item.get("feature_version"),
        "saved_at": memory_item.get("created_at"),
        "source": "edge_bind_memory",
    }
    save_jsonl_records(records_path, records)
    response = f"已为{products[product_id].get('name', product_id)}保存商品记忆。"
    return 0, response, {
        "memory_id": memory_item.get("memory_id"),
        "product_id": product_id,
        "transaction_id": transaction_id,
        "feature_version": memory_item.get("feature_version"),
    }


# 把命令结果写设备事件：本地 JSONL 必写，--no-event-publish 决定是否 MQTT 推回云端
def publish_command_event(args, command: str, payload: dict, status: str, message: str, extra: dict | None = None) -> None:
    try:
        policy = load_policy(Path(args.device_policy))
    except Exception:
        policy = {}
    device_id = str(payload.get("device_id") or args.device_id)
    event = build_device_event(
        "voice_command_executed",
        device_id,
        policy,
        status=status,
        message=message,
        extra={
            "command": command,
            "source": payload.get("source", ""),
            "request_id": payload.get("request_id", ""),
            **(extra or {}),
        },
    )
    append_device_event(Path(args.events), event)
    if args.no_event_publish:
        return
    try:
        publish_device_event(event, load_mqtt_config(Path(args.mqtt_config)))
    except Exception as exc:
        print(f"Voice command event publish failed: {exc}")


# 主循环：连 broker → 订阅 voice-commands → 调子进程或直说 → 写事件；断线 sleep 重连
def main() -> int:
    parser = argparse.ArgumentParser(description="Subscribe MQTT voice commands and execute Smart-Cat TTS commands.")
    parser.add_argument("--mqtt-config", default="config/mqtt.json", help="MQTT config JSON path.")
    parser.add_argument("--topic", default="", help="Override voice command topic.")
    parser.add_argument("--runtime-config", default="config/device_runtime.json", help="Runtime config JSON path.")
    parser.add_argument("--device-policy", default="config/device_policy.json", help="Device policy JSON path.")
    parser.add_argument("--service-status", default="records/service_status.json", help="Service status JSON path.")
    parser.add_argument("--records", default="records/transactions.jsonl", help="Transaction JSONL path.")
    parser.add_argument("--products", default="config/products.json", help="Products JSON path.")
    parser.add_argument("--product-memory", default=str(DEFAULT_MEMORY_PATH), help="Product memory JSONL path.")
    parser.add_argument("--command-log", default="records/voice_commands.jsonl", help="Voice command log JSONL path.")
    parser.add_argument("--events", default="records/device_events.jsonl", help="Device events JSONL path.")
    parser.add_argument("--device-id", default="lubancat3_demo_001", help="Fallback device ID.")
    parser.add_argument("--no-event-publish", action="store_true", help="Do not publish command result as device event.")
    args = parser.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit("paho-mqtt is not installed. Run: python -m pip install paho-mqtt") from exc

    config = load_mqtt_config(Path(args.mqtt_config))
    topic = args.topic or derive_voice_command_topic(config)
    qos = int(config.get("qos", 1))
    host = str(config["host"])
    port = int(config["port"])
    keepalive = int(config.get("keepalive", 60))
    client_id = f"{config['client_id']}-voice-command-{uuid4().hex[:8]}"

    # broker 连上回调：rc==0 才订阅 voice-commands topic，失败只打日志不抛
    def on_connect(client, _userdata, _flags, rc) -> None:
        if rc == 0:
            client.subscribe(topic, qos=qos)
            print(f"Voice command MQTT connected: {host}:{port}")
            print(f"Voice command topic: {topic}")
        else:
            print(f"Voice command MQTT connect failed: rc={rc}")

    # 收到命令回调：解析 → 路由 → 执行 → 写事件；try/except 包裹保证单条失败不中断订阅
    def on_message(_client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Voice command payload must be a JSON object")
            payload.setdefault("received_at", datetime.now().isoformat(timespec="seconds"))
            command = normalize_command(payload)
            if command == "speak_text":
                code, response = run_direct_speech(args, payload)
                extra = {}
            elif command == "bind_memory":
                code, response, extra = run_bind_memory(args, payload)
            else:
                code, response = run_voice_command(args, command)
                extra = {}
            ok = code == 0
            print(f"Voice command: {command} exit={code} response={response}")
            publish_command_event(
                args,
                command,
                payload,
                "success" if ok else "failed",
                response or f"Voice command {command} finished with exit code {code}.",
                {"exit_code": code, "response": response, **extra},
            )
        except Exception as exc:
            print(f"Voice command failed: {exc}")
            payload = payload if "payload" in locals() and isinstance(payload, dict) else {}
            publish_command_event(args, "unknown", payload, "failed", str(exc))

    while True:
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        if config.get("username"):
            client.username_pw_set(str(config["username"]), str(config.get("password", "")))
        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(host, port, keepalive=keepalive)
            client.loop_forever()
        except KeyboardInterrupt:
            print("\nVoice command MQTT stopped.")
            return 0
        except Exception as exc:
            print(f"Voice command MQTT disconnected: {exc}")
            try:
                client.disconnect()
            except Exception:
                pass
            time.sleep(float(config.get("reconnect_interval", 5)))


# python voice_command_mqtt.py 直接执行时跑 main()；被 import 时不跑（保留模块可用性）
if __name__ == "__main__":
    raise SystemExit(main())
