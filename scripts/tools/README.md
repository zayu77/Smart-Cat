# tools

这里放当前检测模型路线仍在使用的训练、数据集、模型转换和环境检查工具。这些工具不参与板端实时交易主流程，但在采集检测数据、训练 YOLO11 检测模型、导出 ONNX、转换 RKNN 和排查环境时仍然有用。

```text
capture_detection_dataset.py  摄像头采集检测图片，并用空秤背景差分自动生成 YOLO 框标签
detect_dataset_report.py      检查 YOLO 检测数据集图片/标签配对和类别分布
train_yolo_detector.py        训练 YOLO 检测模型
export_yolo_detector_onnx.py  导出 YOLO 检测 ONNX
make_rknn_quant_dataset.py    生成 RKNN 量化图片列表
convert_onnx_to_rknn.py       ONNX 转 RKNN
check_env.py                  环境检查
image_utils.py                数据集工具函数
```

早期分类模型路线工具不再保留在当前最终提交目录中，相关演进过程见 `../../docs/开发日志.md`。

## 检测数据自动标注采集

用于从分类模型升级到 YOLO 检测模型时快速生成初始框标注。适合摄像头固定、秤台背景固定、一次只放一个水果的场景。

第一步，空秤状态下采集背景图：

```bash
python scripts/tools/capture_detection_dataset.py \
  --save-background outputs/empty_scale_background.jpg \
  --device 0 \
  --width 1920 \
  --height 1080
```

第二步，放上某一类水果，自动采集图片并生成 YOLO 标签：

```bash
python scripts/tools/capture_detection_dataset.py \
  --class-name apple \
  --background outputs/empty_scale_background.jpg \
  --count 80 \
  --manual \
  --save-preview
```

输出目录：

```text
yolov11model-train/dataset_det/images/<class_name>/*.jpg
yolov11model-train/dataset_det/labels/<class_name>/*.txt
yolov11model-train/dataset_det/previews/<class_name>/*.jpg
```

`previews` 中会画出自动生成的检测框，正式训练前建议快速抽查一遍，明显框错的样本直接删除或人工修正。

开启 `--manual` 后，每次按 Enter 才会拍一张。每拍一张都会同时生成：

```text
1 张原始图片
1 个 YOLO txt 标签
1 张带框预览图（使用 --save-preview 时）
```

## YOLO 检测模型训练

已有标准 YOLO 检测数据集时，先检查数据：

```bash
python scripts/tools/detect_dataset_report.py --data yolov11model-train/product_yolov11/data.yaml
```

训练 YOLO11 检测模型：

```bash
python scripts/tools/train_yolo_detector.py \
  --data yolov11model-train/product_yolov11/data.yaml \
  --model yolo11n.pt \
  --epochs 80 \
  --imgsz 640 \
  --batch 16 \
  --name product_yolo11_det
```

导出 ONNX：

```bash
python scripts/tools/export_yolo_detector_onnx.py \
  --model models/yolo_product_detector.pt \
  --output models/yolo_product_detector.onnx \
  --labels-output models/yolo_product_detector.labels.json \
  --imgsz 640
```

当前 `yolov11model-train/product_yolov11` 数据集类别为：

```text
0 apple
1 banana
2 orange
```

如果后续要检测 `pear` 和 `tomato`，需要补充对应检测图片和标签，并更新 `data.yaml` 的 `names`。

