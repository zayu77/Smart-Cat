# scripts 目录说明

本目录按照当前项目状态分为两类：核心主链路、训练与部署工具。

## 核心主链路

这些文件直接参与当前板端运行、Web 后台、MQTT 同步和交易闭环。

```text
smart_scale_demo.py       板端主流程：拍照、识别、称重、播报、记录、上报
smart_scale_service.py    常驻称重触发服务：空秤待机、稳定判断、自动交易
voice_accessibility.py    语音补盲命令层：状态、重量、价格、最近交易播报
voice_command_mqtt.py     MQTT 语音补盲命令监听：Web/手机下发命令后在板端执行播报
mqtt_runtime_config.py    板端拉取 MQTT retained 运行参数
mqtt_publisher.py         MQTT JSON 发布、交易上报、参数 topic 推导
device_policy.py          设备策略校验、策略计价、语音模板和应用事件
web_dashboard.py          Web 后台：交易、商品、设备、统计、参数下发
dashboard_data.py         Web 后台数据层：记录、商品、统计、修正、CSV
dashboard_mqtt.py         Web 后台 MQTT 层：交易同步、运行参数下发
hx711_reader.py           HX711 称重读取、滤波、校准
predict_rknn_detector.py  RKNN YOLO 检测单图推理和 NMS 后处理
recognize_product_detector_rknn.py RKNN YOLO 检测业务封装
product_business.py       商品表、状态判断、计价和播报文本
transaction_utils.py      交易记录构建、JSONL 写入、终端收据
tts_output.py             语音输出适配层
syn6288_tts.py            SYN6288 串口 TTS 协议
camera_test.py            摄像头设备解析和拍照测试
```

板端入口在项目根目录的 `bin/` 下：

```text
../bin/run_smart_scale.sh
../bin/run_smart_scale_service.sh
../bin/run_voice_command.sh
../bin/run_voice_command_mqtt.sh
```

`bin/run_smart_scale.sh` 用于单次调试和演示；`bin/run_smart_scale_service.sh` 用于实际部署时常驻运行。常驻服务会在空秤待机时同步远程参数和策略，预加载 RKNN predictor，并保持摄像头打开；检测到物体放上秤并稳定后，先刷新摄像头缓冲帧，再使用预加载 RKNN 实例完成交易。交易结束后释放本次 RKNN，下一轮空秤时再重新预加载；摄像头继续保持打开，除非设备号或分辨率配置发生变化。需要回退完全隔离的子进程模式时可加 `--subprocess-transaction`。

当前固定识别后端是 `rknn-det`，使用 `models/yolo_product_detector.rknn` 做 YOLO 检测。检测到 `apple`、`banana`、`orange` 框且置信度达到阈值时自动计价；低置信度或没有检测框时进入待确认，并可通过手机/Web 语音补盲修正为番茄、梨等商品。Web 后台不再开放识别后端切换，板端同步远程运行参数时也会把早期旧配置归一为 `rknn-det`。

`bin/run_voice_command.sh` 用于执行语音补盲命令，当前可播报设备状态、当前重量、最近交易、价格和待确认状态。

`bin/run_voice_command_mqtt.sh` 用于常驻监听 Web 后台/手机页面通过 MQTT 下发的语音补盲命令，收到命令后调用 `voice_accessibility.py` 执行播报并上报设备事件。

手机语音输入阶段新增了 Web 语音意图接口。手机页面可以把“这是苹果 / 改成番茄 / 不是香蕉，是橙子”解析为商品修正请求，Web 后台会修正最近一笔交易并通过 MQTT 下发 `speak_text`，由 `voice_command_mqtt.py` 直接调用 SYN6288 播报修正后的报价。

## 训练与部署工具

这些文件不是每天运行，但在采集数据、训练模型、转换 RKNN 或检查环境时仍然有用。

```text
tools/capture_detection_dataset.py 摄像头采集检测图片，并用空秤背景差分自动生成 YOLO 框标签
tools/detect_dataset_report.py     检查 YOLO 检测数据集图片/标签配对和类别分布
tools/train_yolo_detector.py      训练 YOLO 检测模型
tools/export_yolo_detector_onnx.py 导出 YOLO 检测 ONNX
tools/make_rknn_quant_dataset.py  生成 RKNN 量化图片列表
tools/convert_onnx_to_rknn.py     ONNX 转 RKNN
tools/check_env.py                环境检查
tools/image_utils.py              数据集工具函数
```

## 历史过程

早期验证脚本、旧分类模型路线和旧工具不再保留在当前 `scripts/` 目录中。项目从分类模型、小闭环验证到 YOLO11 检测 + RKNN/NPU 主链路的演进过程见 `../docs/开发日志.md`。

## Web 后台拆分

```text
web_dashboard.py          HTTP 服务、API 路由、静态资源返回
dashboard_data.py         records/products/runtime config 数据处理
dashboard_mqtt.py         MQTT 同步与参数/策略下发
../web/index.html         Web 页面结构
../web/static/style.css   Web 样式
../web/static/app.js      Web 前端交互
```

