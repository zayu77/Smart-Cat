"""Sync Smart-Cat runtime config from MQTT retained message."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from dashboard_data import normalize_runtime_config
from mqtt_publisher import derive_device_policy_topic, derive_runtime_config_topic, load_mqtt_config


# 从 MQTT 字节 payload 抽运行参数：支持 envelope/裸 config 两种格式 + normalize 补齐字段
def extract_config(payload: bytes) -> dict[str, Any]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Runtime config MQTT payload must be a JSON object")
    config = data.get("config", data)
    if not isinstance(config, dict):
        raise ValueError("Runtime config payload does not contain a config object")
    return normalize_runtime_config(config)


# 从 MQTT 字节 payload 抽设备策略：支持 envelope/裸 policy 两种格式，不 normalize 原样返回
def extract_policy(payload: bytes) -> dict[str, Any]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Device policy MQTT payload must be a JSON object")
    policy = data.get("policy", data)
    if not isinstance(policy, dict):
        raise ValueError("Device policy payload does not contain a policy object")
    return policy


# 把 dict 写为格式化 JSON：建父目录、保中文、缩进 2、末尾加换行（POSIX 文本惯例）
def save_runtime_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


# 临时连 broker 拉一条 retained 消息：超时/收到任一即停；payload_extractor 让运行参数和策略共用
def pull_runtime_config(
    mqtt_config: dict[str, Any],
    output: Path,
    timeout: float,
    topic_override: str | None = None,
    save_output: bool = True,
    payload_extractor=extract_config,
) -> dict[str, Any] | None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("paho-mqtt is not installed. Run: python -m pip install paho-mqtt") from exc

    topic = topic_override or derive_runtime_config_topic(mqtt_config)
    qos = int(mqtt_config.get("qos", 1))
    host = str(mqtt_config["host"])
    port = int(mqtt_config["port"])
    keepalive = int(mqtt_config.get("keepalive", 60))
    client_id = f"{mqtt_config['client_id']}-runtime-config-pull"
    received: dict[str, Any] = {}
    error: dict[str, str] = {}

    # paho 收到 retained 消息回调：抽数据 → 可选写盘 → 主动 disconnect 结束阻塞
    def on_message(client, _userdata, message) -> None:
        try:
            config = payload_extractor(message.payload)
            if save_output:
                save_runtime_config(output, config)
            received["config"] = config
            received["topic"] = message.topic
            client.disconnect()
        except Exception as exc:
            error["message"] = str(exc)
            client.disconnect()

    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
    if mqtt_config.get("username"):
        client.username_pw_set(str(mqtt_config["username"]), str(mqtt_config.get("password", "")))
    client.on_message = on_message

    client.connect(host, port, keepalive=keepalive)
    client.subscribe(topic, qos=qos)
    client.loop_start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not received and not error:
            time.sleep(0.05)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

    if error:
        raise ValueError(error["message"])
    if not received:
        return None
    return received


# CLI 入口：拉运行参数 + 可选拉策略，--optional 让失败时只打印警告不报错
def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Smart-Cat runtime config from MQTT retained message.")
    parser.add_argument("--mqtt-config", default="config/mqtt.json", help="MQTT config JSON path.")
    parser.add_argument("--output", default="config/device_runtime.json", help="Runtime config output JSON path.")
    parser.add_argument("--topic", default=None, help="Override runtime config topic.")
    parser.add_argument("--policy-output", default=None, help="Optional device policy output JSON path.")
    parser.add_argument("--policy-topic", default=None, help="Override device policy topic.")
    parser.add_argument("--timeout", type=float, default=3.0, help="Seconds to wait for retained config.")
    parser.add_argument("--optional", action="store_true", help="Do not fail if MQTT sync fails or no config is retained.")
    parser.add_argument("--dry-run", action="store_true", help="Read retained config without writing the output file.")
    parser.add_argument("--print-config", action="store_true", help="Print the received runtime config JSON.")
    args = parser.parse_args()

    try:
        config = load_mqtt_config(Path(args.mqtt_config))
        topic = args.topic or derive_runtime_config_topic(config)
        print(f"Runtime config topic: {topic}")
        result = pull_runtime_config(
            config,
            Path(args.output),
            args.timeout,
            args.topic,
            save_output=not args.dry_run,
        )
        if result is None:
            message = "No retained runtime config received."
            if args.optional:
                print(message)
            else:
                raise TimeoutError(message)
        else:
            action = "read" if args.dry_run else "synced"
            print(f"Runtime config {action}: {result['topic']} -> {args.output}")
            if args.print_config:
                print(json.dumps(result["config"], ensure_ascii=False, indent=2))
        if args.policy_output:
            policy_topic = args.policy_topic or derive_device_policy_topic(config)
            print(f"Device policy topic: {policy_topic}")
            policy_result = pull_runtime_config(
                config,
                Path(args.policy_output),
                args.timeout,
                policy_topic,
                save_output=not args.dry_run,
                payload_extractor=extract_policy,
            )
            if policy_result is None:
                message = "No retained device policy received."
                if args.optional:
                    print(message)
                else:
                    raise TimeoutError(message)
            else:
                action = "read" if args.dry_run else "synced"
                print(f"Device policy {action}: {policy_result['topic']} -> {args.policy_output}")
        return 0
    except Exception as exc:
        if args.optional:
            print(f"Runtime config sync skipped: {exc}")
            return 0
        raise


# python mqtt_runtime_config.py 直接执行时跑 main()；被 import 时不跑（保留模块可用性）
if __name__ == "__main__":
    raise SystemExit(main())
