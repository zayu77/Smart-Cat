"""Read an HX711 load-cell ADC through Linux GPIO sysfs."""

from __future__ import annotations

import argparse
import errno
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DOUT_GPIO = 134  # GPIO4_A6 = 4 * 32 + 6
DEFAULT_SCK_GPIO = 132  # GPIO4_A4 = 4 * 32 + 4


# HX711 校准参数：零点偏移 + 每克对应的原始 ADC 计数
@dataclass
class HX711Config:
    dout_gpio: int = DEFAULT_DOUT_GPIO
    sck_gpio: int = DEFAULT_SCK_GPIO
    dout_chip: str = "gpiochip4"
    dout_line: int = 6
    sck_chip: str = "gpiochip4"
    sck_line: int = 4
    offset: float = 0.0
    scale: float = 1.0  # raw counts per gram


# 通过 Linux sysfs 文件接口操作 GPIO（兼容性最好，但需要 root + 手动 export）
class SysfsGPIO:
    # 导出 GPIO 到 /sys/class/gpio 并设置方向（in/out）
    def __init__(self, number: int, direction: str) -> None:
        self.number = number
        self.path = Path("/sys/class/gpio") / f"gpio{number}"
        if not self.path.exists():
            export = Path("/sys/class/gpio/export")
            try:
                export.write_text(str(number), encoding="ascii")
            except PermissionError as exc:
                raise PermissionError("GPIO export failed. Run this script with sudo.") from exc
            time.sleep(0.1)
        (self.path / "direction").write_text(direction, encoding="ascii")
        self.value_path = self.path / "value"

    # 读引脚当前电平（0 或 1）
    def read(self) -> int:
        return 1 if self.value_path.read_text(encoding="ascii").strip() == "1" else 0

    # 设置引脚输出电平
    def write(self, value: int) -> None:
        self.value_path.write_text("1" if value else "0", encoding="ascii")


# 通过 libgpiod v1.x API 操作 GPIO（旧版 Debian/Ubuntu 系统）
class GpiodV1GPIO:
    # 申请并设置引脚方向（in / out）
    def __init__(self, chip_name: str, line_offset: int, direction: str) -> None:
        import gpiod

        self.gpiod = gpiod
        self.chip = gpiod.Chip(chip_name)
        self.line = self.chip.get_line(line_offset)
        try:
            if direction == "in":
                self.line.request(consumer="smart-cat-hx711", type=gpiod.LINE_REQ_DIR_IN)
            else:
                self.line.request(consumer="smart-cat-hx711", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise RuntimeError(
                    f"{chip_name} line {line_offset} is busy. If this GPIO was exported by sysfs, run "
                    "`echo <gpio_number> | sudo tee /sys/class/gpio/unexport` before using gpiod."
                ) from exc
            raise

    # 读引脚当前电平（0 或 1）
    def read(self) -> int:
        return int(self.line.get_value())

    # 设置引脚输出电平
    def write(self, value: int) -> None:
        self.line.set_value(1 if value else 0)


# 通过 libgpiod v2.x API 操作 GPIO（新版，鲁班猫系统推荐）
class GpiodV2GPIO:
    # 申请并设置引脚方向（in / out）
    def __init__(self, chip_name: str, line_offset: int, direction: str) -> None:
        import gpiod
        from gpiod.line import Direction, Value

        line_settings = gpiod.LineSettings(
            direction=Direction.INPUT if direction == "in" else Direction.OUTPUT,
            output_value=Value.INACTIVE,
        )
        self.offset = line_offset
        self.value_active = Value.ACTIVE
        self.value_inactive = Value.INACTIVE
        try:
            self.request = gpiod.request_lines(
                f"/dev/{chip_name}",
                consumer="smart-cat-hx711",
                config={line_offset: line_settings},
            )
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise RuntimeError(
                    f"{chip_name} line {line_offset} is busy. If this GPIO was exported by sysfs, run "
                    "`echo <gpio_number> | sudo tee /sys/class/gpio/unexport` before using gpiod."
                ) from exc
            raise

    # 读引脚当前电平（0 或 1）
    def read(self) -> int:
        return 1 if self.request.get_value(self.offset) == self.value_active else 0

    # 设置引脚输出电平
    def write(self, value: int) -> None:
        self.request.set_value(self.offset, self.value_active if value else self.value_inactive)


# 自动检测 gpiod 版本（v1.x / v2.x）并返回对应 GPIO 包装实例
def make_gpiod_gpio(chip_name: str, line_offset: int, direction: str):
    try:
        import gpiod
    except ImportError as exc:
        raise SystemExit("Python gpiod is not installed. On LubanCat try: sudo apt install python3-libgpiod") from exc

    if hasattr(gpiod, "request_lines"):
        return GpiodV2GPIO(chip_name, line_offset, direction)
    return GpiodV1GPIO(chip_name, line_offset, direction)


# HX711 24-bit ADC 协议封装：DOUT 数据线 + SCK 时钟线，通过 24 次位操作读 ADC
class HX711:
    # 初始化两路 GPIO：DOUT 输入（数据）、SCK 输出（时钟），SCK 默认拉低
    def __init__(
        self,
        dout_gpio: int,
        sck_gpio: int,
        backend: str = "sysfs",
        dout_chip: str = "gpiochip4",
        dout_line: int = 6,
        sck_chip: str = "gpiochip4",
        sck_line: int = 4,
    ) -> None:
        if backend == "gpiod":
            self.dout = make_gpiod_gpio(dout_chip, dout_line, "in")
            self.sck = make_gpiod_gpio(sck_chip, sck_line, "out")
        else:
            self.dout = SysfsGPIO(dout_gpio, "in")
            self.sck = SysfsGPIO(sck_gpio, "out")
        self.sck.write(0)

    # 阻塞等待 HX711 转换完成（DOUT 拉低 = 数据就绪），超时返回 False
    def wait_ready(self, timeout: float = 1.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.dout.read() == 0:
                return True
            time.sleep(0.001)
        return False

    # 读一次 24-bit 原始 ADC 值（24 个数据脉冲 + 1 个通道选择脉冲，2's complement 解析）
    def read_raw_once(self, timeout: float = 1.0) -> int:
        if not self.wait_ready(timeout=timeout):
            raise TimeoutError("HX711 not ready. Check wiring, power, and DOUT pin.")

        value = 0
        for _ in range(24):
            self.sck.write(1)
            value = (value << 1) | self.dout.read()
            self.sck.write(0)

        # One extra pulse selects channel A, gain 128 for the next conversion.
        self.sck.write(1)
        self.sck.write(0)

        if value & 0x800000:
            value -= 0x1000000
        return value

    # 连续读 N 个原始样本（每次间隔 interval 秒），返回原始值列表
    def read_values(self, samples: int = 10, interval: float = 0.05) -> list[int]:
        values = []
        for _ in range(samples):
            values.append(self.read_raw_once())
            time.sleep(interval)
        return values

    # 读 N 个样本后做中位数 + 离群值过滤，返回滤波后的代表值
    def read_average(self, samples: int = 10, interval: float = 0.05) -> float:
        return filtered_raw(self.read_values(samples=samples, interval=interval))


# 从 JSON 文件加载校准配置；文件不存在或字段缺失时用入参兜底
def load_config(path: Path, dout_gpio: int, sck_gpio: int) -> HX711Config:
    if not path.exists():
        return HX711Config(dout_gpio=dout_gpio, sck_gpio=sck_gpio)
    data = json.loads(path.read_text(encoding="utf-8"))
    return HX711Config(
        dout_gpio=int(data.get("dout_gpio", dout_gpio)),
        sck_gpio=int(data.get("sck_gpio", sck_gpio)),
        dout_chip=str(data.get("dout_chip", "gpiochip4")),
        dout_line=int(data.get("dout_line", 6)),
        sck_chip=str(data.get("sck_chip", "gpiochip4")),
        sck_line=int(data.get("sck_line", 4)),
        offset=float(data.get("offset", 0.0)),
        scale=float(data.get("scale", 1.0)),
    )


# 把当前校准配置写回 JSON 文件（覆盖式）
def save_config(path: Path, config: HX711Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")


# 把原始 ADC 值换算成克：克 = (raw - offset) / scale
def grams_from_raw(raw: float, config: HX711Config) -> float:
    if abs(config.scale) < 1e-9:
        raise ValueError("Scale is zero. Run tare first, then place a known weight and calibrate again.")
    return (raw - config.offset) / config.scale


# 中位数 + 离群值过滤（剔除偏离中位数超过 max_deviation 的样本）
def filtered_raw(values: list[int], max_deviation: float = 5000.0) -> float:
    if not values:
        raise ValueError("No HX711 samples were read.")
    median = float(statistics.median(values))
    filtered = [value for value in values if abs(value - median) <= max_deviation]
    if not filtered:
        filtered = values
    return float(statistics.median(filtered))


# 根据样本模式打印常见故障的诊断提示（全 -1 = 接线问题，全相同 = 信号无变化）
def print_diagnostics(values: list[int]) -> None:
    if not values:
        return
    unique = sorted(set(values))
    if len(unique) == 1 and unique[0] == -1:
        print(
            "Diagnostic: all samples are -1. DOUT is ready, but the 24-bit data is all 1s. "
            "This is often caused by HX711 input wiring problems or GPIO bit-banging that keeps SCK high too long."
        )
    elif len(unique) == 1:
        print(
            "Diagnostic: all samples are identical. If pressing the load cell does not change this value, "
            "check the load-cell wiring, force structure, DOUT/SCK pins, and HX711 power."
        )


# CLI 入口：支持读原始值 / 去皮（--tare）/ 标定（--known-weight-g）/ 显示克重
def main() -> int:
    parser = argparse.ArgumentParser(description="Read HX711 raw values and calibrated weight.")
    parser.add_argument("--gpio-backend", choices=["gpiod", "sysfs"], default="gpiod", help="GPIO backend.")
    parser.add_argument("--dout-gpio", type=int, default=DEFAULT_DOUT_GPIO, help="DOUT GPIO number. Default GPIO4_A6=134.")
    parser.add_argument("--sck-gpio", type=int, default=DEFAULT_SCK_GPIO, help="SCK GPIO number. Default GPIO4_A4=132.")
    parser.add_argument("--dout-chip", default="gpiochip4", help="DOUT GPIO chip for gpiod backend.")
    parser.add_argument("--dout-line", type=int, default=6, help="DOUT line offset for gpiod backend.")
    parser.add_argument("--sck-chip", default="gpiochip4", help="SCK GPIO chip for gpiod backend.")
    parser.add_argument("--sck-line", type=int, default=4, help="SCK line offset for gpiod backend.")
    parser.add_argument("--config", default="config/hx711_scale.json", help="Calibration config path.")
    parser.add_argument("--samples", type=int, default=10, help="Samples per reading.")
    parser.add_argument("--interval", type=float, default=0.05, help="Delay between samples.")
    parser.add_argument("--max-deviation", type=float, default=5000.0, help="Raw-count deviation used to filter outliers.")
    parser.add_argument("--print-samples", action="store_true", help="Print each raw sample before averaging.")
    parser.add_argument("--tare", action="store_true", help="Measure current no-load offset.")
    parser.add_argument("--known-weight-g", type=float, default=None, help="Known calibration weight in grams.")
    parser.add_argument(
        "--min-calibration-delta",
        type=float,
        default=100.0,
        help="Minimum raw-count change required when calibrating with a known weight.",
    )
    parser.add_argument("--save-config", action="store_true", help="Save measured offset/scale to config.")
    parser.add_argument("--raw", action="store_true", help="Only print raw average.")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path, dout_gpio=args.dout_gpio, sck_gpio=args.sck_gpio)
    config.dout_chip = args.dout_chip
    config.dout_line = args.dout_line
    config.sck_chip = args.sck_chip
    config.sck_line = args.sck_line
    hx711 = HX711(
        config.dout_gpio,
        config.sck_gpio,
        backend=args.gpio_backend,
        dout_chip=config.dout_chip,
        dout_line=config.dout_line,
        sck_chip=config.sck_chip,
        sck_line=config.sck_line,
    )

    values = hx711.read_values(samples=args.samples, interval=args.interval)
    if args.print_samples:
        for index, value in enumerate(values, start=1):
            print(f"sample[{index}]: {value}")
    raw = filtered_raw(values, max_deviation=args.max_deviation)
    print(f"Raw filtered: {raw:.2f}")
    print(f"Raw mean: {statistics.mean(values):.2f}")
    print_diagnostics(values)

    if args.tare:
        config.offset = raw
        print(f"Tare offset: {config.offset:.2f}")
        if args.save_config:
            save_config(config_path, config)
            print(f"Saved config: {config_path}")
        return 0

    if args.known_weight_g is not None:
        if args.known_weight_g <= 0:
            raise ValueError("--known-weight-g must be positive.")
        delta = raw - config.offset
        if abs(delta) < args.min_calibration_delta:
            raise RuntimeError(
                "Calibration raw value barely changed from tare offset. "
                "Place the known weight on the scale, check load-cell wiring, or lower --min-calibration-delta."
            )
        config.scale = delta / args.known_weight_g
        print(f"Known weight: {args.known_weight_g:.2f} g")
        print(f"Raw delta: {delta:.2f}")
        print(f"Scale: {config.scale:.6f} raw/g")
        if args.save_config:
            save_config(config_path, config)
            print(f"Saved config: {config_path}")
        return 0

    if not args.raw:
        weight_g = grams_from_raw(raw, config)
        print(f"Weight: {weight_g:.2f} g")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
