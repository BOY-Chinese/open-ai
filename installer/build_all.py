#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_all.py — open-ai 完整打包（v3.0，自动化，无 pause）
==========================================================
与 build_exe.bat 等价，但：
  * 由 Python 驱动，规避 PowerShell `-File` 对含中文路径的拒绝
    （安装目录为 D:\\MyCode\\项目\\open-ai-github-dev 时必需）
  * 全程无交互，便于脚本化/CI

步骤：uninstall(onedir) -> launcher(onedir) -> resources.zip(含桌面端) -> installer(onefile)
用法：python build_all.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "build_installer.log")
ICON = os.path.join(HERE, "ico", "open-ai.ico")


def log(msg):
    print(msg, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def run(args, step):
    """运行并同时写日志；返回是否成功。"""
    log(f"--- [{step}] {' '.join(args)}")
    with open(LOG, "a", encoding="utf-8", errors="replace") as lf:
        p = subprocess.run(args, cwd=HERE, stdout=lf,
                           stderr=subprocess.STDOUT, text=True)
    return p.returncode == 0


def ensure_pyinstaller():
    for pkg in ("pyinstaller", "pefile", "pywin32-ctypes"):
        r = subprocess.run([sys.executable, "-m", "pip", "show", pkg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  安装 {pkg} ...")
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"],
                           cwd=HERE)


def main():
    open(LOG, "w", encoding="utf-8").write(
        f"==== open-ai installer build (v3.0) {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")

    ensure_pyinstaller()
    icon_args = ["--icon", ICON] if os.path.isfile(ICON) else []

    steps = [
        ("1/4 uninstall.exe (onedir)",
         [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
          "--windowed", "--contents-directory", "uninstall_internal",
          "--name", "uninstall", *icon_args, "uninstaller.py"]),
        ("2/4 open-ai-launcher.exe (onedir)",
         [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
          "--windowed", "--contents-directory", "launcher_internal",
          "--name", "open-ai-launcher", *icon_args, "launcher.py"]),
    ]

    for step, args in steps:
        log(f"[{step}] 开始")
        if not run(args, step):
            log(f"[ERROR] {step} 失败")
            return 1
        log(f"[{step}] 完成")

    # resources.zip（内含 desktop/ 桌面端）
    log("[3/4] resources.zip（含 desktop/ 桌面端）")
    if not run([sys.executable, "build_resources.py"], "3/4 resources"):
        log("[ERROR] resources 打包失败")
        return 1

    # 安装器（onefile）
    log("[4/4] open-ai-installer-dev.exe")
    if not run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
        "--onefile", "--windowed", "--uac-admin",
        "--name", "open-ai-installer-dev", *icon_args,
        "--add-data", "resources.zip;.",
        "--add-data", "config.shell.json;.",
        "--add-data", "ico\\open-ai.ico;ico",
        "installer.py",
    ], "4/4 installer"):
        log("[ERROR] 安装器构建失败")
        return 1

    out = os.path.join(HERE, "dist", "open-ai-installer-dev.exe")
    if os.path.isfile(out):
        log(f"[DONE] {out} ({os.path.getsize(out)/1024/1024:.1f} MB)")
        return 0
    log("[ERROR] 未生成安装器")
    return 1


if __name__ == "__main__":
    sys.exit(main())
