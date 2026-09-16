#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_undefined_names.py — 未定义名静态门禁 (构建前必跑)
==========================================================
起因 (2026-09-16 portable 用户机事故):
    scripts/signin_all.py 的 Loomy 白天补签分支写成

        loomy_state = lc.load_daily_state() or {}
        ... not (loom_state.get(...) or {}).get('claimed')     # ← 少了个 y

    拼错的 `loom_state` 从未定义, 一执行就 NameError, 把签到进程整个打挂:

        File "signin_all.py", line 529, in main
        NameError: name 'loom_state' is not defined

为什么必须做成构建门禁 (而不是只写个单测):
  1. **编译期不报**: Python 只在执行到那一行时才抛 NameError。该行还被
     `if pending:` / 账号存在性等条件包着 —— 开发机没登录 Loomy 账号就永远
     跑不到, 本地全量测试照样全绿, 属于典型的「装了用户机才炸」。
  2. **三仓同源**: 本机版 / dev / portable 是同一份代码的副本, 改一处忘回流,
     另外两仓就带着同一个坑各自发版。门禁挂在构建脚本上, 谁打包都会先过一遍。
  3. **同类错误反复出现**: 之前的 `_ = rid`(UnboundLocalError)、
     `__file__` 家族(#4/#5) 都是「静态可查但没人查」的错。

检查内容 (保守, 宁可漏报不误报):
  对每个函数, 收集「自身绑定 + 闭包外层 + 模块级定义 + 内建 + 推导式/lambda
  变量」, 剩下的 Load 名才报。嵌套函数只产出节点本身、不展开其内部 ——
  否则嵌套函数的形参会把外层判成未定义 (实测不清掉会多出十几条假阳性,
  那样的门禁上线当天就会被当噪声关掉)。

用法:
    python check_undefined_names.py [项目根]
    (缺省 = 本文件上一级)

退出码: 0 = 干净, 1 = 发现未定义名 (构建应中止)
"""
import ast
import builtins
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(HERE)

# 扫描范围: 运行时模块。跳过构建期工具(依赖本机环境)、第三方目录、产物目录。
SKIP_DIRS = {
    '__pycache__', 'node_modules', 'node_modules_store', '.venv', '.git',
    'runtime', 'toolchain', 'build', 'dist', 'target', 'installer',
    'tests', 'desktop', 'desktop-ui', 'trae', 'pic', 'data', 'logs', 'exes',
}

BUILTINS = set(dir(builtins)) | {
    '__file__', '__name__', '__doc__', '__package__', '__spec__',
    '__loader__', '__builtins__',
}


def names_in_target(t):
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Name):
            out.add(n.id)
    return out


def module_level_names(tree):
    names = set()
    for node in tree.body:
        names |= targets_of(node)
    return names


def targets_of(node):
    out = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.add((a.asname or a.name).split('.')[0])
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            out |= names_in_target(t)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        out |= names_in_target(node.target)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        out |= names_in_target(node.target)
        for sub in node.body + node.orelse:
            out |= targets_of(sub)
    elif isinstance(node, (ast.If, ast.While)):
        for sub in node.body + node.orelse:
            out |= targets_of(sub)
    elif isinstance(node, ast.Try):
        for sub in node.body + node.orelse + node.finalbody:
            out |= targets_of(sub)
        for h in node.handlers:
            if h.name:
                out.add(h.name)
            for sub in h.body:
                out |= targets_of(sub)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                out |= names_in_target(item.optional_vars)
        for sub in node.body:
            out |= targets_of(sub)
    return out


def own_nodes(fn):
    """只产出本层的语句节点; 嵌套 def/class 只产出节点本身, 不展开其内部。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def comprehension_names(fn):
    """推导式循环目标 + lambda 形参: 独立作用域, 但保守算作本函数可见。"""
    out = set()
    for n in own_nodes(fn):
        if isinstance(n, ast.comprehension):
            out |= names_in_target(n.target)
        elif isinstance(n, ast.Lambda):
            args = list(n.args.args) + list(n.args.posonlyargs) + \
                list(n.args.kwonlyargs)
            for a in args:
                out.add(a.arg)
            if n.args.vararg:
                out.add(n.args.vararg.arg)
            if n.args.kwarg:
                out.add(n.args.kwarg.arg)
    return out


def collect_fn_scope(fn):
    bound = set()
    args = list(fn.args.args) + list(fn.args.posonlyargs) + list(fn.args.kwonlyargs)
    for a in args:
        bound.add(a.arg)
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    # 嵌套 def/class 的**名字**在本层可见(可以调用它), 但不含其形参
    for n in own_nodes(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
    for n in own_nodes(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound |= set(n.names)
    return bound | comprehension_names(fn)


def iter_functions_with_closure(tree):
    """产出 (函数节点, 闭包外层可见名)。嵌套函数能合法引用外层局部变量,
    不带外层就会把 `cid`/`created` 这类闭包引用全判成未定义。"""
    def walk(node, outer):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, set(outer)
                yield from walk(child, outer | collect_fn_scope(child))
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, outer | {child.name})
            else:
                yield from walk(child, outer)
    yield from walk(tree, set())


def iter_py_files(root):
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        top = rel.split(os.sep)[0] if rel != '.' else ''
        if top in SKIP_DIRS:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(cur, f)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    problems = []
    scanned = 0
    for path in iter_py_files(root):
        rel = os.path.relpath(path, root).replace('\\', '/')
        try:
            with open(path, encoding='utf-8') as fh:
                tree = ast.parse(fh.read(), path)
        except SyntaxError as e:
            problems.append('%s: 语法错误 %s' % (rel, e))
            continue
        scanned += 1
        mod = module_level_names(tree) | BUILTINS
        for fn, outer in iter_functions_with_closure(tree):
            scope = collect_fn_scope(fn) | outer | mod
            used = set()
            for n in own_nodes(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    used.add(n.id)
            for name in sorted(used - scope):
                problems.append('%s:%d %s() -> 未定义的名字 %r'
                                % (rel, fn.lineno, fn.name, name))

    print('[check_undefined_names] scanned %d files' % scanned)
    if problems:
        print('[check_undefined_names] FAIL - 发现 %d 处未定义名:'
              % len(problems))
        for p in problems:
            print('   ' + p)
        print('  这类问题只在执行到那一行时才抛 (且常被条件包着), '
              '本地测试查不出来 —— 必须在这里拦住。')
        return 1
    print('[check_undefined_names] OK - 无未定义名')
    return 0


if __name__ == '__main__':
    sys.exit(main())
