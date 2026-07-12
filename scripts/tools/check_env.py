"""Check the first-stage runtime environment on LubanCat.

这个脚本暂时没用了,主要是刚开始用来检查环境配置的
"""

from __future__ import annotations

import glob
import platform
import shutil
import subprocess
import sys


# 执行外部命令并捕获 stdout/stderr 合并输出,异常时返回 "not found"
def run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        return "not found"
    return result.stdout.strip() or "(no output)"


# 尝试导入模块,成功返回 __version__,失败返回错误信息
def import_version(module_name: str) -> str:
    try:
        module = __import__(module_name)
    except Exception as exc:  # noqa: BLE001 - environment probing should report all import errors.
        return f"not available: {exc}"
    return getattr(module, "__version__", "available")


# 打印系统/包版本、相机设备、可用 CLI 工具,以及 v4l2 设备列表
def main() -> int:
    print("== System ==")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    print()

    print("== Python Packages ==")
    print(f"numpy: {import_version('numpy')}")
    print(f"cv2: {import_version('cv2')}")
    print()

    print("== Camera Devices ==")
    devices = sorted(glob.glob("/dev/video*"))
    print("\\n".join(devices) if devices else "No /dev/video* devices found")
    print()

    print("== Useful Commands ==")
    for name in ("v4l2-ctl", "ffmpeg", "python3", "pip3"):
        path = shutil.which(name)
        print(f"{name}: {path or 'not found'}")
    print()

    if shutil.which("v4l2-ctl"):
        print("== v4l2 Devices ==")
        print(run(["v4l2-ctl", "--list-devices"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
