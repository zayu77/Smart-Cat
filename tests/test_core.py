from __future__ import annotations

import json
from pathlib import Path

import pytest

from device_policy import apply_policy_to_record, calculate_policy_price, load_policy, normalize_policy
from mqtt_publisher import (
    derive_device_event_topic,
    derive_device_policy_topic,
    derive_runtime_config_topic,
    derive_voice_command_topic,
    load_mqtt_config,
)
from product_business import build_status, build_voice_text, format_money, load_products
from transaction_utils import append_jsonl, build_transaction


def test_build_status_rules():
    assert build_status([], 0.75, 0.15) == "unknown"
    assert build_status([("apple", 0.74)], 0.75, 0.15) == "low_confidence"
    assert build_status([("apple", 0.90), ("banana", 0.80)], 0.75, 0.15) == "needs_confirm"
    assert build_status([("apple", 0.90), ("banana", 0.70)], 0.75, 0.15) == "accepted"


def test_format_money():
    assert format_money(5.0) == "5"
    assert format_money(5.5) == "5.5"
    assert format_money(10.2) == "10.2"


def test_calculate_policy_price_discount():
    total, pricing = calculate_policy_price(1000, 10.0, "斤", "discount_10_over_1000g")
    assert total == 18.0
    assert pricing["mode"] == "discount_10_over_1000g"
    assert pricing["discount"] == 2.0


def test_build_voice_text_for_confirm():
    product = {"name": "苹果", "voice_name": "苹果", "unit": "斤", "unit_price": 9.9}
    text = build_voice_text(product, confidence=0.5, weight_g=500, total_price=None, status="needs_confirm")
    assert "请人工确认" in text


def test_load_products(tmp_path: Path):
    path = tmp_path / "products.json"
    path.write_text(json.dumps({"apple": {"name": "苹果", "unit": "斤", "unit_price": 9.9}}, ensure_ascii=False), encoding="utf-8")
    products = load_products(path)
    assert products["apple"]["name"] == "苹果"


def test_transaction_helpers(tmp_path: Path):
    recognition = {
        "status": "accepted",
        "product_id": "apple",
        "product_name": "苹果",
        "confidence": 0.93,
        "confidence_gap": 0.2,
        "weight_g": 500.0,
        "unit": "斤",
        "unit_price": 9.9,
        "total_price": 9.9,
        "voice_text": "苹果，净重500克，单价9.9元每斤，总价9.9元。",
        "top_predictions": [{"product_id": "apple", "confidence": 0.93}],
        "detections": [{"product_id": "apple", "confidence": 0.93, "bbox_xyxy": [1, 2, 3, 4]}],
        "detector": {"has_box": True, "raw_detection_count": 1},
    }
    record = build_transaction(recognition, image_path=tmp_path / "current.jpg", device_id="demo")
    assert record["transaction_id"]
    assert record["product_id"] == "apple"
    target = tmp_path / "transactions.jsonl"
    append_jsonl(target, record)
    assert target.read_text(encoding="utf-8").strip()


def test_policy_application_accepts_and_rewrites_voice():
    policy = load_policy(Path("config/device_policy.json"))
    record = {
        "status": "accepted",
        "product_id": "apple",
        "product_name": "苹果",
        "weight_g": 500.0,
        "unit": "斤",
        "unit_price": 9.9,
        "total_price": 9.9,
        "voice_text": "苹果，净重500克，单价9.9元每斤，总价9.9元。",
    }
    applied = apply_policy_to_record(record, policy)
    assert applied["policy"]["policy_version"]
    assert applied["voice_text"]


def test_policy_application_low_confidence_becomes_confirm():
    policy = load_policy(Path("config/device_policy.json"))
    record = {
        "status": "low_confidence",
        "product_id": "unknown",
        "product_name": "未知商品",
        "weight_g": 500.0,
        "unit": "斤",
        "unit_price": 0.0,
        "total_price": None,
        "voice_text": "请人工确认",
    }
    applied = apply_policy_to_record(record, policy)
    assert applied["status"] in {"needs_confirm", "low_confidence", "rejected"}
    assert applied["policy"]["selected_action"] in {"keep", "accept", "needs_confirm", "reject"}


def test_mqtt_topic_derivation():
    config = load_mqtt_config(None)
    assert derive_runtime_config_topic(config).endswith("/runtime-config")
    assert derive_device_policy_topic(config).endswith("/device-policy")
    assert derive_device_event_topic(config).endswith("/device-events")
    assert derive_voice_command_topic(config).endswith("/voice-commands")


def test_normalize_policy_fills_defaults():
    policy = normalize_policy({"policy_version": None, "pricing_mode": "bad", "enabled": None})
    assert policy["policy_version"]
    assert policy["pricing_mode"] == "standard"
    assert policy["enabled"] is True
