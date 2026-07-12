"""Text-to-speech output backends for the smart scale demo."""

from __future__ import annotations


# TTS 后端路由：按 backend 参数分派到具体实现（none=静默 / mock=控制台打印 / syn6288=硬件串口）
def speak_text(
    text: str,
    backend: str = "none",
    port: str = "/dev/ttyS1",
    baudrate: int = 9600,
    encoding: str = "gb2312",
    music: int = 0,
    volume: int | None = None,
    music_volume: int | None = None,
    speed: int | None = None,
) -> dict | None:
    if backend == "none":
        return None
    if backend == "mock":
        print(f"TTS mock: {text}")
        return None
    if backend == "syn6288":
        from syn6288_tts import speak_syn6288

        frame, response = speak_syn6288(
            text=text,
            port=port,
            baudrate=baudrate,
            encoding=encoding,
            music=music,
            volume=volume,
            music_volume=music_volume,
            speed=speed,
        )
        return {"frame": frame, "response": response}
    raise ValueError(f"Unsupported TTS backend: {backend}")
