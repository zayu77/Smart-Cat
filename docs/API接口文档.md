# Smart-Cat API 接口文档

本文档记录 Smart-Cat 当前 Web 后台、MQTT 云边通信、板端配置下发和交易数据结构，便于项目联调、平台扩展和答辩展示。

## 1. 服务启动

电脑端启动 Web 后台：

```bash
python scripts/web_dashboard.py --host 127.0.0.1 --port 8080 --mqtt-sync --mqtt-config config/mqtt.json
```

默认地址：

```text
http://127.0.0.1:8080
```

当前接口未实现登录鉴权，默认用于局域网或本机调试环境。

## 2. HTTP API 约定

基础地址：

```text
http://127.0.0.1:8080
```

请求格式：

```text
GET  接口使用 URL Query 参数
POST 接口使用 application/json
```

响应格式：

```text
正常：JSON 对象或 JSON 数组
异常：{"error": "错误信息"}
```

常用交易筛选参数：

```text
start    开始日期，格式 YYYY-MM-DD
end      结束日期，格式 YYYY-MM-DD
product  商品 ID 或商品名称
status   交易状态
limit    返回数量，默认 100
```

交易状态：

```text
accepted        已可靠识别
low_confidence  低置信度
needs_confirm   待人工确认
unknown         未知商品
rejected        策略拒绝结算
```

## 3. 交易与统计接口

### 3.1 获取交易摘要

```http
GET /api/summary?start=2026-07-01&end=2026-07-03&product=apple&status=accepted
```

返回：

```json
{
  "total_transactions": 12,
  "total_sales": 86.5,
  "total_weight_g": 5200,
  "status_counts": {
    "accepted": 10,
    "needs_confirm": 2
  },
  "product_counts": {
    "苹果": 8
  },
  "product_sales": {
    "苹果": 63.2
  },
  "latest": {}
}
```

### 3.2 获取交易列表

```http
GET /api/transactions?limit=100&status=needs_confirm
```

返回：交易记录数组，按最新记录在前返回。

### 3.3 获取交易详情

```http
GET /api/transaction?id=交易ID
```

返回：单条交易记录。

### 3.4 导出交易 CSV

```http
GET /api/transactions.csv?start=2026-07-01&end=2026-07-03
```

返回：CSV 文件下载。

导出字段：

```text
transaction_id,timestamp,device_id,status,product_id,product_name,confidence,
weight_g,unit,unit_price,total_price,voice_text,source_image,correction_count
```

### 3.5 获取统计分析

```http
GET /api/analytics?start=2026-07-01&end=2026-07-03
```

返回内容包括：

```text
总交易数
总销售额
总重量
今日销售额
近 7 天销售趋势
商品销量排行
商品销售占比
识别准确率
低置信度/待确认数量
```

## 4. 人工修正接口

### 4.1 确认或修正交易

```http
POST /api/transaction/confirm
Content-Type: application/json
```

请求：

```json
{
  "transaction_id": "f8ea5525b8ea46618c1b3b488892de19",
  "product_id": "apple",
  "unit_price": 9.9,
  "status": "accepted",
  "operator": "web_dashboard",
  "note": "人工确认苹果"
}
```

说明：

```text
product_id   修正后的商品 ID
unit_price   修正后的单价，可选
status       修正后的状态，常用 accepted / needs_confirm / rejected
operator     操作来源
note         备注
```

返回：修正后的交易记录。

系统会记录修正前后的信息，包括商品、单价、状态、总价等变化。

## 5. 商品档案接口

### 5.1 获取商品配置

```http
GET /api/products
```

返回：

```json
{
  "apple": {
    "name": "苹果",
    "unit": "斤",
    "unit_price": 9.9,
    "voice_name": "苹果",
    "enabled": true,
    "remark": "",
    "price_history": [],
    "created_at": "",
    "updated_at": ""
  }
}
```

### 5.2 保存商品配置

```http
POST /api/products
Content-Type: application/json
```

请求：

```json
{
  "apple": {
    "name": "苹果",
    "unit": "斤",
    "unit_price": 9.9,
    "voice_name": "苹果",
    "enabled": true,
    "remark": "普通红富士"
  },
  "banana": {
    "name": "香蕉",
    "unit": "kg",
    "unit_price": 13.6,
    "voice_name": "香蕉",
    "enabled": true,
    "remark": ""
  }
}
```

说明：

```text
商品 ID 由 JSON 第一层 key 表示，例如 apple
unit 当前建议使用 斤 或 kg
enabled=false 表示停用商品
单价变化会写入 price_history
```

返回：保存后的商品配置。

## 6. 运行参数接口

运行参数用于远程调整板端识别、摄像头、称重、语音和 MQTT 行为。Web 后台保存后会发布到 MQTT retained 主题。单次运行模式会在启动流程时拉取；常驻服务模式会在空秤待机状态下定期拉取。

### 6.1 获取运行参数

```http
GET /api/runtime-config
```

返回：

```json
{
  "config": {},
  "status": {},
  "cache": {}
}
```

### 6.2 保存并下发运行参数

```http
POST /api/runtime-config
Content-Type: application/json
```

请求示例：

```json
{
  "version": "1.0.0",
  "device_id": "lubancat3_demo_001",
  "recognition": {
    "backend": "rknn-det",
    "accept_confidence": 0.75,
    "confirm_gap": 0.15,
    "topk": 3,
    "rknn_imgsz": 640,
    "rknn_layout": "nhwc",
    "rknn_float_input": false,
    "det_conf": 0.25,
    "det_iou": 0.45,
    "det_max": 20,
    "det_score_sigmoid": false
  },
  "camera": {
    "device": "0",
    "image_output": "outputs/current.jpg",
    "width": 1920,
    "height": 1080,
    "warmup": 1.0
  },
  "weight": {
    "hx711_config": "config/hx711_scale.json",
    "gpio_backend": "gpiod",
    "samples": 20,
    "max_deviation": 5000.0
  },
  "tts": {
    "backend": "syn6288",
    "port": "/dev/ttyS10",
    "baudrate": 9600,
    "encoding": "gb2312",
    "music": 0,
    "volume": 3,
    "music_volume": 0,
    "speed": 5
  },
  "mqtt": {
    "enabled": true,
    "config": "config/mqtt.json",
    "optional": true
  }
}
```

返回：

```json
{
  "config": {},
  "mqtt": {
    "topic": "smart-cat/lubancat3_demo_001/runtime-config",
    "qos": 1,
    "retain": true
  },
  "applied_summary": {
    "device_id": "lubancat3_demo_001",
    "accept_confidence": 0.75,
    "tts_volume": 3,
    "mqtt_enabled": true
  },
  "status": {},
  "cache": {}
}
```

### 6.3 获取运行参数下发状态

```http
GET /api/runtime-config/status
```

返回字段：

```text
enabled
last_published_at
last_error
topic
```

## 7. 设备策略接口

设备策略用于远程改变板端业务行为，例如低置信度处理、计价模式、语音模板和策略回滚。

### 7.1 获取设备策略

```http
GET /api/device-policy
```

返回：

```json
{
  "policy": {},
  "status": {},
  "cache": {},
  "latest_event": {}
}
```

### 7.2 保存并下发设备策略

```http
POST /api/device-policy
Content-Type: application/json
```

请求：

```json
{
  "policy_version": "policy-v1.0.1",
  "description": "低置信度进入人工确认，标准计价",
  "enabled": true,
  "low_confidence_action": "needs_confirm",
  "unknown_product_action": "needs_confirm",
  "pricing_mode": "standard",
  "voice_template": "{product_name}，净重{weight_g}克，单价{unit_price}元每{unit}，总价{total_price}元。",
  "confirm_voice_template": "请人工确认，识别为{product_name}，重量{weight_g}克。",
  "reject_voice_template": "当前商品未能可靠识别，已暂停结算，请人工处理。"
}
```

枚举值：

```text
low_confidence_action: keep / accept / needs_confirm / reject
unknown_product_action: keep / needs_confirm / reject
pricing_mode: standard / discount_10_over_1000g
```

返回：

```json
{
  "policy": {},
  "mqtt": {},
  "status": {},
  "cache": {},
  "latest_event": {}
}
```

### 7.3 获取策略下发状态

```http
GET /api/device-policy/status
```

返回字段：

```text
enabled
last_published_at
last_error
topic
rollback_available
```

### 7.4 获取策略历史

```http
GET /api/device-policy/history
```

返回：策略历史数组，最新在前。每条包含：

```text
history_index
policy_version
archived_at
modified_at
low_confidence_action
unknown_product_action
pricing_mode
voice_template
```

### 7.5 回滚策略

```http
POST /api/device-policy/rollback
Content-Type: application/json
```

请求：

```json
{
  "history_index": 0
}
```

说明：

```text
history_index 可选。
不传时默认回滚到最近一个历史版本。
回滚成功后会重新发布到 device-policy retained topic。
```

返回：

```json
{
  "policy": {},
  "mqtt": {},
  "status": {},
  "cache": {},
  "latest_event": {}
}
```

## 8. 设备状态与事件接口

### 8.1 获取 MQTT 同步状态

```http
GET /api/mqtt-status
```

返回字段：

```text
enabled
connected
host
port
topic
event_topic
last_error
last_message_at
last_transaction_id
received_count
```

### 8.2 获取设备状态

```http
GET /api/device-status
```

返回内容包括：

```text
设备 ID
服务状态
当前重量
最近一次上报时间
MQTT 连接状态
今日交易数
今日销售额
最近识别状态
设备在线/离线判断
最近心跳时间
```

设备在线判断优先参考 `service_heartbeat` / `service_state` 设备事件；如果没有心跳事件，则退回到最近交易上报时间。默认 300 秒内有心跳或交易上报视为在线。Web 后台自身连接到 MQTT Broker 只表示后台订阅正常，不再直接代表设备在线。

### 8.3 获取设备事件列表

```http
GET /api/device-events?limit=100&event_type=policy_applied
```

返回：设备事件数组。

### 8.4 获取设备事件摘要

```http
GET /api/device-events/summary
```

返回内容包括：

```text
total_events
event_type_counts
policy_apply_count
policy_version_counts
latest
latest_policy
```

### 8.5 获取最近设备事件

```http
GET /api/device-events/latest
```

返回：最近一条设备事件。

## 9. 图片媒体接口

### 9.1 读取本地图片

```http
GET /media?path=outputs/current.jpg
```

说明：

```text
用于 Web 后台展示交易图片。
只允许读取项目目录内的图片文件。
```

## 10. MQTT 接口

MQTT 配置文件：

```text
config/mqtt.json
```

配置示例：

```json
{
  "host": "broker.emqx.io",
  "port": 1883,
  "topic": "smart-cat/lubancat3_demo_001/transactions",
  "runtime_config_topic": "smart-cat/lubancat3_demo_001/runtime-config",
  "device_policy_topic": "smart-cat/lubancat3_demo_001/device-policy",
  "device_event_topic": "smart-cat/lubancat3_demo_001/device-events",
  "voice_command_topic": "smart-cat/lubancat3_demo_001/voice-commands",
  "client_id": "smart-cat-lubancat3-demo-001",
  "username": "",
  "password": "",
  "qos": 1,
  "retain": false,
  "keepalive": 60,
  "connect_timeout": 10
}
```

### 10.1 交易上报 Topic

```text
smart-cat/lubancat3_demo_001/transactions
```

方向：

```text
板端 -> MQTT Broker -> Web 后台
```

retain：

```text
false
```

Payload：交易记录 JSON。

### 10.2 运行参数下发 Topic

```text
smart-cat/lubancat3_demo_001/runtime-config
```

方向：

```text
Web 后台 -> MQTT Broker -> 板端
```

retain：

```text
true
```

Payload：

```json
{
  "type": "runtime_config",
  "timestamp": "2026-07-03T20:30:00",
  "config": {}
}
```

板端通过 `scripts/mqtt_runtime_config.py` 拉取该 retained 消息并写入：

```text
config/device_runtime.json
```

### 10.3 设备策略下发 Topic

```text
smart-cat/lubancat3_demo_001/device-policy
```

方向：

```text
Web 后台 -> MQTT Broker -> 板端
```

retain：

```text
true
```

Payload：

```json
{
  "type": "device_policy",
  "timestamp": "2026-07-03T20:30:00",
  "policy": {}
}
```

板端通过 `scripts/mqtt_runtime_config.py` 拉取该 retained 消息并写入：

```text
config/device_policy.json
```

### 10.4 设备事件上报 Topic

```text
smart-cat/lubancat3_demo_001/device-events
```

方向：

```text
板端 -> MQTT Broker -> Web 后台
```

retain：

```text
false
```

常见事件：

```text
policy_applied  板端已在某次交易中应用策略
service_heartbeat  常驻服务心跳，默认每 60 秒上报一次
service_state  常驻服务状态变化，例如 IDLE、WAIT_STABLE、RUN_TRANSACTION、WAIT_REMOVE
voice_command_executed  板端已执行 Web/手机下发的语音补盲命令
```

Payload 示例：

```json
{
  "event_id": "policy_applied-20260703203000123456",
  "event_type": "policy_applied",
  "timestamp": "2026-07-03T20:30:00",
  "device_id": "lubancat3_demo_001",
  "status": "success",
  "message": "Applied policy policy-v1.0.1 to transaction xxx",
  "policy_version": "policy-v1.0.1",
  "transaction_id": "xxx",
  "record_status": "accepted",
  "pricing_mode": "standard"
}
```

服务心跳 Payload 示例：

```json
{
  "event_id": "service_heartbeat-20260703203000123456",
  "event_type": "service_heartbeat",
  "timestamp": "2026-07-03T20:30:00",
  "device_id": "lubancat3_demo_001",
  "status": "success",
  "message": "",
  "policy_version": "policy-v1.0.1",
  "service_state": "IDLE",
  "current_weight_g": 0.3,
  "transaction_count": 12,
  "runtime_config": "config/device_runtime.json",
  "device_policy": "config/device_policy.json"
}
```

### 10.5 语音补盲命令下发 Topic

```text
smart-cat/lubancat3_demo_001/voice-commands
```

方向：

```text
Web 后台/手机页面 -> MQTT Broker -> 板端
```

retain：

```text
false
```

Payload 示例：

```json
{
  "request_id": "xxx",
  "command": "price",
  "source": "web_mobile",
  "timestamp": "2026-07-04T20:30:00"
}
```

板端 `scripts/voice_command_mqtt.py` 订阅该 topic，收到命令后调用 `scripts/voice_accessibility.py` 执行播报，并通过 `device-events` topic 上报 `voice_command_executed` 事件。

支持命令：

```text
status
weight
latest
price
pending
help
```

板端本地还会维护当前服务状态快照：

```text
records/service_status.json
```

示例：

```json
{
  "updated_at": "2026-07-03T20:30:00",
  "device_id": "lubancat3_demo_001",
  "service_state": "IDLE",
  "current_weight_g": 0.3,
  "transaction_count": 12,
  "event_type": "service_heartbeat",
  "message": "",
  "event_id": "service_heartbeat-20260703203000123456"
}
```

## 11. 交易记录数据结构

交易记录保存位置：

```text
records/transactions.jsonl
```

每行一条 JSON。

核心字段：

```json
{
  "transaction_id": "f8ea5525b8ea46618c1b3b488892de19",
  "timestamp": "2026-07-03T20:30:00",
  "device_id": "lubancat3_demo_001",
  "source_image": "outputs/current.jpg",
  "status": "accepted",
  "product_id": "apple",
  "product_name": "苹果",
  "confidence": 0.616,
  "weight_g": 418,
  "unit": "斤",
  "unit_price": 9.9,
  "total_price": 8.27,
  "voice_text": "苹果，净重418克，单价9.9元每斤，总价8.27元。",
  "top_predictions": [
    {"product_id": "apple", "confidence": 0.616},
    {"product_id": "tomato", "confidence": 0.339}
  ],
  "camera": {},
  "weight_source": {},
  "runtime_config": {},
  "policy": {},
  "pricing": {},
  "tts": {},
  "mqtt": {},
  "device_event": {}
}
```

策略相关字段：

```json
{
  "policy": {
    "enabled": true,
    "policy_version": "policy-v1.0.1",
    "pricing_mode": "standard",
    "low_confidence_action": "needs_confirm",
    "unknown_product_action": "needs_confirm",
    "selected_action": "keep",
    "original": {
      "status": "accepted",
      "total_price": 8.27,
      "voice_text": "原始播报文本"
    },
    "applied_at": "2026-07-03T20:30:00",
    "path": "config/device_policy.json"
  }
}
```

人工修正字段通常包含：

```text
correction_count
corrections
```

用于记录每次人工修改前后的商品、状态、单价和总价。

## 12. 常用联调流程

### 12.0 本地语音补盲命令

语音补盲命令层同时支持本地命令和 Web/MQTT 远程命令，离线语音识别模块、小程序或网页按钮都可以映射到这些命令。

入口：

```bash
./bin/run_voice_command.sh status
```

支持命令：

```text
status   播报设备当前状态、当前重量、交易次数
weight   播报当前重量
latest   播报最近一笔交易
price    播报最近一笔交易价格
pending  播报最近交易是否需要人工确认
help     播报可用命令
```

底层脚本：

```text
scripts/voice_accessibility.py
```

读取数据：

```text
records/service_status.json
records/transactions.jsonl
config/device_runtime.json
```

命令日志：

```text
records/voice_commands.jsonl
```

Web 后台/手机页面远程下发命令：

```http
POST /api/voice-command
Content-Type: application/json
```

请求：

```json
{
  "command": "price",
  "source": "web_mobile"
}
```

支持的 `command`：

```text
status
weight
latest
price
pending
help
```

响应：

```json
{
  "command": "price",
  "topic": "smart-cat/lubancat3_demo_001/voice-commands",
  "request_id": "xxx",
  "payload": {},
  "mqtt": {}
}
```

板端监听：

```bash
./bin/run_voice_command_mqtt.sh
```

### 12.1 Web 后台接收板端交易

1. 电脑端启动：

```bash
python scripts/web_dashboard.py --host 127.0.0.1 --port 8080 --mqtt-sync --mqtt-config config/mqtt.json
```

2. 板端运行：

```bash
./bin/run_smart_scale.sh
```

或常驻运行：

```bash
./bin/run_smart_scale_service.sh
```

3. Web 后台通过交易 Topic 接收交易，通过设备事件 Topic 接收策略应用事件。

### 12.2 Web 后台远程下发参数

1. 调用或页面保存：

```http
POST /api/runtime-config
```

2. Web 后台发布 retained 消息到：

```text
runtime-config topic
```

3. 板端同步并写入：

```text
config/device_runtime.json
```

同步时机：

```text
bin/run_smart_scale.sh 单次模式：每次启动流程时同步
bin/run_smart_scale_service.sh 常驻模式：空秤待机时按 sync-interval 定期同步
```

### 12.3 Web 后台远程下发策略

1. 调用或页面保存：

```http
POST /api/device-policy
```

2. Web 后台发布 retained 消息到：

```text
device-policy topic
```

3. 板端同步并写入：

```text
config/device_policy.json
```

同步时机：

```text
bin/run_smart_scale.sh 单次模式：每次启动流程时同步
bin/run_smart_scale_service.sh 常驻模式：空秤待机时按 sync-interval 定期同步
```

4. 交易完成后板端上报：

```text
device-events topic
```

Web 后台据此显示策略是否真正应用。

## 13. 手机语音输入与语音修正接口

### 13.1 Web 语音意图接口

```text
POST /api/voice-intent
```

请求体：

```json
{
  "text": "不是香蕉，是橙子",
  "source": "web_mobile"
}
```

接口会将中文文本解析为两类意图：

```text
voice_command      普通语音补盲命令，例如播报价格、当前重量、设备状态
confirm_product    商品修正命令，例如这是苹果、改成番茄、不是香蕉，是橙子
```

普通命令会继续向 `voice_command_topic` 发布 `status`、`weight`、`latest`、`price`、`pending`、`help` 等命令。

商品修正命令默认选择 Web 后台最近一笔交易，这更符合“刚识别完马上语音纠正”的现场操作。Web 后台会调用人工修正逻辑更新商品、单价和总价，并在交易记录的 `corrections` 字段里保存修正前后的信息。

响应示例：

```json
{
  "intent": {
    "intent": "confirm_product",
    "product_id": "orange",
    "product_name": "橙子"
  },
  "transaction_id": "xxx",
  "speech_text": "已修正为橙子，净重418克，单价7.8元每斤，总价6.52元。",
  "topic": "smart-cat/lubancat3-demo-001/voice-commands",
  "mqtt": {
    "topic": "smart-cat/lubancat3-demo-001/voice-commands",
    "qos": 1,
    "retain": false
  }
}
```

### 13.2 直接播报 MQTT 命令

手机语音修正后，Web 后台会向板端下发直接播报命令：

```json
{
  "request_id": "xxx",
  "command": "speak_text",
  "text": "已修正为橙子，净重418克，单价7.8元每斤，总价6.52元。",
  "source": "web_mobile",
  "recognized_text": "不是香蕉，是橙子",
  "transaction_id": "xxx",
  "timestamp": "2026-07-04T20:30:00"
}
```

板端 `scripts/voice_command_mqtt.py` 收到 `speak_text` 后直接调用 SYN6288 TTS 播报文本，并写入 `records/voice_commands.jsonl`，同时上报 `voice_command_executed` 设备事件。


