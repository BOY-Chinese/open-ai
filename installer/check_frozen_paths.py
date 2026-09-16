# -*- coding: utf-8 -*-
"""
check_frozen_paths.py — 打包前门禁: 运行时模块禁止裸 __file__ 拼路径
==================================================================
背景 (MEMORY.md「__file__ 相对路径 bug 家族」事故 #1/#3/#5, 2026-09-15 用户机复发):
PyInstaller onefile 打包后, 模块的 __file__ 指向 %TEMP%\\_MEIxxxx\\ 解包临时目录。
任何「os.path.dirname(__file__) 拼 config/data/logs」在装机后都会读错位置 ——
signin_all.py 因此在用户机上开机弹 FileNotFoundError 对话框, 且前一次人工
"全仓清扫" 漏掉了它 (grep 验证不可靠)。

规则: 运行时模块 (仓库根 *.py + scripts/ + providers/) 中出现的 __file__,
只允许存在于 except 兜底分支里 (源码态 fallback)。打包态路径一律走 app_paths。

白名单:
  app_paths.py     —— 根的唯一定义处: frozen 走 sys.executable, 源码态才用 __file__
  sanitize_check.py —— 构建期工具, 从源码仓运行, 永不打包

用法: python check_frozen_paths.py  (build_exe.bat 构建前调用, 退出码非 0 即中止)
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 免检文件: 见模块 docstring「白名单」
EXEMPT_FILES = {'app_paths.py', 'sanitize_check.py'}


def iter_targets():
    """运行时模块清单: 仓库根 *.py + scripts/*.py + providers/*.py (不含 tests/installer)。"""
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        if name.endswith('.py') and os.path.isfile(p):
            yield p
    for sub in ('scripts', 'providers'):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if name.endswith('.py') and os.path.isfile(p):
                yield p


def find_bare(tree):
    """返回裸 __file__ 的行号: 不处于任何 ast.ExceptHandler (except 兜底) 祖先链里。"""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == '__file__':
            cur, guarded = node, False
            while cur is not None:
                if isinstance(cur, ast.ExceptHandler):
                    guarded = True
                    break
                cur = parents.get(cur)
            if not guarded:
                bad.append(node.lineno)
    return bad


def main():
    violations = []
    scanned = 0
    for path in iter_targets():
        if os.path.basename(path) in EXEMPT_FILES:
            continue
        scanned += 1
        try:
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
        except (SyntaxError, UnicodeDecodeError) as e:
            print('[check_frozen_paths] WARN 无法解析 %s: %s' % (path, e))
            continue
        for ln in find_bare(tree):
            violations.append('%s:%d' % (os.path.relpath(path, ROOT), ln))

    print('[check_frozen_paths] scanned %d runtime modules' % scanned)
    if violations:
        print('[check_frozen_paths] FAIL - 裸 __file__ (打包态会指到 _MEI 临时目录, 装机必炸):')
        for v in violations:
            print('  ' + v)
        print('修法: try: import app_paths ... except Exception: 源码态兜底 (参照 scripts/credits_api.py)')
        return 1
    print('[check_frozen_paths] OK - 运行时模块无裸 __file__ 路径')
    return 0


if __name__ == '__main__':
    sys.exit(main())
