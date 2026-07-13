"""Transaction record helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from product_business import format_money


# 把金额格式化为字符串；如果值为 None 则返回 "待确认"（用于未确定价格时显示）
def format_optional_money(value) -> str:
    if value is None:
        return "待确认"
    return format_money(float(value))


# 把 dict 序列化为 JSON 字符串并追加一行到 .jsonl 文件（append-only 写入，不读旧内容）
def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


# 把识别结果（recognition dict）拼装成一条完整交易记录（加 transaction_id / timestamp / device_id / 源图）
def build_transaction(recognition: dict, image_path: Path, device_id: str) -> dict:
    return {
        "transaction_id": uuid4().hex,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "device_id": device_id,
        "source_image": str(image_path),
        "status": recognition["status"],
        "product_id": recognition["product_id"],
        "product_name": recognition["product_name"],
        "confidence": recognition["confidence"],
        "confidence_gap": recognition.get("confidence_gap"),
        "weight_g": recognition["weight_g"],
        "unit": recognition["unit"],
        "unit_price": recognition["unit_price"],
        "total_price": recognition["total_price"],
        "voice_text": recognition["voice_text"],
        "top_predictions": recognition["top_predictions"],
        "detections": recognition.get("detections", []),
        "recognition_source": recognition.get("recognition_source", "detector"),
        "memory_match": recognition.get("memory_match"),
        "memory_lookup": recognition.get("memory_lookup"),
        "detector": recognition.get("detector", {}),
    }


# 把交易记录格式化打印为收银小票到 stdout（CLI 输出 + 调试用）
def print_receipt(record: dict, record_path: Path | None) -> None:
    print("== 模拟称重收银 ==")
    print(f"交易ID：{record['transaction_id']}")
    print(f"时间：{record['timestamp']}")
    print(f"设备：{record['device_id']}")
    print(f"识别状态：{record['status']}")
    print(f"商品：{record['product_name']} ({record['product_id']})")
    print(f"置信度：{record['confidence']:.3f}")
    if record.get("confidence_gap") is not None:
        print(f"置信度差值：{float(record['confidence_gap']):.3f}")
    if record.get("weight_g") is not None:
        print(f"重量：{float(record['weight_g']):.0f} 克")
    else:
        print(f"重量：待确认")
    print(f"单价：{format_optional_money(record.get('unit_price'))} 元/{record['unit']}")
    print(f"总价：{format_optional_money(record.get('total_price'))} 元")
    print(f"语音播报：{record['voice_text']}")
    if record.get("detection_preview_image"):
        print(f"检测图：{record['detection_preview_image']}")
    if record_path is not None:
        print(f"交易记录：{record_path}")
    mqtt_result = record.get("mqtt")
    if isinstance(mqtt_result, dict):
        if mqtt_result.get("error"):
            print(f"MQTT上报：失败，{mqtt_result['error']}")
        elif mqtt_result.get("topic"):
            print(f"MQTT上报：成功，{mqtt_result['topic']}")
    print()
    print("Top predictions:")
    for item in record["top_predictions"]:
        print(f"  {item['product_id']}: {item['confidence']:.3f}")
