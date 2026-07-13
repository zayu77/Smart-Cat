# Smart-Cat

基于视觉与多传感器融合的边缘智能称重识别一体机。

项目主线：鲁班猫 3 / RK3576 + 摄像头 + HX711 称重 + RKNN/NPU 商品检测识别 + SYN6288 动态语音播报 + MQTT 云边通信 + Web 管理后台。

## 功能概览

```text
边缘端自动交易：拍照、识别、称重、计价、播报、记录、上报
常驻称重服务：空秤待机、重量触发、稳定判断、自动生成交易
Web 后台：交易流水、筛选、详情、CSV 导出、商品维护、统计分析
人工确认：低置信度/未知商品可在后台修正，重新计算总价并保留修正记录
语音补盲：手机/Web 下发语音命令，支持商品修正和报价播报
商品记忆库：未知商品经人工确认后，板端保存图片特征；下次相似商品可自动记忆匹配
云边协同：运行参数和设备策略通过 MQTT retained 下发，设备事件回传
策略管理：策略版本、低置信度动作、计价模式、语音模板、回滚记录
模型部署：YOLO11 检测模型导出 ONNX 后转换为 RKNN，在 RK3576 NPU 推理
```

当前检测模型自动识别 `apple`、`banana`、`orange`。其它商品先进入 `unknown / 待确认`，再通过 Web 后台或手机语音补盲修正为番茄、梨等商品。修正时 Web 后台会通过 MQTT 下发 `bind_memory` 命令，板端使用本地交易图片生成 `records/product_memory.jsonl`；后续再次遇到相似商品时，可进入 `memory_matched` 状态并自动计价。

## 项目结构

```text
bin/                    板端一键运行入口脚本
config/                 商品表、MQTT、设备运行参数、策略配置
docs/                   API 文档、开发日志、项目说明材料
models/                 当前 YOLO11 检测模型与 RKNN 部署文件
records/                交易、设备事件、策略历史、语音命令记录
scripts/                当前主链路 Python 脚本
scripts/tools/          检测数据、训练、ONNX 导出、RKNN 转换工具
tests/                  pytest 自动化测试用例
web/                    Web 后台前端页面、样式和交互脚本
yolov11model-train/     当前 YOLO11 检测训练数据与量化图片列表
```

## 公开仓库说明

GitHub 版本只提交源码、前端资源、说明文档、示例配置和模型标签文件；以下内容不会提交：

```text
config/*.json            本地真实配置，可能包含 MQTT 地址、设备参数或现场校准值
records/*.jsonl          本地交易流水、设备事件和策略历史
outputs/                 拍照图片、检测预览和临时输出
models/*.pt/*.onnx/*.rknn 训练权重、ONNX 和 RKNN 模型文件
yolov11model-train/      训练数据集与量化图片列表
交付材料/                课程交付材料和个人信息
```

首次克隆后，需要从示例配置创建本地配置：

```bash
cp config/products.example.json config/products.json
cp config/mqtt.example.json config/mqtt.json
cp config/device_runtime.example.json config/device_runtime.json
cp config/device_policy.example.json config/device_policy.json
```

如需在板端运行识别流程，请自行准备并放入：

```text
models/yolo_product_detector.rknn
models/yolo_product_detector.labels.json
```

## 硬件连接

本项目硬件以鲁班猫 3 / RK3576 为边缘计算设备，外接 USB 摄像头、HX711 称重模块、SYN6288 语音合成模块和 USB 无线网卡。

### 总连线表

| 模块 | 模块引脚/接口 | 鲁班猫连接位置 | 说明 |
| --- | --- | --- | --- |
| USB 摄像头 | USB | USB 口 | 默认 OpenCV 设备号 `0` |
| USB 无线网卡 | USB | USB 口 | 用于连接 WiFi、MQTT broker 和 Web 后台网络 |
| HX711 | DT / DOUT | GPIO4_A6 / GPIO number 134 / gpiochip4 line 6 | 称重 ADC 数据线 |
| HX711 | SCK / CLK | GPIO4_A4 / GPIO number 132 / gpiochip4 line 4 | 称重 ADC 时钟线 |
| HX711 | VCC | 3.3V | 当前接 3.3V 供电 |
| HX711 | GND | GND | 与鲁班猫共地 |
| 称重传感器 | E+ / E- / A+ / A- | 接入 HX711 对应端子 | 按传感器线色定义连接 |
| SYN6288 | RXD | 鲁班猫引脚 8 / UART_TX | 串口 TX 接模块 RXD |
| SYN6288 | TXD | 鲁班猫引脚 10 / UART_RX | 模块 TXD 接串口 RX |
| SYN6288 | VCC | 5V | 语音模块供电 |
| SYN6288 | GND | GND | 与鲁班猫共地 |
| SYN6288 | SPK+ / SPK- | 喇叭 | 接无源喇叭输出声音 |

注意事项：

```text
1. UART 串口需要交叉连接：鲁班猫 TX -> SYN6288 RXD，SYN6288 TXD -> 鲁班猫 RX。
2. HX711 和 SYN6288 必须与鲁班猫共地，否则串口或称重读数可能异常。
3. SYN6288 使用 5V 供电，HX711 当前使用 3.3V 供电，不要把两者电源接反。
4. 板端默认串口设备为 /dev/ttyS10，GPIO 默认使用 gpiod 后端。
5. 第一次更换称重结构、传感器或供电后，需要重新校准 HX711。
```

### 摄像头与网络

```text
USB 摄像头       -> 鲁班猫 USB 口，默认 OpenCV 设备号为 0
USB 无线网卡     -> 鲁班猫 USB 口，用于连接 WiFi、MQTT broker 和 Web 后台所在网络
```

摄像头默认参数在 `config/device_runtime.json`：

```json
"camera": {
  "device": "0",
  "image_output": "outputs/current.jpg",
  "width": 1920,
  "height": 1080,
  "warmup": 1.0
}
```

### HX711 称重模块

当前代码默认使用 `gpiod` 后端，DOUT 和 SCK 对应鲁班猫 GPIO4 组：

```text
HX711 DT/DOUT  -> 鲁班猫 GPIO4_A6 / GPIO number 134 / gpiochip4 line 6
HX711 SCK/CLK  -> 鲁班猫 GPIO4_A4 / GPIO number 132 / gpiochip4 line 4
HX711 VCC      -> 鲁班猫 3.3V
HX711 GND      -> 鲁班猫 GND
称重传感器      -> 按传感器颜色定义接入 HX711 E+ / E- / A+ / A-
```

代码默认值见 `scripts/hx711_reader.py`：

```text
DEFAULT_DOUT_GPIO = 134
DEFAULT_SCK_GPIO  = 132
dout_chip = gpiochip4, dout_line = 6
sck_chip  = gpiochip4, sck_line  = 4
```

板端校准后会使用 `config/hx711_scale.json` 保存零点和比例系数。该文件属于现场校准产物，不同称重结构和传感器需要重新去皮、加载已知重量并保存校准参数。

### SYN6288 语音合成模块

SYN6288 使用 UART10，对应板端设备 `/dev/ttyS10`，默认波特率 `9600`。

```text
鲁班猫 引脚 8  / UART_TX -> SYN6288 RXD
鲁班猫 引脚 10 / UART_RX -> SYN6288 TXD
鲁班猫 5V                 -> SYN6288 VCC
鲁班猫 GND                -> SYN6288 GND
SYN6288 SPK+/SPK-         -> 喇叭
```

语音参数在 `config/device_runtime.json`：

```json
"tts": {
  "backend": "syn6288",
  "port": "/dev/ttyS10",
  "baudrate": 9600,
  "encoding": "gb2312",
  "volume": 3,
  "speed": 5
}
```

单独测试语音模块：

```bash
sudo .venv/bin/python scripts/syn6288_tts.py \
  --text "语音模块测试成功" \
  --port /dev/ttyS10 \
  --baudrate 9600 \
  --volume 3
```

## 板端运行

单次完整交易：

```bash
cd ~/Smart-Cat
chmod +x bin/*.sh
./bin/run_smart_scale.sh
```

该入口会先从 MQTT retained 主题同步运行参数和设备策略，再执行拍照、RKNN 检测、HX711 称重、策略计价、SYN6288 播报、JSONL 记录、设备事件回传和 MQTT 交易上报。

临时覆盖参数示例：

```bash
./bin/run_smart_scale.sh --accept-confidence 0.75 --tts-volume 2
```

常驻称重触发服务：

```bash
./bin/run_smart_scale_service.sh
```

常驻流程：

```text
空秤待机
-> 空秤时定期同步 Web 后台下发的运行参数和设备策略
-> 空秤时预加载 RKNN runtime，并保持摄像头打开
-> 重量超过触发阈值
-> 连续多次读数稳定
-> 拍照前刷新摄像头缓冲帧，并使用预加载 RKNN runtime 执行识别、计价、播报、记录和上报
-> 交易结束后释放本次 RKNN runtime
-> 等待物体拿走
-> 回到空秤待机
```

常用调试命令：

```bash
./bin/run_smart_scale_service.sh --once
./bin/run_smart_scale_service.sh --heartbeat-interval 60
./bin/run_smart_scale_service.sh --subprocess-transaction
./bin/run_smart_scale_service.sh --verbose
```

## Web 后台

电脑端启动：

```bash
python scripts/web_dashboard.py --host 0.0.0.0 --port 8080 --mqtt-sync --mqtt-config config/mqtt.json
```

默认模式会隐藏 HTTP 访问日志和 MQTT 同步细节；排查接口轮询或 MQTT 消息时可加 `--verbose`。

浏览器访问：

```text
http://127.0.0.1:8080
```

后台功能：

```text
交易流水查看、筛选、详情、CSV 导出
低置信度/待确认交易人工修正
商品档案、单位、单价、启用状态和备注维护
设备状态、最近心跳、最近识别状态展示
销售统计、商品排行、识别质量分析
运行参数配置并通过 MQTT retained 下发
设备策略配置、策略版本、策略历史和回滚
设备事件日志与策略应用记录可视化
语音补盲页面和手机语音输入修正
商品记忆绑定状态与记忆匹配来源展示
```

## 语音补盲

本地命令：

```bash
./bin/run_voice_command.sh status
./bin/run_voice_command.sh latest
./bin/run_voice_command.sh price
./bin/run_voice_command.sh pending
```

MQTT 常驻监听：

```bash
./bin/run_voice_command_mqtt.sh
```

Web 后台“语音补盲”页面会发布命令到：

```text
smart-cat/lubancat3-demo-001/voice-commands
```

手机语音或文本输入示例：

```text
这是苹果
改成番茄
不是香蕉，是橙子
播报价格
当前重量
```

商品修正会更新最近一笔交易，重新计算总价，记录修正前后信息，并通过 MQTT 下发 `speak_text` 让板端 SYN6288 播报修正后的报价。

同时，商品修正会额外下发 `bind_memory` 命令。板端收到后读取本地交易原图，提取轻量图像特征并写入：

```text
records/product_memory.jsonl
```

后续如果 YOLO/RKNN 对某个商品输出 `unknown / low_confidence / needs_confirm`，板端会先查询商品记忆库。匹配成功时交易状态为：

```text
memory_matched
```

交易详情中会显示“识别来源：商品记忆库”、相似度、相似度差值和记忆 ID。

## MQTT 主题

由 `config/mqtt.json` 配置：

```json
{
  "topic": "smart-cat/lubancat3-demo-001/transactions",
  "runtime_config_topic": "smart-cat/lubancat3-demo-001/runtime-config",
  "device_policy_topic": "smart-cat/lubancat3-demo-001/device-policy",
  "device_event_topic": "smart-cat/lubancat3-demo-001/device-events",
  "voice_command_topic": "smart-cat/lubancat3-demo-001/voice-commands"
}
```

## 模型与训练

当前板端推理使用非量化 RKNN 检测模型：

```text
models/yolo_product_detector.rknn
models/yolo_product_detector.labels.json
```

检测训练数据：

```text
yolov11model-train/product_yolov11/
```

数据检查：

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

转换 RKNN：

```bash
python scripts/tools/convert_onnx_to_rknn.py --target-platform rk3576
```

## 主要脚本

```text
bin/run_smart_scale.sh              单次完整交易入口
bin/run_smart_scale_service.sh      常驻称重触发服务入口
bin/run_voice_command.sh            本地语音补盲命令入口
bin/run_voice_command_mqtt.sh       MQTT 语音补盲命令监听入口

scripts/smart_scale_demo.py         单次交易主流程
scripts/smart_scale_service.py      常驻称重触发服务
scripts/predict_rknn_detector.py    RKNN YOLO 检测推理和 NMS 后处理
scripts/recognize_product_detector_rknn.py  检测结果转商品、状态和计价数据
scripts/product_business.py         商品表、状态判断、计价和播报文本
scripts/product_memory.py           商品记忆库：图片特征提取、记忆保存和相似度匹配
scripts/transaction_utils.py        交易记录构建、JSONL 写入、终端收据
scripts/device_policy.py            设备策略校验、计价模式、语音模板和应用事件
scripts/mqtt_publisher.py           MQTT 发布、主题推导、交易和事件上报
scripts/mqtt_runtime_config.py      板端拉取 MQTT retained 参数和策略
scripts/dashboard_data.py           Web 后台数据层
scripts/dashboard_mqtt.py           Web 后台 MQTT 同步和参数/策略下发
scripts/web_dashboard.py            Web 后台 HTTP 服务和 API
scripts/voice_accessibility.py      语音补盲命令执行
scripts/voice_command_mqtt.py       语音补盲 MQTT 监听
scripts/hx711_reader.py             HX711 读取、滤波和校准
scripts/syn6288_tts.py              SYN6288 串口 TTS 协议
scripts/tts_output.py               TTS 输出适配层
scripts/camera_test.py              摄像头拍照测试
```

## 测试验证

当前自动化测试覆盖商品状态判断、计价策略、交易记录、MQTT topic 推导和策略默认值等核心纯逻辑：

```bash
python -m pytest -q tests
```

板端硬件、RKNN/NPU、HX711、SYN6288、MQTT 和 Web 后台属于端到端联调范围，详见 `交付材料/7-测试计划.md` 与 `交付材料/8-测试报告.md`。

## 演示流程

1. 电脑端启动 Web 后台。
2. 板端启动 `./bin/run_voice_command_mqtt.sh` 监听语音补盲命令。
3. 板端启动 `./bin/run_smart_scale_service.sh` 常驻称重。
4. 放上苹果、香蕉或橙子，等待设备自动识别、称重、计价和播报。
5. 在 Web 后台查看交易流水、统计分析和设备事件。
6. 对低置信度或未知商品使用 Web/手机语音输入“这是苹果”“改成番茄”等完成修正。
7. 板端收到 `bind_memory` 后生成商品记忆；再次放上相似未知商品时，可自动进入 `memory_matched`。
8. 在 Web 后台查看修正后的交易详情、商品记忆来源、策略版本和设备事件回传。

## 开发过程

当前提交目录只保留最终主链路、仍使用的训练/转换工具和交付文档。早期分类路线、旧脚本、旧模型和数据整理过程已经沉淀在开发日志中，便于复盘项目从小闭环逐步演进到完整系统的过程。

详细材料见：

```text
docs/开发日志.md
docs/API接口文档.md
docs/项目说明.md
docs/脚本参数说明.md
```
