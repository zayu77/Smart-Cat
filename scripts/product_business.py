"""Shared product lookup and receipt text helpers."""

from __future__ import annotations

import json
from pathlib import Path


# 从 JSON 文件加载商品表（原样返回，不做字段转换）
def load_products(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


# 根据 Top-1 置信度 + Top-1/Top-2 差距判定业务状态（4 种之一：unknown/low_confidence/needs_confirm/accepted）
def build_status(predictions: list[tuple[str, float]], accept: float, confirm_gap: float) -> str:
    if not predictions:
        return "unknown"
    top_confidence = predictions[0][1]
    if top_confidence < accept:
        return "low_confidence"
    if len(predictions) >= 2 and top_confidence - predictions[1][1] < confirm_gap:
        return "needs_confirm"
    return "accepted"


# 把数字格式化成金额字符串：去尾部 0 + 去尾部点（5.50→"5.5", 5.00→"5", 10.20→"10.2"）
def format_money(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


# 根据业务状态生成 TTS 中文播报文本（不同状态播报不同内容）
def build_voice_text(product: dict, confidence: float, weight_g: float | None, total_price: float | None, status: str) -> str:
    name = product.get("voice_name") or product.get("name")
    unit_price = format_money(float(product["unit_price"]))
    unit = product.get("unit", "斤")

    if status == "needs_confirm":
        return "识别结果不太确定，请人工确认。"
    if status == "low_confidence":
        return "未能可靠识别商品，请人工确认。"
    if status == "unknown":
        return "未检测到有效商品，请人工确认。"
    if status == "rejected":
        return "当前商品未能可靠识别，已暂停结算，请人工处理。"

    if weight_g is None or total_price is None:
        return f"{name}，单价{unit_price}元每{unit}。"
    return f"{name}，净重{weight_g:.0f}克，单价{unit_price}元每{unit}，总价{format_money(total_price)}元。"
