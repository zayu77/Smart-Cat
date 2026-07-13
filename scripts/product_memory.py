"""Lightweight product memory based on image feature similarity."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np


FEATURE_VERSION = "hsv-grid-v1"
DEFAULT_MEMORY_PATH = Path("records/product_memory.jsonl")
DEFAULT_SIMILARITY_THRESHOLD = 0.88
DEFAULT_GAP_THRESHOLD = 0.03


def resolve_project_path(path: str | Path, root: Path | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (root or Path.cwd()) / candidate


def load_memory_items(path: Path = DEFAULT_MEMORY_PATH) -> list[dict]:
    if not path.exists():
        return []
    items = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                items.append(item)
    return items


def append_memory_item(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def _crop_image(image: np.ndarray, bbox_xyxy: list | tuple | None) -> np.ndarray:
    if not bbox_xyxy or len(bbox_xyxy) != 4:
        return image
    height, width = image.shape[:2]
    x1, y1, x2, y2 = [int(round(float(value))) for value in bbox_xyxy]
    x1 = max(0, min(width - 1, x1))
    x2 = max(0, min(width, x2))
    y1 = max(0, min(height - 1, y1))
    y2 = max(0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return image
    return image[y1:y2, x1:x2]


def extract_image_feature(image_path: Path, bbox_xyxy: list | tuple | None = None) -> list[float]:
    image_data = np.fromfile(str(image_path), dtype=np.uint8)
    image = cv2.imdecode(image_data, cv2.IMREAD_COLOR) if image_data.size else None
    if image is None:
        raise ValueError(f"Failed to read image for product memory: {image_path}")

    crop = _crop_image(image, bbox_xyxy)
    resized = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)

    features: list[np.ndarray] = []
    # 2x2 网格颜色直方图保留粗略空间信息，比整图直方图更不容易混淆背景。
    for y_start in (0, 112):
        for x_start in (0, 112):
            cell = hsv[y_start : y_start + 112, x_start : x_start + 112]
            hist = cv2.calcHist([cell], [0, 1], None, [12, 8], [0, 180, 0, 256]).astype(np.float32).reshape(-1)
            hist /= float(np.linalg.norm(hist) + 1e-8)
            features.append(hist)

    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 160)
    edge_density = np.array([float(edges.mean() / 255.0)], dtype=np.float32)
    vector = np.concatenate([*features, edge_density]).astype(np.float32)
    vector /= float(np.linalg.norm(vector) + 1e-8)
    return [round(float(value), 6) for value in vector.tolist()]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8))


def best_bbox_from_record(record: dict) -> list | None:
    detections = record.get("detections")
    if not isinstance(detections, list) or not detections:
        return None
    best = max(detections, key=lambda item: float(item.get("confidence") or 0) if isinstance(item, dict) else 0)
    if isinstance(best, dict) and isinstance(best.get("bbox_xyxy"), list):
        return best["bbox_xyxy"]
    return None


def add_product_memory(
    memory_path: Path,
    record: dict,
    product_id: str,
    product: dict,
    root: Path | None = None,
) -> dict:
    source_image = record.get("source_image") or record.get("detection_preview_image")
    if not source_image:
        raise ValueError("Transaction has no source image for product memory")
    image_path = resolve_project_path(str(source_image), root=root)
    bbox_xyxy = best_bbox_from_record(record)
    embedding = extract_image_feature(image_path, bbox_xyxy=bbox_xyxy)
    item = {
        "memory_id": uuid4().hex,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "feature_version": FEATURE_VERSION,
        "transaction_id": record.get("transaction_id"),
        "product_id": product_id,
        "product_name": product.get("name", product_id),
        "unit": product.get("unit", "斤"),
        "unit_price": float(product.get("unit_price", 0.0)),
        "source_image": str(source_image),
        "bbox_xyxy": bbox_xyxy,
        "embedding": embedding,
    }
    append_memory_item(memory_path, item)
    return item


def match_product_memory(
    image_path: Path,
    products: dict,
    memory_path: Path = DEFAULT_MEMORY_PATH,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,
) -> dict | None:
    items = load_memory_items(memory_path)
    if not items:
        return None
    query = extract_image_feature(image_path)
    scored_by_product: dict[str, tuple[float, dict]] = {}
    for item in items:
        product_id = str(item.get("product_id") or "")
        if product_id not in products or products[product_id].get("enabled") is False:
            continue
        similarity = cosine_similarity(query, item.get("embedding", []))
        current = scored_by_product.get(product_id)
        if current is None or similarity > current[0]:
            scored_by_product[product_id] = (similarity, item)
    if not scored_by_product:
        return None
    scored = sorted(scored_by_product.values(), key=lambda pair: pair[0], reverse=True)
    best_score, best_item = scored[0]
    second_score = scored[1][0] if len(scored) >= 2 else 0.0
    gap = best_score - second_score
    if best_score < threshold or gap < gap_threshold:
        return None
    product_id = str(best_item["product_id"])
    return {
        "source": "product_memory",
        "status": "matched",
        "product_id": product_id,
        "product_name": products[product_id].get("name", product_id),
        "similarity": round(best_score, 4),
        "similarity_gap": round(gap, 4),
        "memory_id": best_item.get("memory_id"),
        "transaction_id": best_item.get("transaction_id"),
        "feature_version": best_item.get("feature_version", FEATURE_VERSION),
    }
