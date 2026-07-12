"""Voice accessibility command layer for Smart-Cat."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from product_business import format_money
from transaction_utils import append_jsonl
from tts_output import speak_text


# 4 个默认文件路径：以本文件位置为锚定推算项目根，便于 CLI 独立运行
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "records" / "transactions.jsonl"
DEFAULT_SERVICE_STATUS = ROOT / "records" / "service_status.json"
DEFAULT_COMMAND_LOG = ROOT / "records" / "voice_commands.jsonl"
DEFAULT_RUNTIME_CONFIG = ROOT / "config" / "device_runtime.json"


# 语音命令的同义词字典：把中英文/不同表达统一映射到 6 个核心命令
COMMAND_ALIASES = {
    "status": "status",
    "state": "status",
    "状态": "status",
    "设备状态": "status",
    "weight": "weight",
    "重量": "weight",
    "当前重量": "weight",
    "latest": "latest",
    "repeat": "latest",
    "last": "latest",
    "重复": "latest",
    "最近交易": "latest",
    "price": "price",
    "价格": "price",
    "多少钱": "price",
    "total": "price",
    "pending": "pending",
    "confirm": "pending",
    "待确认": "pending",
    "是否确认": "pending",
    "help": "help",
    "帮助": "help",
}

# 4 个设备状态机状态的中文 TTS 标签：用于"设备状态"命令播报
STATE_TEXT = {
    "IDLE": "空闲待机",
    "WAIT_STABLE": "检测到商品，正在等待重量稳定",
    "RUN_TRANSACTION": "正在识别和结算",
    "WAIT_REMOVE": "本次结算已完成，请拿走商品",
}

# 5 个交易状态的中文 TTS 标签：用于"最近交易"等命令的状态播报
STATUS_TEXT = {
    "accepted": "已可靠识别",
    "low_confidence": "低置信度",
    "needs_confirm": "需要人工确认",
    "unknown": "未知商品",
    "rejected": "已暂停结算",
}


# 本地 JSON 加载器：文件缺失/解析失败都回退到 fallback（比 dashboard_data 版本更宽松）
def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return fallback


# 读 JSONL：损坏行静默跳过、非 dict 也跳过，永不抛错（语音命令对错误零容忍）
def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


# 按 timestamp 字符串排序取最新一条（要求同时有 transaction_id + timestamp 才算数）
def latest_transaction(path: Path) -> dict[str, Any] | None:
    records = [
        record for record in load_records(path)
        if record.get("transaction_id") and record.get("timestamp")
    ]
    if not records:
        return None
    records.sort(key=lambda record: str(record.get("timestamp", "")))
    return records[-1]


# 从 device_runtime.json 抽 tts 段 8 字段做默认值：CLI 参数再覆盖它们
def runtime_tts_defaults(path: Path) -> dict[str, Any]:
    data = load_json(path, {})
    tts = data.get("tts", {}) if isinstance(data, dict) else {}
    if not isinstance(tts, dict):
        tts = {}
    return {
        "backend": str(tts.get("backend", "mock")),
        "port": str(tts.get("port", "/dev/ttyS10")),
        "baudrate": int(tts.get("baudrate", 9600)),
        "encoding": str(tts.get("encoding", "gb2312")),
        "music": int(tts.get("music", 0)),
        "volume": int(tts.get("volume", 3)),
        "music_volume": int(tts.get("music_volume", 0)),
        "speed": int(tts.get("speed", 5)),
    }


# 规范化命令：去空格 + 小写 + 查 alias 表；找不到原样返回（让 build_response 报"不支持"）
def normalize_command(command: str) -> str:
    value = command.strip().lower()
    return COMMAND_ALIASES.get(value, value)


# TTS 文案：设备状态 + 当前重量 + 交易笔数 + 更新时间（status 命令主用）
def service_status_text(status: dict[str, Any]) -> str:
    if not status:
        return "暂时没有读取到设备服务状态。"
    state = str(status.get("service_state") or "")
    state_label = STATE_TEXT.get(state, state or "未知状态")
    weight = status.get("current_weight_g")
    count = int(status.get("transaction_count") or 0)
    updated_at = str(status.get("updated_at") or "")
    if weight is None:
        weight_text = "当前没有有效重量。"
    else:
        weight_text = f"当前重量约{float(weight):.0f}克。"
    if updated_at:
        return f"设备当前{state_label}，{weight_text}已完成{count}笔交易。状态更新时间，{updated_at}。"
    return f"设备当前{state_label}，{weight_text}已完成{count}笔交易。"


# TTS 文案：先报当前秤上重量，没有就报最近交易重量（weight 命令主用）
def weight_text(status: dict[str, Any], latest: dict[str, Any] | None) -> str:
    weight = status.get("current_weight_g") if status else None
    if weight is not None:
        return f"当前秤上重量约{float(weight):.0f}克。"
    if latest and latest.get("weight_g") is not None:
        return f"最近一笔交易重量为{float(latest.get('weight_g') or 0):.0f}克。"
    return "暂时没有可播报的重量。"


# TTS 文案：最近一笔交易的商品/状态/置信度/重量/价格（latest 命令主用）
def latest_text(record: dict[str, Any] | None) -> str:
    if not record:
        return "暂时没有交易记录。"
    product_name = record.get("product_name") or record.get("product_id") or "未知商品"
    status = STATUS_TEXT.get(str(record.get("status") or ""), str(record.get("status") or "未知状态"))
    confidence = float(record.get("confidence") or 0)
    weight = float(record.get("weight_g") or 0)
    total = record.get("total_price")
    if total is None:
        price_text = "总价暂未生成"
    else:
        price_text = f"总价{format_money(float(total))}元"
    return f"最近一笔交易，商品{product_name}，状态{status}，置信度{confidence:.2f}，重量{weight:.0f}克，{price_text}。"


# TTS 文案：最近交易价格 3 档分支（无价 / 有价无单价 / 完整单价+总价）
def price_text(record: dict[str, Any] | None) -> str:
    if not record:
        return "暂时没有可以播报的价格。"
    product_name = record.get("product_name") or record.get("product_id") or "未知商品"
    total = record.get("total_price")
    unit_price = record.get("unit_price")
    unit = record.get("unit") or "斤"
    if total is None:
        return f"最近识别商品为{product_name}，但暂时没有生成总价。"
    if unit_price is None:
        return f"{product_name}，总价{format_money(float(total))}元。"
    return f"{product_name}，单价{format_money(float(unit_price))}元每{unit}，总价{format_money(float(total))}元。"


# TTS 文案：待确认交易 5 状态分支（needs_confirm/low_confidence/unknown/rejected/其他）
def pending_text(record: dict[str, Any] | None) -> str:
    if not record:
        return "暂时没有交易需要确认。"
    status = str(record.get("status") or "")
    product_name = record.get("product_name") or record.get("product_id") or "未知商品"
    if status == "needs_confirm":
        return f"最近一笔交易需要人工确认，系统识别为{product_name}。"
    if status == "low_confidence":
        return f"最近一笔交易置信度较低，可能是{product_name}，建议人工确认。"
    if status == "unknown":
        return "最近一笔交易未能识别商品，需要人工处理。"
    if status == "rejected":
        return "最近一笔交易已暂停结算，需要人工处理。"
    return f"最近一笔交易状态为{STATUS_TEXT.get(status, status or '未知')}，无需人工确认。"


# TTS 文案：可用命令列表（help 命令主用）
def help_text() -> str:
    return "可用语音补盲命令包括，状态，重量，最近交易，价格，待确认，帮助。"


# 命令分发：把 normalize 后的命令路由到对应 TTS 文案生成器；未知命令给兜底帮助
def build_response(command: str, service_status: dict[str, Any], latest: dict[str, Any] | None) -> str:
    if command == "status":
        return service_status_text(service_status)
    if command == "weight":
        return weight_text(service_status, latest)
    if command == "latest":
        return latest_text(latest)
    if command == "price":
        return price_text(latest)
    if command == "pending":
        return pending_text(latest)
    if command == "help":
        return help_text()
    return f"暂不支持这个命令。{help_text()}"


# CLI 入口：读状态 → 路由 → 播报 → 写命令日志；--no-log 用于纯调试，--tts-* 覆盖运行时配置
def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Smart-Cat voice accessibility command.")
    parser.add_argument("command", nargs="?", default="status", help="Command: status, weight, latest, price, pending, help.")
    parser.add_argument("--service-status", default=str(DEFAULT_SERVICE_STATUS), help="Service status JSON path.")
    parser.add_argument("--records", default=str(DEFAULT_RECORDS), help="Transaction JSONL path.")
    parser.add_argument("--runtime-config", default=str(DEFAULT_RUNTIME_CONFIG), help="Runtime config JSON path for TTS defaults.")
    parser.add_argument("--command-log", default=str(DEFAULT_COMMAND_LOG), help="Voice command log JSONL path.")
    parser.add_argument("--tts", choices=["none", "mock", "syn6288"], default=None, help="TTS backend override.")
    parser.add_argument("--tts-port", default=None, help="SYN6288 serial port override.")
    parser.add_argument("--tts-baudrate", type=int, default=None, help="SYN6288 baudrate override.")
    parser.add_argument("--tts-volume", type=int, default=None, help="SYN6288 volume override.")
    parser.add_argument("--no-log", action="store_true", help="Do not append command log.")
    args = parser.parse_args()

    service_status = load_json(Path(args.service_status), {})
    if not isinstance(service_status, dict):
        service_status = {}
    latest = latest_transaction(Path(args.records))
    command = normalize_command(args.command)
    response = build_response(command, service_status, latest)

    tts = runtime_tts_defaults(Path(args.runtime_config))
    if args.tts is not None:
        tts["backend"] = args.tts
    if args.tts_port is not None:
        tts["port"] = args.tts_port
    if args.tts_baudrate is not None:
        tts["baudrate"] = args.tts_baudrate
    if args.tts_volume is not None:
        tts["volume"] = args.tts_volume

    print(response)
    speak_text(
        response,
        backend=tts["backend"],
        port=tts["port"],
        baudrate=tts["baudrate"],
        encoding=tts["encoding"],
        music=tts["music"],
        volume=tts["volume"],
        music_volume=tts["music_volume"],
        speed=tts["speed"],
    )

    if not args.no_log:
        append_jsonl(Path(args.command_log), {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "command": args.command,
            "normalized_command": command,
            "response": response,
            "tts_backend": tts["backend"],
        })
    return 0


# python voice_accessibility.py 直接执行时跑 main()；被 import 时不跑（保留模块可用性）
if __name__ == "__main__":
    raise SystemExit(main())
