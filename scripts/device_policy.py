"""Device policy helpers for remote behavior control."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from product_business import format_money
from transaction_utils import append_jsonl


# 策略字段白名单：low_confidence_action / unknown_product_action / pricing_mode 各自允许的取值
LOW_CONFIDENCE_ACTIONS = {"keep", "accept", "needs_confirm", "reject"}
UNKNOWN_PRODUCT_ACTIONS = {"keep", "needs_confirm", "reject"}
PRICING_MODES = {"standard", "discount_10_over_1000g"}


# 9 字段默认策略：版本号 + 3 套动作/模式 + 3 套语音模板 + enabled 开关
def default_policy() -> dict[str, Any]:
    return {
        "policy_version": "policy-v1.0.0",
        "description": "Default Smart-Cat device policy.",
        "low_confidence_action": "needs_confirm",
        "unknown_product_action": "needs_confirm",
        "pricing_mode": "standard",
        "voice_template": "{product_name}，净重{weight_g}克，单价{unit_price}元每{unit}，总价{total_price}元。",
        "confirm_voice_template": "请人工确认，识别为{product_name}，重量{weight_g}克。",
        "reject_voice_template": "当前商品未能可靠识别，已暂停结算，请人工处理。",
        "enabled": True,
    }


# 读策略 JSON：文件不存在回退 fallback；非 dict 直接抛 ValueError（比 dashboard_data 版本更严）
def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Policy file must be a JSON object: {path}")
    return data


# 写策略：建父目录 + 写前再过一遍 normalize（保证磁盘上的文件总是干净完整）
def save_policy(path: Path, policy: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(normalize_policy(policy), file, ensure_ascii=False, indent=2)
        file.write("\n")


# 3 阶段策略校验：缺字段补默认 → None 字段忽略 → 白名单过滤（非法值回退到默认）
def normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    base = default_policy()
    if not isinstance(policy, dict):
        return base
    result = dict(base)
    result.update({key: value for key, value in policy.items() if value is not None})

    result["policy_version"] = str(result.get("policy_version", "")).strip() or base["policy_version"]
    result["description"] = str(result.get("description", "")).strip()
    if result["low_confidence_action"] not in LOW_CONFIDENCE_ACTIONS:
        result["low_confidence_action"] = base["low_confidence_action"]
    if result["unknown_product_action"] not in UNKNOWN_PRODUCT_ACTIONS:
        result["unknown_product_action"] = base["unknown_product_action"]
    if result["pricing_mode"] not in PRICING_MODES:
        result["pricing_mode"] = base["pricing_mode"]
    for key in ("voice_template", "confirm_voice_template", "reject_voice_template"):
        result[key] = str(result.get(key, base[key])).strip() or base[key]
    result["enabled"] = bool(result.get("enabled", True))
    return result


# 读策略 + normalize 的便捷封装：调用方不用关心 fallback
def load_policy(path: Path) -> dict[str, Any]:
    return normalize_policy(load_json(path, default_policy()))


# 计价：kg 单位换算 + 满 1000g 九折优惠；返回 (total, breakdown) 让上层做审计和展示
def calculate_policy_price(weight_g: float | None, unit_price: float, unit: str, pricing_mode: str) -> tuple[float | None, dict[str, Any]]:
    if weight_g is None:
        return None, {"mode": pricing_mode, "discount": 0.0, "reason": ""}
    if unit == "kg":
        base_price = weight_g / 1000.0 * unit_price
    else:
        base_price = weight_g / 500.0 * unit_price

    discount = 0.0
    reason = ""
    if pricing_mode == "discount_10_over_1000g" and weight_g >= 1000:
        discount = base_price * 0.1
        reason = "满1000克九折"
    total = round(max(0.0, base_price - discount), 2)
    return total, {
        "mode": pricing_mode,
        "base_price": round(base_price, 2),
        "discount": round(discount, 2),
        "reason": reason,
    }


# 安全的字符串模板渲染：字段缺失有兜底 + KeyError 捕获（绝不抛异常让 TTS 失败）
def render_template(template: str, record: dict[str, Any]) -> str:
    values = {
        "product_id": record.get("product_id") or "unknown",
        "product_name": record.get("product_name") or record.get("product_id") or "未知商品",
        "weight_g": f"{float(record.get('weight_g') or 0):.0f}",
        "unit": record.get("unit") or "斤",
        "unit_price": format_money(float(record.get("unit_price") or 0)),
        "total_price": format_money(float(record.get("total_price") or 0)),
        "confidence": f"{float(record.get('confidence') or 0):.3f}",
        "status": record.get("status") or "",
    }
    try:
        return template.format(**values)
    except KeyError:
        return template


# 核心函数：把策略应用到一个交易记录上 → 改 status、重算价格、生成语音、写 audit 痕迹
def apply_policy_to_record(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    policy = normalize_policy(policy)
    original = {
        "status": record.get("status"),
        "total_price": record.get("total_price"),
        "voice_text": record.get("voice_text"),
    }
    if not policy.get("enabled", True):
        record["policy"] = {
            "enabled": False,
            "policy_version": policy["policy_version"],
            "original": original,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        }
        return record

    status = str(record.get("status") or "unknown")
    action = "keep"
    if status == "low_confidence":
        action = policy["low_confidence_action"]
    elif status == "unknown":
        action = policy["unknown_product_action"]

    if action == "accept":
        record["status"] = "accepted"
    elif action == "needs_confirm":
        record["status"] = "needs_confirm"
    elif action == "reject":
        record["status"] = "rejected"

    if record["status"] not in {"accepted", "memory_matched"} or str(record.get("product_id") or "") == "unknown":
        total_price = None
        pricing = {
            "mode": "pending_confirmation",
            "base_price": None,
            "discount": 0.0,
            "reason": "not accepted",
        }
    else:
        total_price, pricing = calculate_policy_price(
            float(record.get("weight_g")) if record.get("weight_g") is not None else None,
            float(record.get("unit_price") or 0),
            str(record.get("unit") or "斤"),
            policy["pricing_mode"],
        )
    record["total_price"] = total_price
    record["pricing"] = pricing

    if record["status"] == "rejected":
        record["voice_text"] = render_template(policy["reject_voice_template"], record)
    elif record["status"] in {"needs_confirm", "low_confidence", "unknown"}:
        record["voice_text"] = render_template(policy["confirm_voice_template"], record)
    else:
        record["voice_text"] = render_template(policy["voice_template"], record)

    record["policy"] = {
        "enabled": True,
        "policy_version": policy["policy_version"],
        "pricing_mode": policy["pricing_mode"],
        "low_confidence_action": policy["low_confidence_action"],
        "unknown_product_action": policy["unknown_product_action"],
        "selected_action": action,
        "original": original,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
    }
    return record


# 构造结构化设备事件：17 位时间戳（到微秒）保 event_id 唯一 + 必带 policy_version 用于审计
def build_device_event(
    event_type: str,
    device_id: str,
    policy: dict[str, Any],
    status: str = "success",
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "event_id": f"{event_type}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        "event_type": event_type,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "device_id": device_id,
        "status": status,
        "message": message,
        "policy_version": normalize_policy(policy).get("policy_version"),
    }
    if extra:
        event.update(extra)
    return event


# 设备事件 JSONL 追加的薄封装：复用 transaction_utils.append_jsonl 保持格式一致
def append_device_event(path: Path, event: dict[str, Any]) -> None:
    append_jsonl(path, event)
