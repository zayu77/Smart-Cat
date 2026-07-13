from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

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
from product_memory import add_product_memory, match_product_memory
from recognize_product_detector_rknn import recognize_product_detector_rknn
from transaction_utils import append_jsonl, build_transaction
from voice_command_mqtt import run_bind_memory


def write_test_image(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(path.suffix or ".jpg", image)
    assert ok
    encoded.tofile(str(path))


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


def test_product_memory_add_and_match(tmp_path: Path):
    image_path = tmp_path / "tomato.jpg"
    image = np.full((120, 120, 3), (20, 20, 220), dtype=np.uint8)
    write_test_image(image_path, image)
    memory_path = tmp_path / "product_memory.jsonl"
    product = {"name": "番茄", "unit": "斤", "unit_price": 5.8, "enabled": True}
    record = {
        "transaction_id": "tx-memory",
        "source_image": str(image_path),
        "detections": [],
    }

    item = add_product_memory(memory_path, record, "tomato", product)
    assert item["product_id"] == "tomato"
    assert memory_path.exists()

    match = match_product_memory(
        image_path,
        {"tomato": product},
        memory_path=memory_path,
        threshold=0.8,
        gap_threshold=0.0,
    )
    assert match
    assert match["product_id"] == "tomato"
    assert match["source"] == "product_memory"


def test_recognition_uses_product_memory_when_detector_unknown(tmp_path: Path):
    image_path = tmp_path / "pear.jpg"
    image = np.full((120, 120, 3), (30, 180, 80), dtype=np.uint8)
    write_test_image(image_path, image)

    products_path = tmp_path / "products.json"
    products_path.write_text(
        json.dumps({"pear": {"name": "梨", "unit": "斤", "unit_price": 6.0, "enabled": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    memory_path = tmp_path / "product_memory.jsonl"
    add_product_memory(
        memory_path,
        {"transaction_id": "seed", "source_image": str(image_path), "detections": []},
        "pear",
        {"name": "梨", "unit": "斤", "unit_price": 6.0, "enabled": True},
    )

    class EmptyDetector:
        def predict_image(self, *args, **kwargs):
            return []

    result = recognize_product_detector_rknn(
        image_path=image_path,
        model_path=tmp_path / "unused.rknn",
        products_path=products_path,
        labels_path=tmp_path / "unused.labels.json",
        weight_g=500,
        detector=EmptyDetector(),
        memory_path=memory_path,
        memory_threshold=0.8,
        memory_gap=0.0,
    )
    assert result["status"] == "memory_matched"
    assert result["product_id"] == "pear"
    assert result["total_price"] == 6.0
    assert result["recognition_source"] == "product_memory"


def test_bind_memory_command_creates_edge_memory(tmp_path: Path):
    image_path = tmp_path / "orange.jpg"
    image = np.full((120, 120, 3), (30, 120, 240), dtype=np.uint8)
    write_test_image(image_path, image)
    records_path = tmp_path / "transactions.jsonl"
    products_path = tmp_path / "products.json"
    memory_path = tmp_path / "product_memory.jsonl"
    products_path.write_text(
        json.dumps({"orange": {"name": "橙子", "unit": "斤", "unit_price": 7.8, "enabled": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    append_jsonl(records_path, {
        "transaction_id": "tx-bind",
        "timestamp": "2026-07-13T10:00:00",
        "source_image": str(image_path),
        "detections": [],
    })

    class Args:
        records = str(records_path)
        products = str(products_path)
        product_memory = str(memory_path)

    code, response, extra = run_bind_memory(Args, {"transaction_id": "tx-bind", "product_id": "orange"})
    assert code == 0
    assert "橙子" in response
    assert extra["product_id"] == "orange"
    assert memory_path.exists()
    saved = [json.loads(line) for line in memory_path.read_text(encoding="utf-8").splitlines()]
    assert saved[0]["transaction_id"] == "tx-bind"
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["product_memory"]["saved"] is True
