"""Recognize a product image with an RKNN YOLO detector and attach checkout business data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from predict_rknn_detector import RKNNDetector, predict_rknn_detections
from product_business import build_status, build_voice_text, format_money, load_products


# 同一商品被 YOLO 多次检测到时，只保留置信度最高的那一个（按 product_id 去重）
def best_detection_per_product(detections: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for detection in detections:
        product_id = str(detection.get("product_id") or "")
        confidence = float(detection.get("confidence") or 0)
        current = best.get(product_id)
        if current is None or confidence > float(current.get("confidence") or 0):
            best[product_id] = detection
    return sorted(best.values(), key=lambda item: float(item.get("confidence") or 0), reverse=True)


# 业务层包装：拿 YOLO 检测结果 → 查商品表 → 判断状态 → 算总价 → 生成语音播报文本
def recognize_product_detector_rknn(
    image_path: Path,
    model_path: Path,
    products_path: Path,
    labels_path: Path = Path("models/yolo_product_detector.labels.json"),
    weight_g: float | None = None,
    accept_confidence: float = 0.75,
    confirm_gap: float = 0.15,
    topk: int = 3,
    imgsz: int = 640,
    layout: str = "nhwc",
    float_input: bool = False,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    max_det: int = 20,
    score_sigmoid: bool = False,
    detector: RKNNDetector | None = None,
) -> dict:
    products = load_products(products_path)
    if detector is None:
        detections = predict_rknn_detections(
            model_path=model_path,
            image_path=image_path,
            labels_path=labels_path,
            imgsz=imgsz,
            layout=layout,
            float_input=float_input,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            max_det=max_det,
            score_sigmoid=score_sigmoid,
        )
    else:
        detections = detector.predict_image(
            image_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            max_det=max_det,
        )

    detections = sorted(detections, key=lambda item: float(item.get("confidence") or 0), reverse=True)
    product_detections = best_detection_per_product(detections)
    predictions = [(item["product_id"], float(item["confidence"])) for item in product_detections[:topk]]

    detected_product_id = predictions[0][0] if predictions else None
    confidence = predictions[0][1] if predictions else 0.0
    confidence_gap = None
    if len(predictions) >= 2:
        confidence_gap = confidence - predictions[1][1]
    if not detected_product_id or detected_product_id not in products:
        product_id = "unknown"
        product = {
            "name": "未知商品",
            "voice_name": "未知商品",
            "unit": "斤",
            "unit_price": 0.0,
        }
        status = "unknown"
    else:
        detected_product = products[detected_product_id]
        status = build_status(predictions, accept_confidence, confirm_gap)
        if status == "accepted":
            product_id = detected_product_id
            product = detected_product
        else:
            product_id = "unknown"
            product = {
                "name": "未知商品",
                "voice_name": "未知商品",
                "unit": detected_product.get("unit", "斤"),
                "unit_price": 0.0,
            }

    weight_jin = weight_g / 500.0 if weight_g is not None else None
    total_price = weight_jin * float(product["unit_price"]) if status == "accepted" and weight_jin is not None else None
    voice_text = build_voice_text(product, confidence, weight_g, total_price, status)

    return {
        "status": status,
        "model_backend": "rknn-yolo-det",
        "product_id": product_id,
        "product_name": product.get("name", product_id or "未知商品"),
        "confidence": round(float(confidence), 4),
        "confidence_gap": round(float(confidence_gap), 4) if confidence_gap is not None else None,
        "unit": product.get("unit", "斤"),
        "unit_price": float(product.get("unit_price", 0.0)) if product_id != "unknown" else 0.0,
        "weight_g": weight_g,
        "total_price": round(float(total_price), 2) if total_price is not None else None,
        "voice_text": voice_text,
        "top_predictions": [{"product_id": label, "confidence": round(float(score), 4)} for label, score in predictions],
        "detections": product_detections,
        "detector": {
            "has_box": bool(product_detections),
            "raw_detection_count": len(detections),
            "conf_threshold": conf_threshold,
            "iou_threshold": iou_threshold,
            "max_det": max_det,
            "score_sigmoid": score_sigmoid,
        },
    }


# CLI 入口：单图识别 + 可选重量 → 输出业务结果（status / product / price / voice_text）
def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize product image with an RKNN YOLO detector.")
    parser.add_argument("--input", required=True, help="Input image path.")
    parser.add_argument("--model", default="models/yolo_product_detector.rknn", help="RKNN detector model path.")
    parser.add_argument("--labels", default="models/yolo_product_detector.labels.json", help="Labels JSON path.")
    parser.add_argument("--products", default="config/products.json", help="Product table JSON path.")
    parser.add_argument("--weight-g", type=float, default=None, help="Optional weight in grams.")
    parser.add_argument("--accept-confidence", type=float, default=0.75, help="Minimum confidence for auto accept.")
    parser.add_argument("--confirm-gap", type=float, default=0.15, help="Ask confirmation if Top1-Top2 gap is smaller.")
    parser.add_argument("--topk", type=int, default=3, help="Number of predictions to include.")
    parser.add_argument("--imgsz", type=int, default=640, help="Input image size.")
    parser.add_argument("--layout", choices=["nhwc", "nchw"], default="nhwc", help="Input layout for RKNN runtime.")
    parser.add_argument("--float-input", action="store_true", help="Send float32 0-1 input instead of uint8 input.")
    parser.add_argument("--conf", type=float, default=0.25, help="Detection confidence threshold.")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold.")
    parser.add_argument("--max-det", type=int, default=20, help="Maximum detections after NMS.")
    parser.add_argument("--score-sigmoid", action="store_true", help="Apply sigmoid to output scores before filtering.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    args = parser.parse_args()

    result = recognize_product_detector_rknn(
        image_path=Path(args.input),
        model_path=Path(args.model),
        products_path=Path(args.products),
        labels_path=Path(args.labels),
        weight_g=args.weight_g,
        accept_confidence=args.accept_confidence,
        confirm_gap=args.confirm_gap,
        topk=args.topk,
        imgsz=args.imgsz,
        layout=args.layout,
        float_input=args.float_input,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        max_det=args.max_det,
        score_sigmoid=args.score_sigmoid,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"Image: {args.input}")
    print(f"Model backend: {result['model_backend']}")
    print(f"Status: {result['status']}")
    print(f"Product ID: {result['product_id']}")
    print(f"Product name: {result['product_name']}")
    print(f"Confidence: {result['confidence']:.3f}")
    print(f"Detections: {len(result['detections'])}")
    if result["product_id"]:
        print(f"Unit price: {format_money(result['unit_price'])} yuan/{result['unit']}")
    if result["weight_g"] is not None and result["total_price"] is not None:
        print(f"Weight: {result['weight_g']:.0f} g")
        print(f"Total price: {format_money(result['total_price'])} yuan")
    print(f"Voice text: {result['voice_text']}")
    print()
    print("Top detections:")
    for item in result["detections"][: args.topk]:
        print(f"  {item['product_id']}: {item['confidence']:.3f} box={item['bbox_xyxy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
