"""Send Chinese text to a SYN6288 TTS module over UART."""

from __future__ import annotations

import argparse
import time


COMMAND_SYNTHESIZE = 0x01

ENCODINGS = {
    "gb2312": (0x01, "gb18030"),
    "gbk": (0x01, "gb18030"),
    "big5": (0x02, "big5"),
    "unicode": (0x03, "utf-16-be"),
}


# 对字节序列做 XOR 校验和：所有字节按位异或，结果作为帧尾的 1 字节校验位
def xor_checksum(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


# 按 SYN6288 协议组装一帧：帧头(0xFD) + 长度 + 命令字 + 命令参数 + 文本 + 可选校验和
def build_syn6288_frame(
    text: str,
    encoding: str = "gb2312",
    command: int = COMMAND_SYNTHESIZE,
    music: int = 0,
    checksum: bool = True,
) -> bytes:
    if encoding not in ENCODINGS:
        raise ValueError(f"Unsupported SYN6288 encoding: {encoding}")
    if not 0 <= music <= 15:
        raise ValueError("SYN6288 music must be in range 0..15.")

    encoding_flag, python_encoding = ENCODINGS[encoding]
    command_param = encoding_flag | (music << 4)
    text_bytes = text.encode(python_encoding)
    payload = bytes([command, command_param]) + text_bytes
    if checksum:
        payload_length = len(payload) + 1
    else:
        payload_length = len(payload)
    if payload_length > 0xFFFF:
        raise ValueError("SYN6288 frame is too long.")
    frame = bytes([0xFD, (payload_length >> 8) & 0xFF, payload_length & 0xFF]) + payload
    if checksum:
        frame += bytes([xor_checksum(frame)])
    return frame


# 在文本前拼接 TTS 控制标签（音量 [v] / 背景音 [m] / 语速 [t]），用于单次发送时调整播报效果
def with_control_tags(text: str, volume: int | None = None, music_volume: int | None = None, speed: int | None = None) -> str:
    tags = []
    if volume is not None:
        if not 0 <= volume <= 16:
            raise ValueError("SYN6288 volume must be in range 0..16.")
        tags.append(f"[v{volume}]")
    if music_volume is not None:
        if not 0 <= music_volume <= 16:
            raise ValueError("SYN6288 music volume must be in range 0..16.")
        tags.append(f"[m{music_volume}]")
    if speed is not None:
        if not 0 <= speed <= 5:
            raise ValueError("SYN6288 speed must be in range 0..5.")
        tags.append(f"[t{speed}]")
    return "".join(tags) + text


# 打开 UART 串口 → 发送合成帧 → 等待 → 可选读取 SYN6288 返回的状态字节
def speak_syn6288(
    text: str,
    port: str = "/dev/ttyS1",
    baudrate: int = 9600,
    encoding: str = "gb2312",
    music: int = 0,
    volume: int | None = None,
    music_volume: int | None = None,
    speed: int | None = None,
    checksum: bool = True,
    timeout: float = 1.0,
    post_delay: float = 0.05,
    read_response: bool = False,
    response_wait: float = 0.1,
) -> tuple[bytes, bytes]:
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("pyserial is not installed. Run: python -m pip install pyserial") from exc

    text = with_control_tags(text, volume=volume, music_volume=music_volume, speed=speed)
    frame = build_syn6288_frame(text, encoding=encoding, music=music, checksum=checksum)
    with serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity="N", stopbits=1, timeout=timeout) as uart:
        uart.reset_input_buffer()
        uart.write(frame)
        uart.flush()
        if post_delay > 0:
            time.sleep(post_delay)
        response = b""
        if read_response:
            if response_wait > 0:
                time.sleep(response_wait)
            waiting = uart.in_waiting
            if waiting:
                response = uart.read(waiting)
    return frame, response


# CLI 入口：合成 + 发送 TTS 帧；支持 dry-run（只打帧内容、不开串口）便于无硬件时调试
def main() -> int:
    parser = argparse.ArgumentParser(description="Speak text with a SYN6288 TTS module.")
    parser.add_argument("--text", required=True, help="Chinese text to synthesize.")
    parser.add_argument("--port", default="/dev/ttyS1", help="UART device path.")
    parser.add_argument("--baudrate", type=int, default=9600, help="UART baudrate.")
    parser.add_argument("--encoding", choices=sorted(ENCODINGS), default="gb2312", help="Text encoding flag.")
    parser.add_argument("--music", type=int, default=0, help="Background music index, 0 disables music.")
    parser.add_argument("--volume", type=int, default=None, help="Speech volume, 0-16.")
    parser.add_argument("--music-volume", type=int, default=None, help="Background music volume, 0-16.")
    parser.add_argument("--speed", type=int, default=None, help="Speech speed, 0-5.")
    parser.add_argument("--no-checksum", action="store_true", help="Send old no-checksum frame variant.")
    parser.add_argument("--timeout", type=float, default=1.0, help="Serial timeout seconds.")
    parser.add_argument("--read-response", action="store_true", help="Read bytes returned by SYN6288 after sending.")
    parser.add_argument("--response-wait", type=float, default=0.1, help="Seconds to wait before reading response bytes.")
    parser.add_argument("--dry-run", action="store_true", help="Only print the frame, do not open UART.")
    args = parser.parse_args()

    text = with_control_tags(
        args.text,
        volume=args.volume,
        music_volume=args.music_volume,
        speed=args.speed,
    )
    frame = build_syn6288_frame(
        text,
        encoding=args.encoding,
        music=args.music,
        checksum=not args.no_checksum,
    )
    print(f"Text: {text}")
    print(f"Port: {args.port}")
    print(f"Baudrate: {args.baudrate}")
    print(f"Encoding: {args.encoding}")
    print(f"Music: {args.music}")
    print(f"Checksum: {not args.no_checksum}")
    print(f"Frame: {frame.hex(' ')}")

    if not args.dry_run:
        sent_frame, response = speak_syn6288(
            text=args.text,
            port=args.port,
            baudrate=args.baudrate,
            encoding=args.encoding,
            music=args.music,
            volume=args.volume,
            music_volume=args.music_volume,
            speed=args.speed,
            checksum=not args.no_checksum,
            timeout=args.timeout,
            read_response=args.read_response,
            response_wait=args.response_wait,
        )
        print("Sent.")
        if args.read_response:
            print(f"Response: {response.hex(' ') if response else '<none>'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
