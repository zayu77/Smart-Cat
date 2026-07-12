"""Publish Smart-Cat transaction records to an MQTT broker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# MQTT 基础配置默认值：host/port/credentials/topics 等，加载 mqtt.json 时叠加覆盖
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 1883,
    "topic": "smart-cat/lubancat3_demo_001/transactions",
    "voice_command_topic": "smart-cat/lubancat3_demo_001/voice-commands",
    "client_id": "smart-cat-lubancat3-demo-001",
    "username": "",
    "password": "",
    "qos": 1,
    "retain": False,
    "keepalive": 60,
    "connect_timeout": 10,
}


# 从基准 topic 派生运行参数 topic：去掉 /transactions 后缀接 /runtime-config；支持显式覆盖
def derive_runtime_config_topic(config: dict[str, Any]) -> str:
    if config.get("runtime_config_topic"):
        return str(config["runtime_config_topic"])
    topic = str(config.get("topic", DEFAULT_CONFIG["topic"]))
    if topic.endswith("/transactions"):
        return f"{topic[:-len('/transactions')]}/runtime-config"
    return f"{topic}/runtime-config"


# 从基准 topic 派生设备策略 topic：去掉 /transactions 后缀接 /device-policy；支持显式覆盖
def derive_device_policy_topic(config: dict[str, Any]) -> str:
    if config.get("device_policy_topic"):
        return str(config["device_policy_topic"])
    topic = str(config.get("topic", DEFAULT_CONFIG["topic"]))
    if topic.endswith("/transactions"):
        return f"{topic[:-len('/transactions')]}/device-policy"
    return f"{topic}/device-policy"


# 从基准 topic 派生设备事件 topic：去掉 /transactions 后缀接 /device-events；支持显式覆盖
def derive_device_event_topic(config: dict[str, Any]) -> str:
    if config.get("device_event_topic"):
        return str(config["device_event_topic"])
    topic = str(config.get("topic", DEFAULT_CONFIG["topic"]))
    if topic.endswith("/transactions"):
        return f"{topic[:-len('/transactions')]}/device-events"
    return f"{topic}/device-events"


# 从基准 topic 派生语音命令 topic：去掉 /transactions 后缀接 /voice-commands；支持显式覆盖
def derive_voice_command_topic(config: dict[str, Any]) -> str:
    if config.get("voice_command_topic"):
        return str(config["voice_command_topic"])
    topic = str(config.get("topic", DEFAULT_CONFIG["topic"]))
    if topic.endswith("/transactions"):
        return f"{topic[:-len('/transactions')]}/voice-commands"
    return f"{topic}/voice-commands"


# 读 mqtt.json 叠加到默认配置：None 字段保留默认值不覆盖（让"删字段"也能回退到默认）
def load_mqtt_config(path: Path | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path is not None and path.exists():
        with path.open("r", encoding="utf-8") as file:
            user_config = json.load(file)
        config.update({key: value for key, value in user_config.items() if value is not None})
    return config


# 把 dict 序列化为最小化 JSON：ensure_ascii=False 保中文，separators 去掉多余空格
def build_mqtt_payload(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


# 连接 broker 阻塞式发布一条消息：paho 缺失时抛清晰错误，disconnect 包在 finally 防泄漏
def publish_json(payload_data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("paho-mqtt is not installed. Run: python -m pip install paho-mqtt") from exc

    client = mqtt.Client(client_id=str(config["client_id"]), protocol=mqtt.MQTTv311)
    if config.get("username"):
        client.username_pw_set(str(config["username"]), str(config.get("password", "")))

    host = str(config["host"])
    port = int(config["port"])
    keepalive = int(config.get("keepalive", 60))
    topic = str(config["topic"])
    qos = int(config.get("qos", 1))
    retain = bool(config.get("retain", False))
    timeout = float(config.get("connect_timeout", 10))
    payload = build_mqtt_payload(payload_data)

    client.connect(host, port, keepalive=keepalive)
    client.loop_start()
    try:
        result = client.publish(topic, payload=payload.encode("utf-8"), qos=qos, retain=retain)
        result.wait_for_publish(timeout=timeout)
        if not result.is_published():
            raise TimeoutError(f"MQTT publish timed out after {timeout} seconds")
    finally:
        client.loop_stop()
        client.disconnect()

    return {
        "host": host,
        "port": port,
        "topic": topic,
        "qos": qos,
        "retain": retain,
        "payload_bytes": len(payload.encode("utf-8")),
    }


# 业务封装：发布一条交易记录（直接转发到 publish_json，保留语义命名）
def publish_record(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return publish_json(record, config)


# 业务封装：发布一条设备事件到派生 topic、retain=False 不置顶（事件流不留历史）
def publish_device_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    publish_config = dict(config)
    publish_config["topic"] = derive_device_event_topic(config)
    publish_config["retain"] = False
    return publish_json(event, publish_config)


# CLI 入口：读 JSON 记录 → 加载配置 → dry-run 只打 payload 或真发到 broker
def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one transaction JSON record to MQTT.")
    parser.add_argument("--record", required=True, help="JSON file containing one transaction record.")
    parser.add_argument("--config", default="config/mqtt.json", help="MQTT config JSON path.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload and config without publishing.")
    args = parser.parse_args()

    with Path(args.record).open("r", encoding="utf-8") as file:
        record = json.load(file)
    config = load_mqtt_config(Path(args.config))
    payload = build_mqtt_payload(record)

    if args.dry_run:
        print(json.dumps({"config": config, "payload": json.loads(payload)}, ensure_ascii=False, indent=2))
        return 0

    result = publish_record(record, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# python mqtt_publisher.py 直接执行时跑 main()；被 import 时不跑（保留模块可用性）
if __name__ == "__main__":
    raise SystemExit(main())
