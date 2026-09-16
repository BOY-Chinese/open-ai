# -*- coding: utf-8 -*-
"""
app_paths.py — 安装根与派生路径的**唯一定义处**
================================================
为什么需要这个文件: 本项目有两种运行形态, 而它们的"根"不是同一个地方。

  源码形态  `python main.py` / `.venv` + runtime\\Scripts\\*.exe shim
            根 = 仓库目录, `__file__` 的所在目录就是根。

  打包形态  open-ai-gateway.exe / open-ai-daemon.exe / open-ai-task.exe
            PyInstaller onefile 会把代码解包到 `%TEMP%\\_MEIxxxxx\\`,
            于是 **`__file__` 指向的是那个临时目录, 不是安装根**。

打包形态下若拿 `__file__` 当根, 后果是 config.json 读不到、data/ 与 logs/
被写进临时目录并在进程退出时被整体清掉 —— 表现为「装好了但界面上什么都没有,
日志目录也是空的」。这类错误不会抛异常 (读不到就返回空 dict), 排查成本极高。

判别只依赖一个事实: **frozen 时 `sys.executable` 就是安装根里的那个 exe**。

各模块请从这里取根, 不要再各自 `os.path.dirname(__file__)` 拼 '..'。
"""
import os
import sys

FROZEN = bool(getattr(sys, 'frozen', False))

if FROZEN:
    # 打包态: 安装根 = exe 所在目录 (open-ai-gateway.exe / open-ai-daemon.exe 都在根)
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 源码态: 本文件就在仓库根
    ROOT = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(ROOT, 'config.json')
DATA_DIR = os.path.join(ROOT, 'data')
LOGS_DIR = os.path.join(ROOT, 'logs')
SCRIPTS_DIR = os.path.join(ROOT, 'scripts')
RUNTIME_DIR = os.path.join(ROOT, 'runtime')
SHIM_DIR = os.path.join(ROOT, 'runtime', 'Scripts')
VENV_DIR = os.path.join(ROOT, '.venv')

# Playwright 浏览器缓存的**本地**落点 (安装包把该目录整份解到 <root>\ms-playwright)。
#
# 为什么要显式指路: 打包态既没有 pip 也不会跑 `playwright install`, 浏览器二进制
# 由安装包随 resources.zip 带下来。Playwright 默认只认 `%LOCALAPPDATA%\ms-playwright`
# 或环境变量 PLAYWRIGHT_BROWSERS_PATH —— 不设的话它会认为"浏览器未安装",
# 登录助手启动失败后**静默降级到系统 Edge** (国际版 X/OAuth 在那里会卡死, 且不报错)。
PLAYWRIGHT_BROWSERS_DIR = os.path.join(ROOT, 'ms-playwright')


def ensure_playwright_browsers_path():
    """把内嵌的浏览器目录暴露给 Playwright (幂等, 可重复调用)。

    仅当目录**确实存在**时才设置: 源码态开发者机器上通常没有 <root>\\ms-playwright,
    此时保持不设, 让 Playwright 走自己的默认缓存 (开发者自行
    `python -m playwright install chromium`)。

    返回最终生效的路径 (未设置时为 '')。
    """
    if not os.path.isdir(PLAYWRIGHT_BROWSERS_DIR):
        return ''
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = PLAYWRIGHT_BROWSERS_DIR
    return PLAYWRIGHT_BROWSERS_DIR

# 品牌化 exe 的落点:
#   源码态 —— procname.py 在 runtime\Scripts\ 下生成 shim (复制解释器 + 注入图标)
#   打包态 —— 品牌 exe 直接躺在安装根, 没有 runtime\Scripts\ 这一层
EXE_DIR = ROOT if FROZEN else SHIM_DIR


def ensure_dirs():
    """确保 data/ 与 logs/ 存在 (Broker 与 task 都会写)。"""
    for d in (DATA_DIR, LOGS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass


def script_path(name):
    """scripts/<name> 的路径。

    ★ 打包态下这个文件**并不存在** —— 安装包不含 .py 源码。它只作为「路由标记」
      传给 open-ai-task.exe / open-ai-gateway.exe, 由对端按**文件名**路由到
      已打包进 exe 的模块 (见 scripts/task_main.py 与 main.py 的 __main__)。
      所以不要在这里加 os.path.isfile() 判断来"优化", 加了反而会把定时任务
      与网关自愈整个关掉。
    """
    return os.path.join(SCRIPTS_DIR, name)


def exe_path(name):
    """安装根/EXE_DIR 下的品牌 exe 绝对路径。"""
    return os.path.join(EXE_DIR, name)
