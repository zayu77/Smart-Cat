"""Data, product, transaction, and runtime config helpers for the Smart-Cat dashboard."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from product_business import build_voice_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "records" / "transactions.jsonl"
DEFAULT_DEVICE_EVENTS = ROOT / "records" / "device_events.jsonl"
DEFAULT_POLICY_HISTORY = ROOT / "records" / "policy_history.jsonl"
DEFAULT_PRODUCTS = ROOT / "config" / "products.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PRODUCT_UNITS = {"斤", "kg"}
FINAL_SALE_STATUSES = {"accepted", "manually_confirmed", "memory_matched"}

# 读 JSON 文件：不存在时返回调用方给的 fallback（空 dict/空 list），不抛 FileNotFoundError
def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


# 写 JSON 文件：自动创建父目录、保留中文、缩进 2 空格（让人能直接 cat 读）
def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


# 返回运行参数默认值：5 段配置（识别 / 摄像头 / 称重 / TTS / MQTT），web 端字段名约定
def default_runtime_config() -> dict:
    return {
        "version": "1.0.0",
        "device_id": "lubancat3_demo_001",
        "recognition": {
            "backend": "rknn-det",
            "accept_confidence": 0.75,
            "confirm_gap": 0.15,
            "topk": 3,
            "rknn_imgsz": 640,
            "rknn_layout": "nhwc",
            "rknn_float_input": False,
            "det_conf": 0.25,
            "det_iou": 0.45,
            "det_max": 20,
            "det_score_sigmoid": False,
        },
        "camera": {
            "device": "0",
            "image_output": "outputs/current.jpg",
            "width": 1920,
            "height": 1080,
            "warmup": 1.0,
        },
        "weight": {
            "hx711_config": "config/hx711_scale.json",
            "gpio_backend": "gpiod",
            "samples": 20,
            "max_deviation": 5000.0,
        },
        "tts": {
            "backend": "syn6288",
            "port": "/dev/ttyS10",
            "baudrate": 9600,
            "encoding": "gb2312",
            "music": 0,
            "volume": 3,
            "music_volume": 0,
            "speed": 5,
        },
        "mqtt": {
            "enabled": True,
            "config": "config/mqtt.json",
            "optional": True,
        },
    }


# 把任意输入夹紧到 [min, max]：类型错回退到 default，整数可自动 round；表单数据防御性处理
def clamp_number(value, minimum: float, maximum: float, default: float, integer: bool = False):
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        number = default
    number = max(minimum, min(maximum, number))
    return int(number) if integer else number


# 校验并补全运行参数：与默认值深拷贝合并 + 白名单/范围过滤，输出前端可直接用的干净 dict
def normalize_runtime_config(config: dict) -> dict:
    base = default_runtime_config()
    if not isinstance(config, dict):
        config = {}

    result = json.loads(json.dumps(base))
    for section in ("recognition", "camera", "weight", "tts", "mqtt"):
        if isinstance(config.get(section), dict):
            result[section].update(config[section])

    result["version"] = str(config.get("version", result["version"])).strip() or "1.0.0"
    result["device_id"] = str(config.get("device_id", result["device_id"])).strip() or result["device_id"]

    recognition = result["recognition"]
    recognition["backend"] = "rknn-det"
    recognition["accept_confidence"] = clamp_number(recognition["accept_confidence"], 0.0, 1.0, 0.75)
    recognition["confirm_gap"] = clamp_number(recognition["confirm_gap"], 0.0, 1.0, 0.15)
    recognition["topk"] = clamp_number(recognition["topk"], 1, 10, 3, integer=True)
    recognition["rknn_imgsz"] = clamp_number(recognition["rknn_imgsz"], 64, 1280, 640, integer=True)
    recognition["rknn_layout"] = recognition["rknn_layout"] if recognition["rknn_layout"] in {"nhwc", "nchw"} else "nhwc"
    recognition["rknn_float_input"] = bool(recognition["rknn_float_input"])
    recognition["det_conf"] = clamp_number(recognition["det_conf"], 0.0, 1.0, 0.25)
    recognition["det_iou"] = clamp_number(recognition["det_iou"], 0.0, 1.0, 0.45)
    recognition["det_max"] = clamp_number(recognition["det_max"], 1, 300, 20, integer=True)
    recognition["det_score_sigmoid"] = bool(recognition["det_score_sigmoid"])

    camera = result["camera"]
    camera["device"] = str(camera["device"]).strip() or "0"
    camera["image_output"] = str(camera["image_output"]).strip() or "outputs/current.jpg"
    camera["width"] = clamp_number(camera["width"], 320, 3840, 1920, integer=True)
    camera["height"] = clamp_number(camera["height"], 240, 2160, 1080, integer=True)
    camera["warmup"] = clamp_number(camera["warmup"], 0.0, 10.0, 1.0)

    weight = result["weight"]
    weight["hx711_config"] = str(weight["hx711_config"]).strip() or "config/hx711_scale.json"
    weight["gpio_backend"] = weight["gpio_backend"] if weight["gpio_backend"] in {"gpiod", "sysfs"} else "gpiod"
    weight["samples"] = clamp_number(weight["samples"], 1, 200, 20, integer=True)
    weight["max_deviation"] = clamp_number(weight["max_deviation"], 0.0, 1000000.0, 5000.0)

    tts = result["tts"]
    tts["backend"] = tts["backend"] if tts["backend"] in {"none", "mock", "syn6288"} else "syn6288"
    tts["port"] = str(tts["port"]).strip() or "/dev/ttyS10"
    tts["baudrate"] = clamp_number(tts["baudrate"], 1200, 921600, 9600, integer=True)
    tts["encoding"] = tts["encoding"] if tts["encoding"] in {"gb2312", "gbk", "big5", "unicode"} else "gb2312"
    tts["music"] = clamp_number(tts["music"], 0, 15, 0, integer=True)
    tts["volume"] = clamp_number(tts["volume"], 0, 16, 3, integer=True)
    tts["music_volume"] = clamp_number(tts["music_volume"], 0, 16, 0, integer=True)
    tts["speed"] = clamp_number(tts["speed"], 0, 5, 5, integer=True)

    mqtt = result["mqtt"]
    mqtt["enabled"] = bool(mqtt["enabled"])
    mqtt["optional"] = bool(mqtt["optional"])
    mqtt["config"] = str(mqtt["config"]).strip() or "config/mqtt.json"

    return result


# 补全商品字段：缺字段用默认值，unit 不在白名单回退到"斤"；保证下游 .get 不会 KeyError
def normalize_products(products: dict) -> dict:
    normalized = {}
    for product_id, item in products.items():
        if not isinstance(item, dict):
            continue
        unit = item.get("unit", "斤")
        if unit not in PRODUCT_UNITS:
            unit = "斤"
        normalized[str(product_id)] = {
            "name": item.get("name", product_id),
            "unit": unit,
            "unit_price": float(item.get("unit_price", 0)),
            "voice_name": item.get("voice_name") or item.get("name", product_id),
            "enabled": bool(item.get("enabled", True)),
            "remark": item.get("remark", ""),
            "price_history": item.get("price_history", []),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
        }
    return normalized


# 读商品配置：load_json + normalize_products 两步组合，调用方用一行搞定
def load_products_config(path: Path) -> dict:
    return normalize_products(load_json(path, {}))


# 写商品配置：写之前再过一遍 normalize_products，防止脏数据或缺字段被存进磁盘
def save_products_config(path: Path, products: dict) -> None:
    save_json(path, normalize_products(products))


# 合并旧商品与新商品：未提供的字段保留旧值，价格变化时自动追加一条 history 记录
def update_products_config(path: Path, incoming: dict) -> dict:
    if not isinstance(incoming, dict):
        raise ValueError("Product data must be an object")

    current = load_products_config(path)
    next_products = {}
    now = datetime.now().isoformat(timespec="seconds")

    for product_id, item in incoming.items():
        product_id = str(product_id).strip()
        if not product_id:
            raise ValueError("Product ID cannot be empty")
        if not isinstance(item, dict):
            raise ValueError(f"Product {product_id} must be an object")

        old = current.get(product_id, {})
        old_price = old.get("unit_price")
        new_price = float(item.get("unit_price", 0))
        if new_price < 0:
            raise ValueError(f"Product {product_id} unit_price must be greater than or equal to 0")

        history = list(old.get("price_history", []))
        if old and old_price != new_price:
            history.append({
                "timestamp": now,
                "old_price": old_price,
                "new_price": new_price,
                "operator": "web_dashboard",
            })

        unit = str(item.get("unit", old.get("unit", "斤"))).strip() or "斤"
        if unit not in PRODUCT_UNITS:
            unit = "斤"
        next_products[product_id] = {
            "name": str(item.get("name", old.get("name", product_id))).strip() or product_id,
            "unit": unit,
            "unit_price": new_price,
            "voice_name": str(item.get("voice_name", item.get("name", old.get("voice_name", product_id)))).strip() or product_id,
            "enabled": bool(item.get("enabled", old.get("enabled", True))),
            "remark": str(item.get("remark", old.get("remark", ""))).strip(),
            "price_history": history,
            "created_at": old.get("created_at") or now,
            "updated_at": now,
        }

    save_products_config(path, next_products)
    return next_products


# 读 JSONL（交易/事件）：逐行解析，损坏行用 invalid_record 兜底而非中断
def load_records(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({
                    "transaction_id": f"invalid-line-{line_no}",
                    "timestamp": "",
                    "status": "invalid_record",
                    "error": "JSON decode failed",
                    "raw": line,
                })
    return records


# 取最新一条指定类型事件：可按 event_type 过滤，用于 dashboard 状态卡"最近一次 XX"
def latest_device_event(events_path: Path, event_type: str | None = None) -> dict | None:
    events = load_records(events_path)
    if event_type:
        events = [event for event in events if event.get("event_type") == event_type]
    events = [
        event for event in events
        if parse_timestamp(str(event.get("timestamp", ""))) is not None
    ]
    events.sort(key=lambda event: parse_timestamp(str(event.get("timestamp", ""))) or datetime.min)
    return events[-1] if events else None


# 读事件 JSONL：按类型过滤 + 倒序返回最近 N 条（最新在前），供事件流页面
def load_device_events(path: Path, limit: int = 100, event_type: str = "") -> list[dict]:
    events = load_records(path)
    if event_type:
        events = [event for event in events if str(event.get("event_type", "")) == event_type]
    events.sort(key=lambda event: parse_timestamp(str(event.get("timestamp", ""))) or datetime.min)
    return list(reversed(events[-limit:]))


# 聚合 device_events.jsonl：按类型 / 策略版本计数 + 取最新事件，供 dashboard 顶部状态卡片
def summarize_device_events(path: Path) -> dict:
    events = load_records(path)
    valid_events = [
        event for event in events
        if parse_timestamp(str(event.get("timestamp", ""))) is not None
    ]
    valid_events.sort(key=lambda event: parse_timestamp(str(event.get("timestamp", ""))) or datetime.min)
    type_counts = Counter(str(event.get("event_type", "unknown")) for event in valid_events)
    policy_counts = Counter(
        str(event.get("policy_version", "unknown"))
        for event in valid_events
        if event.get("event_type") == "policy_applied"
    )
    latest = valid_events[-1] if valid_events else None
    latest_policy = latest_device_event(path, "policy_applied")
    return {
        "total_events": len(valid_events),
        "event_type_counts": dict(type_counts),
        "policy_apply_count": type_counts.get("policy_applied", 0),
        "policy_version_counts": dict(policy_counts),
        "latest": latest,
        "latest_policy_applied": latest_policy,
    }


# 整文件写 JSONL：先清空再全量重写，用于"校正交易"等需要全量覆盖的场景（不能 append）
def save_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


# 汇总交易：总笔数 / 总额 / 总重 / 状态分布 / 商品销售排行 / 最新一笔；供 dashboard 总览页
def summarize(records: list[dict]) -> dict:
    status_counts = Counter(record.get("status", "unknown") for record in records)
    product_counts = Counter(record.get("product_name") or record.get("product_id") or "未知" for record in records)
    product_sales = defaultdict(float)
    total_sales = 0.0
    total_weight = 0.0

    for record in records:
        is_final_sale = str(record.get("status", "unknown")) in FINAL_SALE_STATUSES
        price = float(record.get("total_price") or 0) if is_final_sale else 0.0
        weight = float(record.get("weight_g") or 0) if is_final_sale else 0.0
        name = record.get("product_name") or record.get("product_id") or "未知"
        total_sales += price
        total_weight += weight
        product_sales[name] += price

    latest = records[-1] if records else None
    return {
        "total_transactions": len(records),
        "total_sales": round(total_sales, 2),
        "total_weight_g": round(total_weight, 2),
        "status_counts": dict(status_counts),
        "product_counts": dict(product_counts),
        "product_sales": {name: round(value, 2) for name, value in product_sales.items()},
        "latest": latest,
    }


# ISO 时间字符串 → datetime：解析失败返回 None（不抛错，让调用方用 datetime.min 兜底）
def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# 拼装设备在线状态：基于"最近一笔交易"和"最近一次心跳"是否在 5 分钟内，判断 online/offline
def build_device_status(records: list[dict], mqtt_status: dict, events_path: Path | None = None) -> dict:
    valid_records = [
        record for record in records
        if parse_timestamp(str(record.get("timestamp", ""))) is not None
    ]
    valid_records.sort(key=lambda record: parse_timestamp(str(record.get("timestamp", ""))) or datetime.min)
    latest = valid_records[-1] if valid_records else None
    latest_time = parse_timestamp(str(latest.get("timestamp", ""))) if latest else None
    now = datetime.now()
    today = now.date().isoformat()
    today_records = [
        record for record in valid_records
        if str(record.get("timestamp", "")).startswith(today)
    ]
    service_event = None
    service_event_time = None
    if events_path is not None:
        service_events = [
            event for event in load_records(events_path)
            if str(event.get("event_type", "")) in {"service_heartbeat", "service_state"}
            and parse_timestamp(str(event.get("timestamp", ""))) is not None
        ]
        service_events.sort(key=lambda event: parse_timestamp(str(event.get("timestamp", ""))) or datetime.min)
        service_event = service_events[-1] if service_events else None
        service_event_time = parse_timestamp(str(service_event.get("timestamp", ""))) if service_event else None

    online_timeout_seconds = 300
    seconds_since_report = (now - latest_time).total_seconds() if latest_time else None
    seconds_since_heartbeat = (now - service_event_time).total_seconds() if service_event_time else None
    recent_report = bool(
        latest_time is not None
        and seconds_since_report is not None
        and seconds_since_report <= online_timeout_seconds
    )
    recent_heartbeat = bool(
        service_event_time is not None
        and seconds_since_heartbeat is not None
        and seconds_since_heartbeat <= online_timeout_seconds
    )
    online = recent_heartbeat or recent_report
    latest_product = ""
    if latest:
        latest_product = latest.get("product_name") or latest.get("product_id") or ""
    device_id = ""
    if service_event:
        device_id = str(service_event.get("device_id", ""))
    if not device_id and latest:
        device_id = str(latest.get("device_id", ""))

    return {
        "device_id": device_id,
        "online": online,
        "online_status": "online" if online else "offline",
        "recent_report": recent_report,
        "recent_heartbeat": recent_heartbeat,
        "mqtt_connected": bool(mqtt_status.get("connected")),
        "mqtt_host": mqtt_status.get("host", ""),
        "mqtt_port": mqtt_status.get("port", ""),
        "mqtt_topic": mqtt_status.get("topic", ""),
        "last_report_at": latest.get("timestamp", "") if latest else "",
        "last_heartbeat_at": service_event.get("timestamp", "") if service_event else "",
        "seconds_since_report": round(seconds_since_report, 1) if seconds_since_report is not None else None,
        "seconds_since_heartbeat": round(seconds_since_heartbeat, 1) if seconds_since_heartbeat is not None else None,
        "online_timeout_seconds": online_timeout_seconds,
        "today_transactions": len(today_records),
        "today_sales": round(
            sum(
                float(record.get("total_price") or 0)
                for record in today_records
                if str(record.get("status", "unknown")) in FINAL_SALE_STATUSES
            ),
            2,
        ),
        "latest_status": latest.get("status", "") if latest else "",
        "latest_product": latest_product,
        "service_state": service_event.get("service_state", "") if service_event else "",
        "current_weight_g": service_event.get("current_weight_g") if service_event else None,
        "service_event_type": service_event.get("event_type", "") if service_event else "",
        "service_message": service_event.get("message", "") if service_event else "",
        "service_transaction_count": service_event.get("transaction_count", 0) if service_event else 0,
        "record_count": len(valid_records),
        "last_error": mqtt_status.get("last_error", ""),
    }


# 7 日趋势 + 商品销售排行 + 识别质量统计：dashboard 中"分析"标签页所有图表的数据源
def build_analytics(records: list[dict]) -> dict:
    now = datetime.now()
    today = now.date()
    last_7_days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    daily = {
        day: {
            "date": day,
            "transactions": 0,
            "sales": 0.0,
            "weight_g": 0.0,
        }
        for day in last_7_days
    }
    product_stats: dict[str, dict] = {}
    status_counts = Counter()
    recognized_count = 0
    low_quality_count = 0
    total_sales = 0.0
    total_weight = 0.0

    for record in records:
        status = str(record.get("status", "unknown"))
        status_counts[status] += 1
        is_final_sale = status in FINAL_SALE_STATUSES
        price = float(record.get("total_price") or 0) if is_final_sale else 0.0
        weight = float(record.get("weight_g") or 0) if is_final_sale else 0.0
        product_key = record.get("product_name") or record.get("product_id") or "未知"
        total_sales += price
        total_weight += weight

        if status in {"accepted", "manually_confirmed"}:
            recognized_count += 1
        if status in {"low_confidence", "needs_confirm", "unknown", "invalid_record"}:
            low_quality_count += 1

        if is_final_sale:
            product = product_stats.setdefault(product_key, {
                "product": product_key,
                "transactions": 0,
                "sales": 0.0,
                "weight_g": 0.0,
                "share": 0.0,
            })
            product["transactions"] += 1
            product["sales"] += price
            product["weight_g"] += weight

        day = date_prefix(str(record.get("timestamp", "")))
        if day in daily:
            if is_final_sale:
                daily[day]["transactions"] += 1
            daily[day]["sales"] += price
            daily[day]["weight_g"] += weight

    for item in product_stats.values():
        item["sales"] = round(item["sales"], 2)
        item["weight_g"] = round(item["weight_g"], 2)
        item["share"] = round((item["sales"] / total_sales * 100), 2) if total_sales else 0.0

    for item in daily.values():
        item["sales"] = round(item["sales"], 2)
        item["weight_g"] = round(item["weight_g"], 2)

    total_transactions = len(records)
    today_stats = daily.get(today.isoformat(), {"transactions": 0, "sales": 0.0, "weight_g": 0.0})
    return {
        "total_transactions": total_transactions,
        "total_sales": round(total_sales, 2),
        "total_weight_g": round(total_weight, 2),
        "pending_count": sum(status_counts.get(key, 0) for key in ("low_confidence", "needs_confirm", "unknown", "invalid_record")),
        "today_sales": today_stats["sales"],
        "today_transactions": today_stats["transactions"],
        "last_7_days": list(daily.values()),
        "product_ranking": sorted(product_stats.values(), key=lambda item: item["sales"], reverse=True),
        "product_share": sorted(product_stats.values(), key=lambda item: item["share"], reverse=True),
        "recognition_quality": {
            "recognized_count": recognized_count,
            "low_quality_count": low_quality_count,
            "recognized_rate": round((recognized_count / total_transactions * 100), 2) if total_transactions else 0.0,
            "low_quality_rate": round((low_quality_count / total_transactions * 100), 2) if total_transactions else 0.0,
            "status_counts": dict(status_counts),
        },
    }


# 从 URL 查询参数 dict 里取首个值并 strip：处理"空字符串=不过滤"的语义
def first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    return query.get(key, [default])[0].strip()


# 从 ISO 时间戳截前 10 位（YYYY-MM-DD），做日期分组/范围比较用（短于 10 位原样返回）
def date_prefix(value: str) -> str:
    return value[:10] if len(value) >= 10 else value


# 多条件过滤交易：时间范围 / 商品 / 状态，empty 值表示"不过滤"；供交易流水页查询
def filter_records(records: list[dict], query: dict[str, list[str]]) -> list[dict]:
    start_date = first_query_value(query, "start")
    end_date = first_query_value(query, "end")
    product = first_query_value(query, "product")
    status = first_query_value(query, "status")

    filtered = []
    for record in records:
        record_date = date_prefix(str(record.get("timestamp", "")))
        if (start_date or end_date) and not record_date:
            continue
        if start_date and record_date and record_date < start_date:
            continue
        if end_date and record_date and record_date > end_date:
            continue
        if product and product not in {str(record.get("product_id", "")), str(record.get("product_name", ""))}:
            continue
        if status and str(record.get("status", "")) != status:
            continue
        filtered.append(record)
    return filtered


# 按 transaction_id 找记录：倒序遍历，多数场景找的是刚生成的（位于文件尾部）
def find_record(records: list[dict], transaction_id: str) -> dict | None:
    for record in reversed(records):
        if str(record.get("transaction_id", "")) == transaction_id:
            return record
    return None


# 计算交易总价：克 → 斤（除 500）× 单价，保留 2 位小数；与 product_business 的 weight_jin 保持一致
def calculate_total_price(weight_g: float, unit_price: float) -> float:
    return round(weight_g / 500.0 * unit_price, 2)


# 从交易记录抽 7 个关键字段，做"改前/改后"对比快照用（不存大字段如 corrections / raw）
def correction_snapshot(record: dict) -> dict:
    keys = [
        "status",
        "product_id",
        "product_name",
        "unit",
        "unit_price",
        "total_price",
        "voice_text",
        "recognition_source",
    ]
    return {key: record.get(key) for key in keys}


# 人工校正某笔交易：改商品 / 改价 → 重算总价 → 写一份 before/after 快照到 record["corrections"]
def update_transaction_record(records_path: Path, products_path: Path, transaction_id: str, payload: dict) -> dict:
    records = load_records(records_path)
    products = load_products_config(products_path)
    product_id = str(payload.get("product_id", "")).strip()
    operator = str(payload.get("operator", "web_dashboard")).strip() or "web_dashboard"
    note = str(payload.get("note", "")).strip()

    if not transaction_id:
        raise ValueError("transaction_id is required")
    if product_id not in products:
        raise ValueError(f"Unknown product_id: {product_id}")
    if not products[product_id].get("enabled", True):
        raise ValueError(f"Product is disabled: {product_id}")

    unit_price = float(payload.get("unit_price", products[product_id].get("unit_price", 0)))
    if unit_price < 0:
        raise ValueError("unit_price must be greater than or equal to 0")

    updated_record = None
    for record in records:
        if str(record.get("transaction_id", "")) != transaction_id:
            continue

        before = correction_snapshot(record)
        product = dict(products[product_id])
        product["unit_price"] = unit_price
        weight_g = float(record.get("weight_g") or 0)
        total_price = calculate_total_price(weight_g, unit_price)
        record.update({
            "status": "manually_confirmed",
            "product_id": product_id,
            "product_name": product.get("name", product_id),
            "unit": product.get("unit", "斤"),
            "unit_price": unit_price,
            "total_price": total_price,
            "voice_text": build_voice_text(product, float(record.get("confidence") or 0), weight_g, total_price, "accepted"),
            "recognition_source": "manual_confirmation",
        })
        record["product_memory"] = {
            "saved": False,
            "binding": "pending_edge_bind",
            "message": "已在后台完成交易修正，等待板端 bind_memory 命令生成商品记忆。",
        }
        corrections = record.setdefault("corrections", [])
        corrections.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operator": operator,
            "note": note,
            "before": before,
            "after": correction_snapshot(record),
            "product_memory": {
                "saved": False,
                "binding": "pending_edge_bind",
            },
        })
        updated_record = record

    if updated_record is not None:
        save_records(records_path, records)
        return updated_record

    raise ValueError(f"Transaction not found: {transaction_id}")


# 把交易记录列表转 CSV 字节流：带 UTF-8 BOM 头，Excel 直接打开中文不乱码
def records_to_csv(records: list[dict]) -> bytes:
    output = io.StringIO()
    fieldnames = [
        "transaction_id",
        "timestamp",
        "device_id",
        "status",
        "product_id",
        "product_name",
        "confidence",
        "confidence_gap",
        "weight_g",
        "unit",
        "unit_price",
        "total_price",
        "voice_text",
        "source_image",
        "detection_preview_image",
        "correction_count",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = dict(record)
        row["correction_count"] = len(record.get("corrections", []))
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


# 安全解析用户传入的图片路径：阻止 ../ 跳出工作目录 + 限制后缀 + 校验存在
def safe_media_path(raw_path: str) -> Path:
    decoded = unquote(raw_path).replace("\\", "/")
    candidate = (ROOT / decoded).resolve()
    if ROOT not in candidate.parents and candidate != ROOT:
        raise ValueError("Path is outside workspace")
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Unsupported media type")
    if not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


