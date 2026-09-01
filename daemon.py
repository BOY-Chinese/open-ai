# -*- coding: utf-8 -*-
"""
daemon.py — open-ai 常驻进程入口 (v2.4 起 = 进程 Broker)
==========================================================
自 v2.4 进程架构重构后, 本文件只是 app_runtime.py (Broker) 的薄启动入口:

  - 旧职责"自愈拉起网关/Node"  → Broker 监督循环 (父子进程树 + Job 托管)
  - 旧职责"每日签到/流水采集" → Broker 内置 TaskScheduler (task shim 托管)
  - 旧职责"看门狗保活"        → Job Object KILL_ON_JOB_CLOSE + watchdog_boot.py

保留此文件是为了兼容: 计划任务 / 旧快捷方式 / 用户手动 `pythonw daemon.py`。
推荐统一入口: bootstrap.py (open-ai.exe shim)。

后台无窗口运行:
    .venv\\Scripts\\open-ai-daemon.exe        (推荐, 任务管理器显示 open-ai 品牌信息)
    .venv\\Scripts\\pythonw.exe daemon.py     (兼容, 名字显示为 pythonw.exe)
"""
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from app_runtime import main

if __name__ == '__main__':
    sys.exit(main())
