"""Local Web dashboard for Smart-Cat records and product data."""

from __future__ import annotations

import argparse
import json
import mimetypes
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from dashboard_data import (
    DEFAULT_DEVICE_EVENTS,
    DEFAULT_POLICY_HISTORY,
    DEFAULT_PRODUCTS,
    DEFAULT_RECORDS,
    ROOT,
    build_analytics,
    build_device_status,
    filter_records,
    find_record,
    first_query_value,
    load_products_config,
    load_device_events,
    load_records,
    latest_device_event,
    save_records,
    normalize_runtime_config,
    records_to_csv,
    safe_media_path,
    summarize,
    summarize_device_events,
    update_products_config,
    update_transaction_record,
)
from dashboard_mqtt import (
    DEVICE_POLICY_CACHE,
    DEVICE_POLICY_PUSH_STATUS,
    MQTT_SYNC_STATUS,
    RUNTIME_CONFIG_CACHE,
    RUNTIME_CONFIG_PUSH_STATUS,
    get_device_policy_for_dashboard,
    get_runtime_config_for_dashboard,
    publish_device_policy,
    publish_runtime_config,
    rollback_device_policy,
    start_mqtt_sync,
)
from device_policy import normalize_policy
from mqtt_publisher import derive_voice_command_topic, load_mqtt_config, publish_json
from product_business import format_money
from transaction_utils import append_jsonl


# 把 URL 里的 /static/xxx 解析到 web/static 下的真实文件,做路径越界校验防目录穿越
def safe_static_path(raw_path: str) -> Path:
    relative = raw_path.removeprefix("/static/").strip("/")
    candidate = (STATIC_ROOT / relative).resolve()
    static_root = STATIC_ROOT.resolve()
    if static_root != candidate and static_root not in candidate.parents:
        raise ValueError("Static path is outside web/static")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative)
    return candidate


WEB_ROOT = ROOT / "web"
INDEX_HTML_PATH = WEB_ROOT / "index.html"
STATIC_ROOT = WEB_ROOT / "static"


POLICY_COMPARE_KEYS = (
    "policy_version",
    "description",
    "enabled",
    "low_confidence_action",
    "unknown_product_action",
    "pricing_mode",
    "voice_template",
    "confirm_voice_template",
    "reject_voice_template",
)
VOICE_COMMANDS = {"status", "weight", "latest", "price", "pending", "help"}
CONFIRMABLE_STATUSES = {"low_confidence", "needs_confirm", "unknown", "rejected"}
VOICE_COMMAND_KEYWORDS = {
    "status": ("状态", "设备状态", "运行状态"),
    "weight": ("重量", "多重", "当前重量", "称重"),
    "latest": ("最近", "上一笔", "刚刚", "重复", "最近交易"),
    "price": ("价格", "多少钱", "总价", "报价", "单价"),
    "pending": ("待确认", "确认", "是否确认", "识别了吗"),
    "help": ("帮助", "怎么用", "命令"),
}
PRODUCT_ALIASES = {
    "apple": ("苹果", "红苹果"),
    "banana": ("香蕉",),
    "orange": ("橙子", "橘子", "桔子"),
    "pear": ("梨", "雪梨"),
    "tomato": ("西红柿", "番茄"),
}


# 语音文本归一化:小写 + 去首尾空白 + 去除中英文标点和空白,方便做包含匹配
def compact_voice_text(text: str) -> str:
    return "".join(char for char in text.strip().lower() if char not in " ，。,.!?！？、：:；;“”\"' \t\r\n")


# 找最近一条可以"语音修正"的交易:跳过 status=invalid_record 的噪声记录,优先选 low_confidence/needs_confirm/unknown/rejected
def latest_voice_correction_target(records: list[dict]) -> dict | None:
    valid_records = [record for record in records if record.get("transaction_id")]
    if not valid_records:
        return None
    latest_record = valid_records[-1]
    if str(latest_record.get("status") or "") != "invalid_record":
        return latest_record
    for record in reversed(valid_records):
        if str(record.get("status") or "") in CONFIRMABLE_STATUSES:
            return record
    return latest_record


# 在语音文本里搜产品 id/name/voice_name/中文别名,命中就返回 product_id,否则返回 None
def match_product_from_voice(text: str, products: dict) -> str | None:
    compact = compact_voice_text(text)
    for product_id, item in products.items():
        candidates = {
            str(product_id),
            str(item.get("name", "")),
            str(item.get("voice_name", "")),
            *PRODUCT_ALIASES.get(str(product_id), ()),
        }
        for candidate in candidates:
            candidate = compact_voice_text(candidate)
            if candidate and candidate in compact:
                return str(product_id)
    return None


# 解析手机端语音文本 → 意图(intent):confirm_product(改商品) 或 voice_command(查询类命令);空文本或都不匹配则抛错
def parse_voice_intent(text: str, products: dict) -> dict:
    raw_text = str(text or "").strip()
    compact = compact_voice_text(raw_text)
    if not compact:
        raise ValueError("语音文本不能为空")

    target_text = raw_text
    for marker in ("改成", "改为", "应该是", "应为", "这是", "确认"):
        if marker in target_text:
            target_text = target_text.rsplit(marker, 1)[-1]
            break
    if "不是" in raw_text and "是" in raw_text.replace("不是", ""):
        target_text = raw_text.rsplit("是", 1)[-1]

    product_id = match_product_from_voice(target_text, products) or match_product_from_voice(raw_text, products)
    correction_words = ("这是", "改成", "改为", "不是", "识别错", "错了", "应该是", "应为", "确认", "是")
    if product_id and any(word in raw_text for word in correction_words):
        product = products[product_id]
        return {
            "intent": "confirm_product",
            "product_id": product_id,
            "product_name": product.get("name", product_id),
        }
    if product_id and not any(keyword in compact for values in VOICE_COMMAND_KEYWORDS.values() for keyword in values):
        product = products[product_id]
        return {
            "intent": "confirm_product",
            "product_id": product_id,
            "product_name": product.get("name", product_id),
        }

    for command, keywords in VOICE_COMMAND_KEYWORDS.items():
        if any(compact_voice_text(keyword) in compact for keyword in keywords):
            return {"intent": "voice_command", "command": command}
    if compact in VOICE_COMMANDS:
        return {"intent": "voice_command", "command": compact}
    raise ValueError("没有识别到可执行的语音补盲指令")


# 生成 TTS 播报内容:已修正为 xx,净重 xx 克,单价 x 元每斤,总价 x 元
def build_correction_speech(record: dict) -> str:
    product_name = record.get("product_name") or record.get("product_id") or "商品"
    weight_g = float(record.get("weight_g") or 0)
    unit_price = format_money(float(record.get("unit_price") or 0))
    total_price = format_money(float(record.get("total_price") or 0))
    unit = record.get("unit") or "斤"
    return f"已修正为{product_name}，净重{weight_g:.0f}克，单价{unit_price}元每{unit}，总价{total_price}元。"


# 把语音指令 payload 通过 MQTT 发布到设备的语音命令主题,捕获异常并以 error 字段回传(不让 HTTP 5xx)
def publish_voice_payload(mqtt_config_path: Path, payload: dict) -> dict:
    mqtt_config = load_mqtt_config(mqtt_config_path)
    topic = derive_voice_command_topic(mqtt_config)
    publish_config = dict(mqtt_config)
    publish_config["topic"] = topic
    publish_config["retain"] = False
    response = {
        "topic": topic,
        "payload": payload,
        "mqtt": None,
    }
    try:
        response["mqtt"] = publish_json(payload, publish_config)
    except Exception as exc:
        response["mqtt"] = {"error": str(exc)}
    return response


# 规范化两版策略后逐字段比较(只比较关键业务字段,忽略时间戳/版本号),用于决定是否要归档历史
def policy_changed(left: dict, right: dict) -> bool:
    left_policy = normalize_policy(left)
    right_policy = normalize_policy(right)
    return any(left_policy.get(key) != right_policy.get(key) for key in POLICY_COMPARE_KEYS)


# HTTP 处理器:对外暴露记录/产品/策略/语音指令等 REST 接口,文件路径通过类属性由 main 注入
class DashboardHandler(BaseHTTPRequestHandler):
    records_path: Path = DEFAULT_RECORDS
    events_path: Path = DEFAULT_DEVICE_EVENTS
    policy_history_path: Path = DEFAULT_POLICY_HISTORY
    products_path: Path = DEFAULT_PRODUCTS
    mqtt_config_path: Path = ROOT / "config" / "mqtt.json"
    runtime_config_topic: str | None = None
    device_policy_topic: str | None = None
    verbose = False

    # 改写 BaseHTTPRequestHandler 默认的访问日志,只在 --verbose 时打印,避免日常运行被日志刷屏
    def log_message(self, format: str, *args) -> None:
        if self.verbose:
            print(f"{self.address_string()} - {format % args}")

    # 通用二进制响应:状态行 + Content-Type/Length/Cache-Control(强制 no-store)+ 写 body
    def send_bytes(self, data: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # send_bytes 的 JSON 便捷封装:utf-8 编码 + ensure_ascii=False 保留中文
    def send_json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", status)

    # 文件下载响应:加 Content-Disposition 触发浏览器另存为,文件名由调用方指定
    def send_download(self, data: bytes, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # 读 POST 请求体:按 Content-Length 读取并以 utf-8 解码后 json.loads,长度缺失默认为 0
    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # 读 POST 请求体(可空版本):没有 Content-Length 或 body 为空时返回 {},供 rollback 这类可选 body 的接口用
    def read_optional_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # GET 路由:首页 / 静态文件 / 记录/分析/交易/产品 / 运行时配置/状态 / 设备策略/状态/历史 / 设备事件 / MQTT 状态 / 设备总览 / 媒体文件
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(INDEX_HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            try:
                static_path = safe_static_path(parsed.path)
                content_type = mimetypes.guess_type(static_path.name)[0] or "application/octet-stream"
                self.send_bytes(static_path.read_bytes(), content_type)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/summary":
            query = parse_qs(parsed.query)
            records = filter_records(load_records(self.records_path), query)
            self.send_json(summarize(records))
            return
        if parsed.path == "/api/analytics":
            query = parse_qs(parsed.query)
            records = filter_records(load_records(self.records_path), query)
            self.send_json(build_analytics(records))
            return
        if parsed.path == "/api/transactions":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["100"])[0])
            records = filter_records(load_records(self.records_path), query)
            self.send_json(list(reversed(records[-limit:])))
            return
        if parsed.path == "/api/transaction":
            query = parse_qs(parsed.query)
            transaction_id = first_query_value(query, "id")
            record = find_record(load_records(self.records_path), transaction_id)
            if record is None:
                self.send_json({"error": "Transaction not found"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(record)
            return
        if parsed.path == "/api/transactions.csv":
            query = parse_qs(parsed.query)
            records = filter_records(load_records(self.records_path), query)
            self.send_download(records_to_csv(records), "smart-cat-transactions.csv", "text/csv; charset=utf-8")
            return
        if parsed.path == "/api/products":
            self.send_json(load_products_config(self.products_path))
            return
        if parsed.path == "/api/runtime-config":
            config = get_runtime_config_for_dashboard(self.mqtt_config_path, self.runtime_config_topic)
            self.send_json({
                "config": config,
                "status": dict(RUNTIME_CONFIG_PUSH_STATUS),
                "cache": dict(RUNTIME_CONFIG_CACHE),
            })
            return
        if parsed.path == "/api/runtime-config/status":
            self.send_json(dict(RUNTIME_CONFIG_PUSH_STATUS))
            return
        if parsed.path == "/api/device-policy":
            policy = get_device_policy_for_dashboard(self.mqtt_config_path, self.device_policy_topic)
            self.send_json({
                "policy": policy,
                "status": dict(DEVICE_POLICY_PUSH_STATUS),
                "cache": dict(DEVICE_POLICY_CACHE),
                "latest_event": latest_device_event(self.events_path, "policy_applied") or {},
            })
            return
        if parsed.path == "/api/device-policy/status":
            self.send_json(dict(DEVICE_POLICY_PUSH_STATUS))
            return
        if parsed.path == "/api/device-policy/history":
            history = load_records(self.policy_history_path)
            indexed = []
            for index, item in enumerate(history):
                if isinstance(item, dict):
                    entry = dict(item)
                    entry["history_index"] = index
                    indexed.append(entry)
            self.send_json(list(reversed(indexed)))
            return
        if parsed.path == "/api/device-events/latest":
            self.send_json(latest_device_event(self.events_path) or {})
            return
        if parsed.path == "/api/device-events":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["100"])[0])
            event_type = first_query_value(query, "event_type")
            self.send_json(load_device_events(self.events_path, limit=limit, event_type=event_type))
            return
        if parsed.path == "/api/device-events/summary":
            self.send_json(summarize_device_events(self.events_path))
            return
        if parsed.path == "/api/mqtt-status":
            self.send_json(dict(MQTT_SYNC_STATUS))
            return
        if parsed.path == "/api/device-status":
            self.send_json(build_device_status(load_records(self.records_path), dict(MQTT_SYNC_STATUS), self.events_path))
            return
        if parsed.path == "/media":
            query = parse_qs(parsed.query)
            raw_path = query.get("path", [""])[0]
            try:
                media_path = safe_media_path(raw_path)
                content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
                self.send_bytes(media_path.read_bytes(), content_type)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    # POST 路由:清空记录/事件 / 改产品配置 / 推送运行时配置/设备策略 / 策略回滚 / 语音命令 / 语音意图(含商品修正) / 手动确认交易
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/transactions/clear":
            try:
                count = len(load_records(self.records_path))
                save_records(self.records_path, [])
                self.send_json({"cleared": "transactions", "count": count, "path": str(self.records_path)})
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/device-events/clear":
            try:
                count = len(load_records(self.events_path))
                save_records(self.events_path, [])
                self.send_json({"cleared": "device_events", "count": count, "path": str(self.events_path)})
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/products":
            try:
                data = self.read_json_body()
                products = update_products_config(self.products_path, data)
                self.send_json(products)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/runtime-config":
            try:
                data = self.read_json_body()
                config = normalize_runtime_config(data)
                response = {"config": config, "mqtt": None}
                try:
                    response["mqtt"] = publish_runtime_config(config, self.mqtt_config_path, self.runtime_config_topic)
                except Exception as exc:
                    RUNTIME_CONFIG_PUSH_STATUS.update({
                        "enabled": True,
                        "last_error": str(exc),
                        "topic": self.runtime_config_topic or "",
                    })
                    response["mqtt"] = {"error": str(exc)}
                response["applied_summary"] = {
                    "device_id": config.get("device_id", ""),
                    "backend": config.get("recognition", {}).get("backend"),
                    "accept_confidence": config.get("recognition", {}).get("accept_confidence"),
                    "det_conf": config.get("recognition", {}).get("det_conf"),
                    "tts_volume": config.get("tts", {}).get("volume"),
                    "mqtt_enabled": config.get("mqtt", {}).get("enabled"),
                }
                response["status"] = dict(RUNTIME_CONFIG_PUSH_STATUS)
                response["cache"] = dict(RUNTIME_CONFIG_CACHE)
                self.send_json(response)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/device-policy":
            try:
                data = self.read_json_body()
                if not isinstance(data, dict):
                    raise ValueError("Device policy must be a JSON object")
                data["modified_at"] = datetime.now().isoformat(timespec="seconds")
                response = {"policy": data, "mqtt": None}
                try:
                    current_policy = get_device_policy_for_dashboard(self.mqtt_config_path, self.device_policy_topic)
                    archived_policy = None
                    if policy_changed(current_policy, data):
                        archived_policy = dict(current_policy)
                        archived_policy["archived_at"] = data["modified_at"]
                    response["mqtt"] = publish_device_policy(data, self.mqtt_config_path, self.device_policy_topic)
                    if archived_policy is not None:
                        append_jsonl(self.policy_history_path, archived_policy)
                    response["policy"] = get_device_policy_for_dashboard(self.mqtt_config_path, self.device_policy_topic)
                except Exception as exc:
                    DEVICE_POLICY_PUSH_STATUS.update({
                        "enabled": True,
                        "last_error": str(exc),
                        "topic": self.device_policy_topic or "",
                    })
                    response["mqtt"] = {"error": str(exc)}
                response["status"] = dict(DEVICE_POLICY_PUSH_STATUS)
                response["cache"] = dict(DEVICE_POLICY_CACHE)
                response["latest_event"] = latest_device_event(self.events_path, "policy_applied") or {}
                self.send_json(response)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/device-policy/rollback":
            try:
                payload = self.read_optional_json_body()
                history = load_records(self.policy_history_path)
                if history:
                    requested_index = payload.get("history_index") if isinstance(payload, dict) else None
                    if requested_index is None:
                        rollback_index = len(history) - 1
                    else:
                        rollback_index = int(requested_index)
                    if rollback_index < 0 or rollback_index >= len(history):
                        raise ValueError("Policy history item not found")
                    policy = normalize_policy(history[rollback_index])
                    policy["modified_at"] = datetime.now().isoformat(timespec="seconds")
                    mqtt_result = publish_device_policy(policy, self.mqtt_config_path, self.device_policy_topic)
                    save_records(self.policy_history_path, history[:rollback_index] + history[rollback_index + 1:])
                    response = {
                        "policy": policy,
                        "mqtt": mqtt_result,
                    }
                else:
                    response = rollback_device_policy(self.mqtt_config_path, self.device_policy_topic)
                response["status"] = dict(DEVICE_POLICY_PUSH_STATUS)
                response["cache"] = dict(DEVICE_POLICY_CACHE)
                response["policy"] = get_device_policy_for_dashboard(self.mqtt_config_path, self.device_policy_topic)
                response["latest_event"] = latest_device_event(self.events_path, "policy_applied") or {}
                self.send_json(response)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/voice-command":
            try:
                data = self.read_json_body()
                if not isinstance(data, dict):
                    raise ValueError("Voice command request body must be an object")
                command = str(data.get("command", "")).strip().lower()
                if command not in VOICE_COMMANDS:
                    raise ValueError(f"Unsupported voice command: {command}")
                payload = {
                    "request_id": uuid4().hex,
                    "command": command,
                    "source": str(data.get("source") or "web_dashboard"),
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
                response = publish_voice_payload(self.mqtt_config_path, payload)
                response.update({
                    "command": command,
                    "request_id": payload["request_id"],
                })
                self.send_json(response)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/voice-intent":
            try:
                data = self.read_json_body()
                if not isinstance(data, dict):
                    raise ValueError("Voice intent request body must be an object")
                text = str(data.get("text", "")).strip()
                source = str(data.get("source") or "web_mobile")
                products = load_products_config(self.products_path)
                intent = parse_voice_intent(text, products)

                if intent["intent"] == "voice_command":
                    command = str(intent["command"])
                    payload = {
                        "request_id": uuid4().hex,
                        "command": command,
                        "source": source,
                        "text": text,
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                    }
                    response = publish_voice_payload(self.mqtt_config_path, payload)
                    response.update({
                        "intent": intent,
                        "command": command,
                        "request_id": payload["request_id"],
                    })
                    self.send_json(response)
                    return

                if intent["intent"] == "confirm_product":
                    target = latest_voice_correction_target(load_records(self.records_path))
                    if target is None:
                        raise ValueError("暂无可修正的交易记录")
                    product = products[intent["product_id"]]
                    updated = update_transaction_record(self.records_path, self.products_path, str(target["transaction_id"]), {
                        "product_id": intent["product_id"],
                        "unit_price": product.get("unit_price", 0),
                        "operator": "voice_mobile",
                        "note": f"手机语音补盲：{text}",
                    })
                    bind_payload = {
                        "request_id": uuid4().hex,
                        "command": "bind_memory",
                        "source": source,
                        "recognized_text": text,
                        "transaction_id": updated.get("transaction_id"),
                        "product_id": intent["product_id"],
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                    }
                    bind_response = publish_voice_payload(self.mqtt_config_path, bind_payload)
                    speech_text = build_correction_speech(updated)
                    payload = {
                        "request_id": uuid4().hex,
                        "command": "speak_text",
                        "text": speech_text,
                        "source": source,
                        "recognized_text": text,
                        "transaction_id": updated.get("transaction_id"),
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                    }
                    response = publish_voice_payload(self.mqtt_config_path, payload)
                    response.update({
                        "intent": intent,
                        "transaction_id": updated.get("transaction_id"),
                        "updated": updated,
                        "memory_bind": bind_response,
                        "speech_text": speech_text,
                        "request_id": payload["request_id"],
                    })
                    self.send_json(response)
                    return

                raise ValueError(f"Unsupported voice intent: {intent['intent']}")
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/transaction/confirm":
            try:
                data = self.read_json_body()
                if not isinstance(data, dict):
                    raise ValueError("Request body must be an object")
                transaction_id = str(data.get("transaction_id", "")).strip()
                updated = update_transaction_record(self.records_path, self.products_path, transaction_id, data)
                bind_payload = {
                    "request_id": uuid4().hex,
                    "command": "bind_memory",
                    "source": str(data.get("operator") or "web_dashboard"),
                    "transaction_id": updated.get("transaction_id"),
                    "product_id": updated.get("product_id"),
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
                try:
                    updated["product_memory"]["bind_command"] = publish_voice_payload(self.mqtt_config_path, bind_payload)
                except Exception as exc:
                    updated["product_memory"]["bind_command"] = {"error": str(exc)}
                self.send_json(updated)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

# 解析 CLI → 用 args 覆盖 DashboardHandler 的文件路径/MQTT 配置/主题,启动 ThreadingHTTPServer,可选开启 MQTT 后台同步
def main() -> int:
    parser = argparse.ArgumentParser(description="Run Smart-Cat local Web dashboard.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host.")
    parser.add_argument("--port", type=int, default=8080, help="Listen port.")
    parser.add_argument("--records", default=str(DEFAULT_RECORDS), help="Transaction JSONL path.")
    parser.add_argument("--events", default=str(DEFAULT_DEVICE_EVENTS), help="Device events JSONL path.")
    parser.add_argument("--policy-history", default=str(DEFAULT_POLICY_HISTORY), help="Device policy history JSONL path.")
    parser.add_argument("--products", default=str(DEFAULT_PRODUCTS), help="Products JSON path.")
    parser.add_argument("--mqtt-sync", action="store_true", help="Subscribe MQTT transactions into the dashboard records file.")
    parser.add_argument("--mqtt-config", default="config/mqtt.json", help="MQTT config JSON path.")
    parser.add_argument("--mqtt-topic", default=None, help="Override MQTT subscribe topic.")
    parser.add_argument("--runtime-config-topic", default=None, help="Override MQTT runtime config topic.")
    parser.add_argument("--device-policy-topic", default=None, help="Override MQTT device policy topic.")
    parser.add_argument("--verbose", action="store_true", help="Show HTTP access logs and MQTT sync details.")
    args = parser.parse_args()

    class Handler(DashboardHandler):
        records_path = Path(args.records)
        events_path = Path(args.events)
        policy_history_path = Path(args.policy_history)
        products_path = Path(args.products)
        mqtt_config_path = Path(args.mqtt_config)
        runtime_config_topic = args.runtime_config_topic
        device_policy_topic = args.device_policy_topic
        verbose = args.verbose

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sync_text = "on" if args.mqtt_sync else "off"
    print(f"Smart-Cat dashboard: http://{args.host}:{args.port} records={Handler.records_path} mqtt-sync={sync_text}")
    if args.verbose:
        print(f"Device events: {Handler.events_path}")
        print(f"Policy history: {Handler.policy_history_path}")
        print(f"Products: {Handler.products_path}")
        print("Runtime config source: MQTT retained topic")
    if args.mqtt_sync:
        start_mqtt_sync(Path(args.mqtt_config), Handler.records_path, args.mqtt_topic, Handler.events_path, verbose=args.verbose)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
