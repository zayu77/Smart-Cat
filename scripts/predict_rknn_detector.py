"""RKNN YOLO 检测单图推理和 NMS 后处理"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


# S 形激活函数：把任意实数压到 (0, 1)（YOLO 类别概率常用）
def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-values))


# 从 JSON 读类别名称（支持 list 格式和 {0: "name"} 字典格式）
def load_labels(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    names = data.get("names", data)
    if isinstance(names, list):
        return [str(name) for name in names]
    if isinstance(names, dict):
        return [str(names[key]) for key in sorted(names, key=lambda value: int(value))]
    raise ValueError(f"Unsupported labels format: {path}")


# 读图 → BGR 转 RGB → 缩放到 imgsz×imgsz → 可选归一化 → 可选 NHWC/NCHW 转换
def preprocess_image(image_path: Path, imgsz: int, layout: str, float_input: bool) -> tuple[np.ndarray, tuple[int, int]]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")

    original_h, original_w = image.shape[:2]
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (imgsz, imgsz), interpolation=cv2.INTER_LINEAR)
    if float_input:
        image = image.astype(np.float32) / 255.0

    if layout == "nchw":
        image = np.transpose(image, (2, 0, 1))

    return np.expand_dims(image, axis=0), (original_w, original_h)


# 把不同 YOLO 版本的输出形状统一为 [N, 4+nc] 或 [N, 5+nc]（4/5 是 box 部分）
def normalize_yolo_output(output: np.ndarray, class_count: int) -> np.ndarray:
    values = np.asarray(output)
    values = np.squeeze(values)
    if values.ndim != 2:
        raise RuntimeError(f"Unsupported YOLO output shape: {np.asarray(output).shape}")

    expected_without_obj = 4 + class_count
    expected_with_obj = 5 + class_count
    if values.shape[0] in {expected_without_obj, expected_with_obj}:
        values = values.T
    if values.shape[1] not in {expected_without_obj, expected_with_obj}:
        raise RuntimeError(f"Unsupported YOLO output shape after reshape: {values.shape}")
    return values.astype(np.float32)


# 把 [cx, cy, w, h] 中心点格式转 [x1, y1, x2, y2] 对角线格式 + 缩放回原图坐标
def xywh_to_xyxy(boxes: np.ndarray, original_size: tuple[int, int], imgsz: int) -> np.ndarray:
    original_w, original_h = original_size
    boxes = boxes.astype(np.float32).copy()

    if np.nanmax(boxes) <= 2.0:
        x_scale = float(original_w)
        y_scale = float(original_h)
    else:
        x_scale = float(original_w) / float(imgsz)
        y_scale = float(original_h) / float(imgsz)

    cx = boxes[:, 0] * x_scale
    cy = boxes[:, 1] * y_scale
    bw = boxes[:, 2] * x_scale
    bh = boxes[:, 3] * y_scale

    xyxy = np.stack(
        [
            cx - bw / 2.0,
            cy - bh / 2.0,
            cx + bw / 2.0,
            cy + bh / 2.0,
        ],
        axis=1,
    )
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, original_w - 1)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, original_h - 1)
    return xyxy


# 计算一个框和一组框的 IoU（交并比），NMS 算法的核心
def box_iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)

    box_area = np.maximum(0.0, box[2] - box[0]) * np.maximum(0.0, box[3] - box[1])
    boxes_area = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0.0, boxes[:, 3] - boxes[:, 1])
    union = box_area + boxes_area - inter
    return inter / np.maximum(union, 1e-6)


# 非极大值抑制：按置信度排序，每轮保留最高分框，剔除 IoU 超过阈值的重叠框
def nms_indices(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    order = np.argsort(scores)[::-1]
    keep: list[int] = []
    while order.size > 0:
        index = int(order[0])
        keep.append(index)
        if order.size == 1:
            break
        ious = box_iou(boxes[index], boxes[order[1:]])
        order = order[1:][ious <= iou_threshold]
    return keep


# YOLO 完整后处理：标准化输出 → 取最佳类别 → 置信度过滤 → NMS → 输出结构化结果
def decode_detections(
    output: np.ndarray,
    labels: list[str],
    original_size: tuple[int, int],
    imgsz: int,
    conf_threshold: float,
    iou_threshold: float,
    max_det: int,
    score_sigmoid: bool = False,
) -> list[dict]:
    predictions = normalize_yolo_output(output, len(labels))
    boxes_xywh = predictions[:, :4]
    score_values = predictions[:, 4:]

    if score_values.shape[1] == len(labels) + 1:
        objectness = score_values[:, :1]
        class_scores = score_values[:, 1:]
        if score_sigmoid:
            objectness = sigmoid(objectness)
            class_scores = sigmoid(class_scores)
        class_scores = objectness * class_scores
    else:
        class_scores = sigmoid(score_values) if score_sigmoid else score_values

    class_ids = np.argmax(class_scores, axis=1)
    scores = class_scores[np.arange(class_scores.shape[0]), class_ids]
    mask = scores >= conf_threshold
    if not np.any(mask):
        return []

    boxes = xywh_to_xyxy(boxes_xywh[mask], original_size=original_size, imgsz=imgsz)
    scores = scores[mask]
    class_ids = class_ids[mask]

    keep = nms_indices(boxes, scores, iou_threshold=iou_threshold)[:max_det]
    detections = []
    for index in keep:
        class_id = int(class_ids[index])
        x1, y1, x2, y2 = boxes[index].tolist()
        detections.append(
            {
                "product_id": labels[class_id],
                "class_id": class_id,
                "confidence": round(float(scores[index]), 4),
                "bbox_xyxy": [round(float(x1), 2), round(float(y1), 2), round(float(x2), 2), round(float(y2), 2)],
            }
        )
    return detections


# 调试用：输出 YOLO 原始张量的统计信息 + Top-10 候选框（用于调阈值/排查模型）
def debug_yolo_output(output: np.ndarray, labels: list[str], score_sigmoid: bool) -> dict:
    predictions = normalize_yolo_output(output, len(labels))
    boxes = predictions[:, :4]
    score_values = predictions[:, 4:]
    has_objectness = score_values.shape[1] == len(labels) + 1
    if has_objectness:
        objectness = score_values[:, :1]
        class_scores = score_values[:, 1:]
        if score_sigmoid:
            objectness = sigmoid(objectness)
            class_scores = sigmoid(class_scores)
        class_scores = objectness * class_scores
    else:
        class_scores = sigmoid(score_values) if score_sigmoid else score_values

    best_class_ids = np.argmax(class_scores, axis=1)
    best_scores = class_scores[np.arange(class_scores.shape[0]), best_class_ids]
    order = np.argsort(best_scores)[::-1][:10]
    return {
        "raw_shape": list(np.asarray(output).shape),
        "normalized_shape": list(predictions.shape),
        "box_min": float(np.nanmin(boxes)),
        "box_max": float(np.nanmax(boxes)),
        "score_min": float(np.nanmin(class_scores)),
        "score_max": float(np.nanmax(class_scores)),
        "has_objectness": has_objectness,
        "top_candidates": [
            {
                "product_id": labels[int(best_class_ids[index])],
                "class_id": int(best_class_ids[index]),
                "score": float(best_scores[index]),
                "box_xywh": [float(value) for value in boxes[index].tolist()],
            }
            for index in order
        ],
    }


# RKNN 检测器封装（适用于 Rockchip NPU 板子，如鲁班猫 RK3576）
class RKNNDetector:
    # 加载 RKNN 模型到 NPU，初始化运行时环境
    def __init__(
        self,
        model_path: Path,
        labels_path: Path,
        imgsz: int = 640,
        layout: str = "nhwc",
        float_input: bool = False,
        score_sigmoid: bool = False,
    ) -> None:
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise SystemExit(
                "rknn-toolkit-lite2 is not installed on this board. Install the matching RKNN Lite2 package first."
            ) from exc

        if not model_path.exists():
            raise FileNotFoundError(f"RKNN model not found: {model_path}")

        self.model_path = model_path
        self.labels_path = labels_path
        self.imgsz = imgsz
        self.layout = layout
        self.float_input = float_input
        self.score_sigmoid = score_sigmoid
        self.labels = load_labels(labels_path)
        self.rknn = RKNNLite()
        self.closed = False

        ret = self.rknn.load_rknn(str(model_path))
        if ret != 0:
            self.close()
            raise RuntimeError(f"load_rknn failed: {ret}")

        ret = self.rknn.init_runtime()
        if ret != 0:
            self.close()
            raise RuntimeError(f"init_runtime failed: {ret}")

    # 单图推理：预处理 → NPU 推理 → 后处理 → 返回结构化检测结果
    def predict_image(
        self,
        image_path: Path,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        max_det: int = 20,
    ) -> list[dict]:
        if self.closed:
            raise RuntimeError("RKNNDetector has been closed")
        input_data, original_size = preprocess_image(
            image_path,
            imgsz=self.imgsz,
            layout=self.layout,
            float_input=self.float_input,
        )
        outputs = self.rknn.inference(inputs=[input_data])
        if not outputs:
            raise RuntimeError("RKNN returned no outputs.")
        return decode_detections(
            outputs[0],
            labels=self.labels,
            original_size=original_size,
            imgsz=self.imgsz,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            max_det=max_det,
            score_sigmoid=self.score_sigmoid,
        )

    # 仅返回 NPU 原始输出张量（不做后处理），用于 debug/调阈值
    def raw_output(self, image_path: Path):
        if self.closed:
            raise RuntimeError("RKNNDetector has been closed")
        input_data, _original_size = preprocess_image(
            image_path,
            imgsz=self.imgsz,
            layout=self.layout,
            float_input=self.float_input,
        )
        outputs = self.rknn.inference(inputs=[input_data])
        if not outputs:
            raise RuntimeError("RKNN returned no outputs.")
        return outputs[0]

    # 释放 NPU 资源（RKNNLite 不会自动释放，必须手动 close）
    def close(self) -> None:
        if self.closed:
            return
        try:
            self.rknn.release()
        finally:
            self.closed = True

    # 上下文管理器入口（支持 `with RKNNDetector(...) as d:` 写法）
    def __enter__(self):
        return self

    # 上下文管理器退出时自动 close（防止 NPU 资源泄漏）
    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


# 一次性函数：内部创建并自动关闭 RKNNDetector，适合脚本调用（不必手动管 close）
def predict_rknn_detections(
    model_path: Path,
    image_path: Path,
    labels_path: Path,
    imgsz: int = 640,
    layout: str = "nhwc",
    float_input: bool = False,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    max_det: int = 20,
    score_sigmoid: bool = False,
) -> list[dict]:
    with RKNNDetector(
        model_path=model_path,
        labels_path=labels_path,
        imgsz=imgsz,
        layout=layout,
        float_input=float_input,
        score_sigmoid=score_sigmoid,
    ) as detector:
        return detector.predict_image(
            image_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            max_det=max_det,
        )


# 把检测结果画到原图上（绿框 + 类别名 + 置信度），保存为预览图
def save_preview(image_path: Path, detections: list[dict], output_path: Path) -> None:
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")
    for detection in detections:
        x1, y1, x2, y2 = [int(round(value)) for value in detection["bbox_xyxy"]]
        label = f"{detection['product_id']} {detection['confidence']:.2f}"
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(image, label, (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"Failed to save preview: {output_path}")


# CLI 入口：单图识别、可选画框、可选打印 raw 调试信息、可选 JSON 输出
def main() -> int:
    parser = argparse.ArgumentParser(description="Predict product boxes with an RKNN YOLO detector.")
    parser.add_argument("--input", required=True, help="Input image path.")
    parser.add_argument("--model", default="models/yolo_product_detector.rknn", help="RKNN detector model path.")
    parser.add_argument("--labels", default="models/yolo_product_detector.labels.json", help="Labels JSON path.")
    parser.add_argument("--imgsz", type=int, default=640, help="Input image size.")
    parser.add_argument("--layout", choices=["nhwc", "nchw"], default="nhwc", help="Input layout for RKNN runtime.")
    parser.add_argument("--float-input", action="store_true", help="Send float32 0-1 input instead of uint8 input.")
    parser.add_argument("--conf", type=float, default=0.25, help="Detection confidence threshold.")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold.")
    parser.add_argument("--max-det", type=int, default=20, help="Maximum detections after NMS.")
    parser.add_argument("--score-sigmoid", action="store_true", help="Apply sigmoid to output scores before filtering.")
    parser.add_argument("--preview", default="", help="Optional output image with boxes drawn.")
    parser.add_argument("--debug-output", action="store_true", help="Print raw YOLO output statistics for threshold/debug tuning.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args()

    detections = predict_rknn_detections(
        model_path=Path(args.model),
        image_path=Path(args.input),
        labels_path=Path(args.labels),
        imgsz=args.imgsz,
        layout=args.layout,
        float_input=args.float_input,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        max_det=args.max_det,
        score_sigmoid=args.score_sigmoid,
    )
    if args.preview:
        save_preview(Path(args.input), detections, Path(args.preview))

    debug_info = None
    if args.debug_output:
        with RKNNDetector(
            model_path=Path(args.model),
            labels_path=Path(args.labels),
            imgsz=args.imgsz,
            layout=args.layout,
            float_input=args.float_input,
            score_sigmoid=args.score_sigmoid,
        ) as detector:
            debug_info = debug_yolo_output(detector.raw_output(Path(args.input)), detector.labels, args.score_sigmoid)

    if args.json:
        print(json.dumps({"image": args.input, "model": args.model, "detections": detections, "debug": debug_info}, ensure_ascii=False, indent=2))
        return 0

    print(f"Image: {args.input}")
    print(f"Model: {args.model}")
    print(f"Detections: {len(detections)}")
    for detection in detections:
        print(
            f"  {detection['product_id']}: {detection['confidence']:.3f} "
            f"box={detection['bbox_xyxy']}"
        )
    if args.preview:
        print(f"Preview: {args.preview}")
    if debug_info is not None:
        print()
        print("Debug output:")
        print(json.dumps(debug_info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
