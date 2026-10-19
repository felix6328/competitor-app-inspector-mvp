#!/usr/bin/env python3
"""Fast, actionable environment check for the inspection MVP."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PACKAGES = {
    "uiautomator2": "uiautomator2",
    "PyYAML": "yaml",
    "Pillow": "PIL",
}
APP_PACKAGES = {
    "淘宝": "com.taobao.taobao",
    "京东": "com.jingdong.app.mall",
    "抖音": "com.ss.android.ugc.aweme",
    "美团": "com.sankuai.meituan",
}


def command_output(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=8).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="检查竞品巡检项目的本机依赖和 Android 真机连接")
    parser.add_argument("--allow-no-device", action="store_true", help="没有连接手机时仍以成功退出，适合安装后检查")
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []
    print(f"项目目录: {ROOT}")
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 9):
        failures.append("Python 需要 3.9 或更高版本")

    for label, module in PACKAGES.items():
        if importlib.util.find_spec(module) is None:
            failures.append(f"缺少 Python 依赖: {label}（执行 pip install -r requirements.txt）")
        else:
            print(f"依赖: {label} OK")

    # Validate the safety contract as soon as PyYAML is available. This catches
    # accidental edits to app selectors before a real device is touched.
    if importlib.util.find_spec("yaml") is not None:
        try:
            import yaml  # type: ignore
            config = yaml.safe_load((ROOT / "config" / "apps.yaml").read_text(encoding="utf-8"))
            prohibited = set(config.get("defaults", {}).get("prohibited_action_texts", []))
            if not prohibited:
                failures.append("config/apps.yaml 没有支付提交拦截词")
            for key, app in config.get("apps", {}).items():
                candidates = app.get("checkout_entry_texts", []) or []
                unsafe = sorted(set(candidates) & prohibited)
                if unsafe:
                    failures.append(f"App {key} 的 checkout_entry_texts 含高危文案: {', '.join(unsafe)}")
            print("安全配置: checkout 入口未包含支付提交文案")
        except Exception as exc:
            failures.append(f"无法解析 config/apps.yaml: {exc}")

    adb = shutil.which("adb")
    local_adb = ROOT / "work" / "android-tools" / "platform-tools" / "adb"
    if not adb and local_adb.exists():
        adb = str(local_adb)
        print(f"使用项目内 ADB: {adb}")
    if not adb:
        failures.append("找不到 adb（macOS 执行 brew install android-platform-tools）")
        devices: list[tuple[str, str]] = []
    else:
        print(f"adb: {adb}")
        raw = command_output([adb, "devices"])
        devices = []
        in_device_table = False
        for line in raw.splitlines():
            if line.strip().startswith("List of devices attached"):
                in_device_table = True
                continue
            if not in_device_table or line.strip().startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append((parts[0], parts[1]))
        if not devices:
            warnings.append("adb 已安装，但没有发现 Android 手机")
            if not args.allow_no_device:
                failures.append("未发现 Android 手机；连接并授权 USB 调试后再运行巡检")
        for serial, state in devices:
            print(f"设备: {serial} [{state}]")
            if state != "device":
                failures.append(f"设备 {serial} 状态为 {state}，请在手机上允许 USB 调试")

        ready = next((serial for serial, state in devices if state == "device"), None)
        if ready:
            for name, package in APP_PACKAGES.items():
                installed = bool(command_output([adb, "-s", ready, "shell", "pm", "path", package]))
                print(f"App: {name} {'已安装' if installed else '未安装/包名需核对'}")
                if not installed:
                    warnings.append(f"{name} 未检测到（{package}），该 App 将无法巡检")

    if warnings:
        print("\n提示:")
        for item in warnings:
            print(f"  - {item}")
    if failures:
        print("\n需要处理:")
        for item in failures:
            print(f"  - {item}")
        if args.allow_no_device and all("没有发现 Android 手机" not in item for item in failures):
            return 1
        if args.allow_no_device:
            return 0
        return 1

    print("\n预检通过，可以运行: python runner.py --query \"洗衣液\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
