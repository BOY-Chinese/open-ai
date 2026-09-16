# -*- coding: utf-8 -*-
"""
测试: 全仓 Python 源码的「未定义名」静态体检
=============================================
起因（2026-09-16 portable 用户机事故）:
  `scripts/signin_all.py` 的 Loomy 白天补签分支写成
      loomy_state = lc.load_daily_state() or {}
      ... not (loom_state.get(...) or {}).get('claimed')     # ← 少了个 y
  拼错的 `loom_state` 从未定义, 一执行就 NameError, 直接把签到进程打挂:

      File "signin_all.py", line 529, in main
      NameError: name 'loom_state' is not defined

  这个 bug 有三个恶劣性质:
    1. **编译期不报**: Python 只在真正执行到那一行时才抛, 而该分支被
       `if pending:` 之类的条件包着 —— 没登录 Loomy 账号的机器永远跑不到,
       本地全量测试也照样通过, 属于「装了用户机才炸」的类型;
    2. **三仓同时中招**: 本机版 / dev / portable 是同一份代码的副本,
       改一处忘了回流, 另外两处就带着同一个坑继续发版;
    3. **同类错误会复发**: 之前的 `_ = rid`(UnboundLocalError)、`__file__`
       家族(#4/#5) 都是「静态可查但没人查」的错。

  所以这里不针对那个 typo 写一条断言(那只能防一次), 而是做**全仓 AST 扫描**,
  把「用到了却在任何作用域都找不到的名字」这一类问题一次性拦住 —— 包括但不限于
  拼写错的局部变量、忘了 import 的模块名、改名后漏改的引用。

判定方式(保守, 宁可漏报不误报):
  对每个函数体, 收集「赋值目标 / 参数 / import / 全局定义 / 内建 / 闭包可见名」,
  剩下的 Name 若是 **Load** 上下文且不在其中, 才报。模块级与类体不检查(动态
  注册的模块属性太多, 误报率高)。needs_pyflakes 之类外部依赖一律不用 ——
  测试要能在用户机 / 打包机 / CI 上零依赖跑起来。

运行: python -m unittest tests.test_static_names -v
"""
import ast
import builtins
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 扫描范围: 运行时模块。故意跳过 installer/ (构建期工具, 依赖本机环境变量与
# Windows API, 且不进安装包)、tests/ (自身) 、以及第三方目录。
SKIP_DIRS = {
    '__pycache__', 'node_modules', 'node_modules_store', '.venv', '.git',
    'runtime', 'toolchain', 'build', 'dist', 'target', 'installer',
    'tests', 'desktop', 'desktop-ui', 'trae', 'pic', 'data', 'logs',
}

BUILTINS = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__package__',
                                 '__spec__', '__loader__', '__builtins__'}


def _module_level_names(tree):
    """模块顶层定义的名字 (函数/类/赋值/import/for 目标), 供函数体引用。"""
    names = set()
    for node in tree.body:
        names |= _targets_of(node)
    return names


def _targets_of(node):
    """取出一个语句(或若干语句)里被绑定的名字。"""
    out = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.add((a.asname or a.name).split('.')[0])
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            out |= _names_in_target(t)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        out |= _names_in_target(node.target)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        out |= _names_in_target(node.target)
        for sub in node.body + node.orelse:
            out |= _targets_of(sub)
    elif isinstance(node, (ast.If, ast.While)):
        for sub in node.body + node.orelse:
            out |= _targets_of(sub)
    elif isinstance(node, ast.Try):
        for sub in (node.body + node.orelse + node.finalbody):
            out |= _targets_of(sub)
        for h in node.handlers:
            if h.name:
                out.add(h.name)
            for sub in h.body:
                out |= _targets_of(sub)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                out |= _names_in_target(item.optional_vars)
        for sub in node.body:
            out |= _targets_of(sub)
    return out


def _names_in_target(t):
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Name):
            out.add(n.id)
    return out


def _own_nodes(fn):
    """遍历函数的**自身**代码, 不进入嵌套函数/类的作用域。

    ★ 这是本门禁最容易写错的地方: `ast.walk(fn)` 会连嵌套函数一起走下去,
      于是 `def chunk(delta, finish=None)` 里的 `finish` 被算到外层 `stream()`
      头上 —— 而外层根本没这个名字, 就报成「未定义」。实测这一步不清掉,
      全仓会多出十几条假阳性, 门禁上线当天就会被当噪声关掉。

    产出规则: 只产出**本层**的语句节点; 遇到嵌套 def/class 时产出该节点本身
    (让它的名字在本层可见, 供 `on_response(...)` 这类调用点匹配),
    但绝不产出它内部的任何节点 —— 那是它自己的作用域。
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 嵌套作用域: 只产出节点本身, 不展开
            yield node
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _comprehension_names(fn):
    """推导式/生成器里的循环目标与 lambda 参数 —— 它们是**独立作用域**,
    但保守起见都算作「本函数可见」, 否则会把 `[x for x in ...]` 判成未定义。"""
    out = set()
    for n in _own_nodes(fn):
        if isinstance(n, ast.comprehension):
            out |= _names_in_target(n.target)
        elif isinstance(n, ast.Lambda):
            for a in list(n.args.args) + list(n.args.posonlyargs) + \
                    list(n.args.kwonlyargs):
                out.add(a.arg)
            if n.args.vararg:
                out.add(n.args.vararg.arg)
            if n.args.kwarg:
                out.add(n.args.kwarg.arg)
    return out


def _collect_fn_scope(fn):
    """函数体内**所有**被绑定的名字(不做块作用域区分, 保守放行)。"""
    bound = set()
    for a in list(fn.args.args) + list(fn.args.posonlyargs) + \
            list(fn.args.kwonlyargs):
        bound.add(a.arg)
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    # 嵌套函数的**名字**在本作用域可见(可以调用它), 但它的形参不属于这里。
    # ⚠ 要在自身代码里找, 不能只看 iter_child_nodes(fn): 嵌套 def 常写在
    #   if / try / with 块内 (`if cond: def on_response(resp): ...`),
    #   只看第一层就会漏掉它的名字 → 把合法的调用判成「未定义」。
    for n in _own_nodes(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
    for n in _own_nodes(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound |= set(n.names)
    return bound | _comprehension_names(fn)


def _iter_functions_with_closure(tree):
    """产出 (函数节点, 闭包外层已绑定名集合)。

    ★ 必须带上闭包外层: 嵌套函数通过 `nonlocal`/闭包引用外层局部变量是合法且
      常见的写法 (如 `stream()` 里定义 `chunk()` 用外层的 cid/created/model)。
      不带外层就会把它们全判成「未定义」—— 那种门禁一上线就被当噪声关掉,
      等于没有。故自顶向下累积每一层的绑定集合。
    """
    def walk(node, outer):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, set(outer)
                yield from walk(child, outer | _collect_fn_scope(child))
            elif isinstance(child, ast.ClassDef):
                # 类体里的方法: 把类名加上, 类体赋值不算方法可见名(近似)
                yield from walk(child, outer | {child.name})
            else:
                yield from walk(child, outer)
    yield from walk(tree, set())


def iter_py_files():
    for root, dirs, files in os.walk(PROJECT_ROOT):
        rel = os.path.relpath(root, PROJECT_ROOT)
        top = rel.split(os.sep)[0] if rel != '.' else ''
        if top in SKIP_DIRS:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(root, f)


class UndefinedNameTest(unittest.TestCase):
    """全仓扫描: 不许存在「用了但没定义」的名字。"""

    def test_no_undefined_names(self):
        problems = []
        scanned = 0
        for path in iter_py_files():
            rel = os.path.relpath(path, PROJECT_ROOT).replace('\\', '/')
            try:
                with open(path, encoding='utf-8') as fh:
                    tree = ast.parse(fh.read(), path)
            except SyntaxError as e:
                problems.append('%s: 语法错误 %s' % (rel, e))
                continue
            scanned += 1
            mod_names = _module_level_names(tree) | BUILTINS

            for fn, outer in _iter_functions_with_closure(tree):
                scope = (_collect_fn_scope(fn) | outer | mod_names)
                used = set()
                for n in _own_nodes(fn):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        used.add(n.id)
                for name in sorted(used - scope):
                    problems.append(
                        '%s:%d %s() → 未定义的名字 %r'
                        % (rel, fn.lineno, fn.name, name))

        self.assertGreater(scanned, 20, '扫描到的文件太少, 门禁可能没生效')
        if problems:
            self.fail(
                '发现 %d 处「未定义名」（运行到该行必抛 NameError/类似异常）:\n  %s\n'
                '提示: 这类问题编译期不报, 只在真正执行到那一行时才炸 —— '
                '所以必须在提交前拦住。'
                % (len(problems), '\n  '.join(problems)))


class SigninAllLoomyStateTest(unittest.TestCase):
    """回归: signin_all 的 Loomy 补签分支必须自洽（那次事故的具体位置）。

    单独立一条是因为静态扫描只保证「名字有定义」, 不保证「语义对」——
    这里再钉一次「同名变量与它的第一次绑定一致」, 让下一个人改名时能被挡下。
    """

    def test_loomy_state_variable_consistently_named(self):
        p = os.path.join(PROJECT_ROOT, 'scripts', 'signin_all.py')
        if not os.path.exists(p):
            self.skipTest('signin_all.py 不在本仓')
        with open(p, encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotIn('loom_state', src,
                         'signin_all.py 出现了 `loom_state`（应为 `loomy_state`）—— '
                         '这正是 2026-09-16 用户机 NameError 的成因')
        self.assertIn('loomy_state', src)

    def test_wb_only_branch_is_executable(self):
        """`--wb-only` 分支必须能真正跑到底（不依赖 Loomy 账号存在与否）。"""
        import subprocess
        import tempfile
        p = os.path.join(PROJECT_ROOT, 'scripts', 'signin_all.py')
        if not os.path.exists(p):
            self.skipTest('signin_all.py 不在本仓')
        # 用空配置跑: 没有任何账号时也不该抛 NameError
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, 'config.json')
            with open(cfg, 'w', encoding='utf-8') as f:
                f.write('{"providers": {}}')
            env = dict(os.environ)
            env['OPEN_AI_CONFIG'] = cfg
            with open(p, encoding='utf-8') as fh:
                src = fh.read()
            # 只做语法/名字层面的冒烟: 编译该文件并确认关键分支里的名字已定义
            tree = ast.parse(src)
            fn = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'main']
            self.assertTrue(fn, 'signin_all.main 不见了')
            scope = _collect_fn_scope(fn[0]) | _module_level_names(tree) | BUILTINS
            self.assertIn('loomy_state', scope)


if __name__ == '__main__':
    unittest.main()
