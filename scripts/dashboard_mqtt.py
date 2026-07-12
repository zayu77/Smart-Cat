"""MQTT sync and runtime-config publishing helpers for the Smart-Cat dashboard."""

from __future__ import annotations

import json
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path

from dashboard_data import default_runtime_config, normalize_runtime_config
from device_policy import default_policy, normalize_policy
from mqtt_publisher import derive_device_event_topic, derive_device_policy_topic, derive_runtime_config_topic, load_mqtt_config, publish_json
from transaction_utils import append_jsonl


MQTT_SYNC_STATUS = {
    "enabled": False,
    "connected": False,
    "host": "",
    "port": None,
    "topic": "",
    "last_error": "",
    "last_message_at": "",
    "last_transaction_id": "",
    "received_count": 0,
}
RUNTIME_CONFIG_PUSH_STATUS = {
    "enabled": False,
    "last_published_at": "",
    "last_error": "",
    "topic": "",
}
RUNTIME_CONFIG_CACHE = {
    "config": None,
    "source": "",
    "updated_at": "",
}
DEVICE_POLICY_PUSH_STATUS = {
    "enabled": False,
    "last_published_at": "",
    "last_error": "",
    "topic": "",
    "rollback_available": False,
}
DEVICE_POLICY_CACHE = {
    "policy": None,
    "source": "",
    "updated_at": "",
}
DEVICE_POLICY_HISTORY: list[dict] = []


# 从已有 JSONL 抽取所有 transaction_id 装进 set：用于 MQTT 收到重复消息时跳过
def load_seen_transaction_ids(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            transaction_id = record.get("transaction_id")
            if transaction_id:
                seen.add(str(transaction_id))
    return seen


# 把 MQTT 字节 payload 解码 + 解析为 dict：顶层不是对象则抛 ValueError
def normalize_record(payload: bytes) -> dict:
    record = json.loads(payload.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("MQTT payload must be a JSON object")
    return record

# 把运行参数发布到 MQTT retained topic：板子下次拉取即可拿到；同时更新本进程缓存
def publish_runtime_config(config: dict, mqtt_config_path: Path, topic_override: str | None = None) -> dict:
    mqtt_config = load_mqtt_config(mqtt_config_path)
    topic = topic_override or derive_runtime_config_topic(mqtt_config)
    publish_config = dict(mqtt_config)
    publish_config["topic"] = topic
    publish_config["retain"] = True
    result = publish_json({
        "type": "runtime_config",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config": config,
    }, publish_config)
    RUNTIME_CONFIG_PUSH_STATUS.update({
        "enabled": True,
        "last_published_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": "",
        "topic": topic,
    })
    RUNTIME_CONFIG_CACHE.update({
        "config": config,
        "source": "published",
        "updated_at": RUNTIME_CONFIG_PUSH_STATUS["last_published_at"],
    })
    return result


# 从 MQTT retained 消息里抽运行参数：支持"包了一层 envelope"和"直接是 config"两种格式
def extract_runtime_config_payload(payload: bytes) -> dict:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Runtime config MQTT payload must be a JSON object")
    config = data.get("config", data)
    if not isinstance(config, dict):
        raise ValueError("Runtime config payload does not contain a config object")
    return normalize_runtime_config(config)


# 临时连一次 MQTT 拉 retained 消息拿到最新运行参数：1.2s 超时；paho 缺失则返回 None
def fetch_runtime_config_from_mqtt(mqtt_config_path: Path, topic_override: str | None = None, timeout: float = 1.2) -> dict | None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return None

    mqtt_config = load_mqtt_config(mqtt_config_path)
    topic = topic_override or derive_runtime_config_topic(mqtt_config)
    qos = int(mqtt_config.get("qos", 1))
    host = str(mqtt_config["host"])
    port = int(mqtt_config["port"])
    keepalive = int(mqtt_config.get("keepalive", 60))
    client_id = f"{mqtt_config['client_id']}-runtime-config-dashboard"
    received: dict[str, dict] = {}

    def on_message(client, _userdata, message) -> None:
        try:
            received["config"] = extract_runtime_config_payload(message.payload)
            RUNTIME_CONFIG_PUSH_STATUS["topic"] = message.topic
            RUNTIME_CONFIG_PUSH_STATUS["last_error"] = ""
            RUNTIME_CONFIG_CACHE.update({
                "config": received["config"],
                "source": "mqtt_retained",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:
            RUNTIME_CONFIG_PUSH_STATUS["last_error"] = str(exc)
        finally:
            client.disconnect()

    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
    if mqtt_config.get("username"):
        client.username_pw_set(str(mqtt_config["username"]), str(mqtt_config.get("password", "")))
    client.on_message = on_message

    try:
        client.connect(host, port, keepalive=keepalive)
        client.subscribe(topic, qos=qos)
        client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline and "config" not in received:
            time.sleep(0.05)
    except Exception as exc:
        RUNTIME_CONFIG_PUSH_STATUS["last_error"] = str(exc)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

    return received.get("config")


# 3 级降级：MQTT retained → 进程内存缓存 → 代码默认值；web 后台显示运行参数用
def get_runtime_config_for_dashboard(mqtt_config_path: Path, topic_override: str | None = None) -> dict:
    config = fetch_runtime_config_from_mqtt(mqtt_config_path, topic_override)
    if config is not None:
        return config
    cached = RUNTIME_CONFIG_CACHE.get("config")
    if isinstance(cached, dict):
        return normalize_runtime_config(cached)
    return default_runtime_config()


# 把设备策略发布到 MQTT retained topic：version 变了自动把旧版本入历史栈（最多 5 版）
def publish_device_policy(policy: dict, mqtt_config_path: Path, topic_override: str | None = None) -> dict:
    policy = normalize_policy(policy)
    cached = DEVICE_POLICY_CACHE.get("policy")
    if isinstance(cached, dict) and cached.get("policy_version") != policy.get("policy_version"):
        DEVICE_POLICY_HISTORY.append(normalize_policy(cached))
        del DEVICE_POLICY_HISTORY[:-5]

    mqtt_config = load_mqtt_config(mqtt_config_path)
    topic = topic_override or derive_device_policy_topic(mqtt_config)
    publish_config = dict(mqtt_config)
    publish_config["topic"] = topic
    publish_config["retain"] = True
    result = publish_json({
        "type": "device_policy",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "policy": policy,
    }, publish_config)
    DEVICE_POLICY_PUSH_STATUS.update({
        "enabled": True,
        "last_published_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": "",
        "topic": topic,
        "rollback_available": bool(DEVICE_POLICY_HISTORY),
    })
    DEVICE_POLICY_CACHE.update({
        "policy": policy,
        "source": "published",
        "updated_at": DEVICE_POLICY_PUSH_STATUS["last_published_at"],
    })
    return result


# 从 MQTT retained 消息里抽设备策略：支持"包了一层 envelope"和"直接是 policy"两种格式
def extract_device_policy_payload(payload: bytes) -> dict:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Device policy MQTT payload must be a JSON object")
    policy = data.get("policy", data)
    if not isinstance(policy, dict):
        raise ValueError("Device policy payload does not contain a policy object")
    return normalize_policy(policy)


# 临时连一次 MQTT 拉 retained 消息拿到最新设备策略：1.2s 超时；paho 缺失则返回 None
def fetch_device_policy_from_mqtt(mqtt_config_path: Path, topic_override: str | None = None, timeout: float = 1.2) -> dict | None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return None

    mqtt_config = load_mqtt_config(mqtt_config_path)
    topic = topic_override or derive_device_policy_topic(mqtt_config)
    qos = int(mqtt_config.get("qos", 1))
    host = str(mqtt_config["host"])
    port = int(mqtt_config["port"])
    keepalive = int(mqtt_config.get("keepalive", 60))
    client_id = f"{mqtt_config['client_id']}-device-policy-dashboard"
    received: dict[str, dict] = {}

    def on_message(client, _userdata, message) -> None:
        try:
            received["policy"] = extract_device_policy_payload(message.payload)
            DEVICE_POLICY_PUSH_STATUS["topic"] = message.topic
            DEVICE_POLICY_PUSH_STATUS["last_error"] = ""
            DEVICE_POLICY_CACHE.update({
                "policy": received["policy"],
                "source": "mqtt_retained",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:
            DEVICE_POLICY_PUSH_STATUS["last_error"] = str(exc)
        finally:
            client.disconnect()

    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
    if mqtt_config.get("username"):
        client.username_pw_set(str(mqtt_config["username"]), str(mqtt_config.get("password", "")))
    client.on_message = on_message

    try:
        client.connect(host, port, keepalive=keepalive)
        client.subscribe(topic, qos=qos)
        client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline and "policy" not in received:
            time.sleep(0.05)
    except Exception as exc:
        DEVICE_POLICY_PUSH_STATUS["last_error"] = str(exc)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

    return received.get("policy")


# 3 级降级：MQTT retained → 进程内存缓存 → 代码默认值；web 后台显示策略用
def get_device_policy_for_dashboard(mqtt_config_path: Path, topic_override: str | None = None) -> dict:
    policy = fetch_device_policy_from_mqtt(mqtt_config_path, topic_override)
    if policy is not None:
        return policy
    cached = DEVICE_POLICY_CACHE.get("policy")
    if isinstance(cached, dict):
        return normalize_policy(cached)
    return default_policy()


# 从历史栈弹一版旧策略并重新发布：策略升级出错时的"撤销"按钮
def rollback_device_policy(mqtt_config_path: Path, topic_override: str | None = None) -> dict:
    if not DEVICE_POLICY_HISTORY:
        raise ValueError("No previous device policy is available for rollback")
    policy = DEVICE_POLICY_HISTORY.pop()
    result = publish_device_policy(policy, mqtt_config_path, topic_override)
    DEVICE_POLICY_PUSH_STATUS["rollback_available"] = bool(DEVICE_POLICY_HISTORY)
    return {"policy": policy, "mqtt": result}

# 后台守护线程：连 broker 订阅交易 + 设备事件 topic，收到消息去重后写 JSONL，断线自动重连
def start_mqtt_sync(
    config_path: Path,
    records_path: Path,
    topic_override: str | None = None,
    events_path: Path | None = None,
    event_topic_override: str | None = None,
    verbose: bool = False,
) -> threading.Thread:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit("paho-mqtt is not installed. Run: python -m pip install paho-mqtt") from exc

    config = load_mqtt_config(config_path)
    if verbose and not config_path.exists():
        print(f"MQTT sync config not found, using defaults: {config_path}")
    if topic_override:
        config["topic"] = topic_override

    seen_ids = load_seen_transaction_ids(records_path)
    topic = str(config["topic"])
    event_topic = event_topic_override or derive_device_event_topic(config)
    qos = int(config.get("qos", 1))
    host = str(config["host"])
    port = int(config["port"])
    keepalive = int(config.get("keepalive", 60))
    connect_timeout = float(config.get("connect_timeout", 10))
    retry_seconds = float(config.get("reconnect_interval", 5))
    client_id = f"{config['client_id']}-dashboard"
    MQTT_SYNC_STATUS.update({
        "enabled": True,
        "connected": False,
        "host": host,
        "port": port,
        "topic": topic,
        "event_topic": event_topic,
        "last_error": "",
        "last_message_at": "",
        "last_transaction_id": "",
        "received_count": 0,
    })

    # paho 收到任意订阅消息的回调：按 topic 分流到"设备事件"或"交易"两条处理链
    def on_message(_client, _userdata, message) -> None:
        try:
            if message.topic == event_topic and events_path is not None:
                event = json.loads(message.payload.decode("utf-8"))
                if not isinstance(event, dict):
                    raise ValueError("Device event payload must be a JSON object")
                append_jsonl(events_path, event)
                MQTT_SYNC_STATUS["last_message_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                MQTT_SYNC_STATUS["last_transaction_id"] = str(event.get("event_id") or "")
                MQTT_SYNC_STATUS["received_count"] = int(MQTT_SYNC_STATUS["received_count"]) + 1
                event_type = str(event.get("event_type", "unknown"))
                if event_type == "service_heartbeat":
                    state = str(event.get("service_state") or "unknown")
                    weight = event.get("current_weight_g")
                    weight_text = "n/a" if weight is None else f"{float(weight):.1f}g"
                    count = int(event.get("transaction_count") or 0)
                    if verbose:
                        print(f"MQTT heartbeat: {event.get('device_id', 'unknown')} {state} {weight_text} tx={count}")
                else:
                    if verbose:
                        print(f"MQTT event: {event_type} {event.get('policy_version', '')}")
                return

            record = normalize_record(message.payload)
            transaction_id = str(record.get("transaction_id") or "")
            if transaction_id and transaction_id in seen_ids:
                if verbose:
                    print(f"MQTT sync skip duplicate: {transaction_id}")
                return
            append_jsonl(records_path, record)
            if transaction_id:
                seen_ids.add(transaction_id)
            MQTT_SYNC_STATUS["last_message_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            MQTT_SYNC_STATUS["last_transaction_id"] = transaction_id
            MQTT_SYNC_STATUS["received_count"] = int(MQTT_SYNC_STATUS["received_count"]) + 1
            product = record.get("product_name") or record.get("product_id") or "unknown"
            if verbose:
                print(f"MQTT sync saved: {transaction_id or 'no-id'} {product}")
        except Exception as exc:
            MQTT_SYNC_STATUS["last_error"] = str(exc)
            print(f"MQTT sync failed to handle message: {exc}")

    # broker 连上/重连后的回调：rc==0 才订阅 topic；MQTT 协议规定订阅必须在连接成功后
    def on_connect(client, _userdata, _flags, rc) -> None:
        MQTT_SYNC_STATUS["connected"] = rc == 0
        MQTT_SYNC_STATUS["last_error"] = "" if rc == 0 else f"connect rc={rc}"
        if rc == 0:
            client.subscribe(topic, qos=qos)
            if events_path is not None:
                client.subscribe(event_topic, qos=qos)
            if verbose:
                print("MQTT sync connected.")
        else:
            print(f"MQTT sync connect failed: rc={rc}")

    # 断线回调：仅在"被动断开"（rc!=0）时记错误；主动断开(rc==0)是正常关闭不算错
    def on_disconnect(_client, _userdata, rc) -> None:
        MQTT_SYNC_STATUS["connected"] = False
        if rc:
            MQTT_SYNC_STATUS["last_error"] = f"disconnect rc={rc}"

    # 后台线程入口：循环"建 client → connect → loop_forever → 断则 sleep 重试"，永不退出
    def worker() -> None:
        while True:
            if verbose:
                client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            else:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=DeprecationWarning)
                    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            if config.get("username"):
                client.username_pw_set(str(config["username"]), str(config.get("password", "")))
            client.on_message = on_message
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client._connect_timeout = connect_timeout
            if verbose:
                print(f"MQTT sync broker: {host}:{port}")
                print(f"MQTT sync topic: {topic}")
            try:
                client.connect(host, port, keepalive=keepalive)
                client.loop_forever()
            except Exception as exc:
                MQTT_SYNC_STATUS["connected"] = False
                MQTT_SYNC_STATUS["last_error"] = str(exc)
                print(f"MQTT sync disconnected: {exc}")
                print(f"MQTT sync retry after {retry_seconds:g}s")
                try:
                    client.disconnect()
                except Exception:
                    pass
                time.sleep(retry_seconds)

    thread = threading.Thread(target=worker, name="smart-cat-mqtt-sync", daemon=True)
    thread.start()
    return thread
