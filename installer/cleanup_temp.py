#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_temp.py — 清扫本项目遗留的 PyInstaller `_MEIxxxxxx` 临时目录
=====================================================================
背景 (为什么会有这些垃圾):
  品牌 exe 用 PyInstaller **onefile** 打包 —— 每次启动都把内嵌资源解到
  `%TEMP%\\_MEIxxxxxx`，正常运行结束时由"父引导进程"删掉。但父进程只有在
  **活着等到子进程退出**之后才有机会执行清理；一旦被 `TerminateProcess` /
  `TerminateJobObject` 强杀 (本项目旧版 `bootstrap.py stop` 每次都这么干)，
  目录就永久遗留。目录名每次不同、没有任何自愈 → 只增不减。
  实测一台机器攒了 11 个、虚拟机攒了 28 个 (每个可达数十~数百 MB)。

  停止流程已修成"先等子进程自然退出、再兜底强杀" (见 bootstrap._wait_jobs_gone)，
  今后不再增长；本脚本负责把**存量**清掉。

★ 安全性设计 —— 绝不动正在使用中的目录:
    Windows 下，只要还有进程持有某目录 (作为当前目录 / 打开的句柄)，
    对该目录做 **重命名** 就会失败。所以这里先 `os.rename` 到一个临时名字:
      - 重命名成功 → 证明没有进程在用 → 删除
      - 重命名失败 → 说明有活的进程 → 跳过 (绝不删、绝不重试到强破)
    这样即使误判"陈旧"也不可能破坏正在运行的 open-ai。

只清理**确认属于本项目**的目录：目录里要有 `base_library.zip` 或 `python3xx.dll`
(PyInstaller 解包特征)，并且带有本项目的痕迹之一：
  - `runtime\\Scripts\\open-ai-*.exe[.stamp]` (旧版 bug 产物，正是要清的对象)，或
  - 文件名/内容里出现 `open-ai` 字样的 stamp / exe
不满足特征的下划线目录一律不碰 —— 别的程序也用 PyInstaller。

用法:
    python cleanup_temp.py             # 预览 (不删)
    python cleanup_temp.py --run       # 真删
    python cleanup_temp.py --run --all # 连"没有本项目痕迹但已可重命名"的也删
"""
import argparse
import os
import re
import shutil
import sys
import time

MARKERS = re.compile(r'^(base_library\.zip|python3\d+\.dll)$', re.IGNORECASE)
PROJECT_HINT = re.compile(r'open[-_.]?ai', re.IGNORECASE)
MEI_RE = re.compile(r'^_MEI', re.IGNORECASE)

# ★ 为什么不能只按名字认"我们的"目录: PyInstaller 把业务模块 (fastapi/uvicorn/
#   main/app_runtime…) 都收进 PYZ 归档, **解包目录里根本没有同名文件** ——
#   实测 162 MB 那个目录里搜 open_ai/fastapi/uvicorn 全部 0 命中。
#   能区分的是**依赖组合**: 本项目独有的一批 loose 依赖
#   (playwright 登录、websockets+zstandard 走 uvicorn[standard]、yaml、PIL、
#    sqlite3/_sqlite3.pyd 流水库) 会真实落地。
OUR_DEP_HINTS = ('playwright', 'websockets', 'zstandard', 'yaml', 'PIL',
                 'psutil', 'pydantic_core', '_sqlite3.pyd', 'sqlite3.dll')

def temp_root():
    return os.path.normpath(
        os.environ.get('TEMP') or os.environ.get('TMP')
        or os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Temp'))


def classify(path):
    """把一个 _MEI 目录分级: 'ours' / 'unknown'。

    'ours' 的判据 (任一成立即可, 都要求目录确实是 PyInstaller 解包物):
      1) 里有名字含 open-ai 的东西 (品牌 shim / 图标)
      2) 有旧版 bug 的产物 runtime\\Scripts\\open-ai*.exe[.stamp]
      3) 有 base_library.zip 且命中 >= 3 个本项目特征依赖
         (单命中不够 —— 别的 playwright 工具也会中一条)
    其余一律 'unknown': 只报告不删除, 交给 --all 由人决定。
    """
    try:
        entries = os.listdir(path)
    except OSError:
        return 'unknown'
    if not entries:
        return 'unknown'
    names = set(entries)
    if any(PROJECT_HINT.search(e) for e in entries):
        return 'ours'
    rs = os.path.join(path, 'runtime', 'Scripts')
    if os.path.isdir(rs):
        try:
            if any(PROJECT_HINT.search(f) for f in os.listdir(rs)):
                return 'ours'
        except OSError:
            pass
    if not any(MARKERS.match(e) for e in entries):
        return 'unknown'
    hits = sum(1 for h in OUR_DEP_HINTS if h.lower() in {e.lower() for e in names})
    return 'ours' if hits >= 3 else 'unknown'


def dir_size(path):
    total = 0
    try:
        for r, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def try_remove(path, ours_only=True):
    """返回 (状态, 说明)。状态: removed / busy / skipped / error"""
    if ours_only and classify(path) != 'ours':
        return 'skipped', '认不出是本项目, 不碰 (要清理请加 --all)'
    probe = path + '.openai-cleanup-%d' % os.getpid()
    try:
        os.rename(path, probe)          # 有进程占用 → 这里必然失败
    except OSError:
        return 'busy', '正在被使用 (有进程持有句柄), 已跳过'
    try:
        shutil.rmtree(probe, ignore_errors=True)
        if os.path.exists(probe):
            os.rename(probe, path)      # 没删干净 → 原样放回, 别留半个目录
            return 'error', '重命名成功但删除未完成, 已放回原状'
        return 'removed', ''
    except OSError as e:
        try:
            os.rename(probe, path)
        except OSError:
            pass
        return 'error', str(e)


def main():
    ap = argparse.ArgumentParser(description='清扫本项目遗留的 _MEI 临时目录')
    ap.add_argument('--run', action='store_true', help='真正删除 (缺省只预览)')
    ap.add_argument('--all', action='store_true',
                    help='连"认不出本项目痕迹"的 _MEI* 也尝试删除 (慎用)')
    args = ap.parse_args()

    root = temp_root()
    if not os.path.isdir(root):
        print('[ERROR] 找不到 TEMP 目录: %r' % root)
        return 1

    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        print('[ERROR] 无法列出 TEMP: %s' % e)
        return 1

    cand = [n for n in names if MEI_RE.match(n)
            and os.path.isdir(os.path.join(root, n))]
    print('TEMP: %s' % root)
    print('发现 _MEI* 目录 %d 个%s\n' % (len(cand), '' if args.run else '  (预览模式)'))

    removed = busy = skipped = 0
    freed = 0
    for n in cand:
        full = os.path.join(root, n)
        try:
            age_days = (time.time() - os.path.getmtime(full)) / 86400.0
        except OSError:
            age_days = -1
        size_mb = dir_size(full) / 1048576.0
        if not args.run:
            kind = classify(full)
            verdict = {'ours': '本项目(默认会删)', 'unknown': '认不出(仅 --all 才删)'}[kind]
            if not os.listdir(full):
                verdict = '空目录(无内容, 删了无害)'
            print('  [预览] %-22s %7.1f MB  %5.1f 天前  %s'
                  % (n, size_mb, age_days, verdict))
            if kind == 'ours':
                freed += int(size_mb * 1048576)
            continue
        st, msg = try_remove(full, ours_only=not args.all)
        if st == 'removed':
            freed += 0
        if st == 'removed':
            removed += 1
            print('  [删除] %s' % n)
        elif st == 'busy':
            busy += 1
            print('  [占用] %s — %s' % (n, msg))
        elif st == 'skipped':
            skipped += 1
        else:
            print('  [失败] %s — %s' % (n, msg))

    if args.run:
        print('\n删除 %d 个, 占用跳过 %d 个, 认不出跳过 %d 个'
              % (removed, busy, skipped))
    else:
        print('\n预计可释放 %.1f MB (仅计"本项目"级)。未做任何修改, 确认后加 --run。'
              % (freed / 1048576.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
