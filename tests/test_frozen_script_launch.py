# -*- coding: utf-8 -*-
"""
测试: 打包态「拉起内置脚本」的路径必须走 task shim
==================================================
背景（2026-09-16 用户机事故，报「重新连接账号」秒失败）：

    [账号] 重新连接账号
    [参数] 通道=all
    [错误] ApiError: signin_all.py 不存在
    [完成] 20:04:58（耗时 41ms）

安装包（PyInstaller）里**没有 `scripts\\*.py` 源码，也没有独立解释器**，
但 `POST /accounts/reconnect` 是拿源码态写法实现的：

    script = os.path.join(BASE, "scripts", "signin_all.py")
    if not os.path.exists(script): raise HTTPException(501, "signin_all.py 不存在")
    exe = os.path.join(BASE, ".venv", "Scripts", "python.exe")

于是源码态看着一切正常，**装机后必 501** —— 同族的「签到/登录/采集」
早就改成了「把脚本路径当路由标记交给 task shim」（见 admin_api._signin_argv），
唯独重连这一处漏改。本测试就是为这类「只有装机后才炸」的分支设的门禁。

两部分：
  A. 行为断言 —— 冻结 `sys.frozen` 后，重连端点拉起的进程必须是
     `open-ai-task.exe <脚本路径标记>`，且绝不能出现 `.venv\\Scripts\\python.exe`
     或 `sys.executable`（打包态没有它们）。
  B. 静态审计 —— 运行时可执行模块里，凡是「既出现 scripts/ 路径、又 spawn 进程」
     的函数，都必须显式区分打包态（调用 `_signin_argv` 或判断 `frozen`）。
     新增端点忘了分支 → 这里立刻红。

运行: python -m unittest tests.test_frozen_script_launch -v
"""
import ast
import asyncio
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import admin_api as A  # noqa: E402


class _FakeProc:
    """足够像 subprocess.Popen 返回值：端点只用到 pid。"""

    pid = 4242


class ReconnectFrozenArgvTest(unittest.TestCase):
    """A. 打包态下重连端点必须拉起 task shim，而不是源码态解释器。"""

    def _run(self, frozen: bool):
        captured = {}

        def fake_popen(argv, *a, **kw):
            captured['argv'] = list(argv)
            captured['kwargs'] = kw
            return _FakeProc()

        with mock.patch.object(sys, 'frozen', frozen, create=True), \
                mock.patch('subprocess.Popen', side_effect=fake_popen):
            res = asyncio.run(A.reconnect_accounts({'channel': 'all'}))
        return res, captured

    def test_frozen_uses_task_shim(self):
        res, cap = self._run(frozen=True)
        self.assertTrue(res.get('ok') and res.get('started'))
        argv = cap['argv']
        self.assertTrue(argv[0].lower().endswith('open-ai-task.exe'),
                        f'打包态必须以 task shim 拉起，实际 argv[0]={argv[0]!r}')
        # argv[1] 是「路由标记」：由 task_main 按文件名路由到 signin_all 模块，
        # 该路径**不要求真实存在**（安装包里没有 .py 源码）。
        self.assertEqual(os.path.basename(argv[1]), 'signin_all.py')
        joined = ' '.join(argv).lower()
        self.assertNotIn('.venv', joined)
        self.assertNotIn('python.exe', joined)
        self.assertNotIn(sys.executable.lower(), joined)

    def test_source_mode_also_branded(self):
        """源码态也走 task shim（品牌化）—— 2026-09-18 起的行为。

        历史：此处原先断言源码态必须以 `.venv\\Scripts\\python.exe` 拉起。
        master 要求「把国际版签到脚本纳入 open-ai 进程树」后改为主态优先
        shim —— 短命脚本进程因此显示 `open-ai-task.exe`，与 gateway / trae /
        daemon 一致（否则用户在任务管理器里只看到一个裸 python.exe，
        「open-ai 到底跑了什么」是断的）。

        ⚠️ shim 不存在时必须仍能回落 —— 否则全新源码检出（还没跑过
           procname.populate）会直接跑不起来。见下一个用例。

        ★ 发布版适配：dev / portable 发布仓**不含 runtime\\**（shim 由
          procname.populate 现场生成，不入库），故此处无 shim 时跳过「必须
          品牌化」的断言 —— 那种形态的正确行为恰恰是下一个用例覆盖的回落。
          本机端（有 shim）该断言照常生效。
        """
        if not A._task_shim():
            self.skipTest('本仓无 runtime\\Scripts\\open-ai-task.exe shim（发布仓形态）')
        _, cap = self._run(frozen=False)
        argv = cap['argv']
        self.assertEqual(os.path.basename(argv[1]), 'signin_all.py')
        self.assertTrue(argv[0].lower().endswith('open-ai-task.exe'),
                        f'源码态未品牌化，argv[0]={argv[0]!r}')

    def test_source_mode_falls_back_when_shim_missing(self):
        """shim 缺失 → 回落 .venv python，再回落当前解释器（不得抛错）。"""
        with mock.patch.object(A, '_task_shim', return_value=''), \
                mock.patch.object(sys, 'frozen', False, create=True):
            argv = A._signin_argv('signin_all.py', ['--wb-only'])
        self.assertTrue(argv[0].lower().endswith('python.exe'), argv[0])
        self.assertNotIn('open-ai-task', argv[0].lower())

    def test_signin_argv_two_modes(self):
        """_signin_argv 是所有「拉起内置脚本」的唯一出口，两种形态各断言一次。

        ★ 两种形态现在**都**以 task shim 品牌化开头，区别只在脚本路径参数：
          打包态是「路由标记」（不要求存在），源码态是真实脚本路径。

        ★ 发布版适配：源码态分支需 shim 存在才有意义（见上一个用例说明）；
          无 shim 的发布仓跳过该分支，打包态分支照常断言。
        """
        with mock.patch.object(sys, 'frozen', True, create=True):
            argv = A._signin_argv('usage_collector.py', ['--collect'])
        self.assertTrue(argv[0].lower().endswith('open-ai-task.exe'))
        self.assertEqual(argv[-1], '--collect')

        if not A._task_shim():
            return  # 发布仓无 shim：源码态品牌化不适用（行为由回落用例覆盖）
        with mock.patch.object(sys, 'frozen', False, create=True):
            argv = A._signin_argv('usage_collector.py', ['--collect'])
        self.assertTrue(argv[0].lower().endswith('open-ai-task.exe'),
                        f'源码态未品牌化：{argv[0]!r}')
        self.assertEqual(os.path.basename(argv[1]), 'usage_collector.py')


class SpawnedScriptAuditTest(unittest.TestCase):
    """B. 静态审计：spawn 内置脚本的每个函数都必须能处理打包态。"""

    # spawn 进程的关键名
    SPAWN_NAMES = {'Popen', 'run', 'call', 'check_output'}

    @staticmethod
    def _is_spawn(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SpawnedScriptAuditTest.SPAWN_NAMES
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'subprocess')

    def _audit(self, path: str) -> list[str]:
        with open(path, encoding='utf-8') as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
        bad = []
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body_src = ast.get_source_segment(src, fn) or ''
            spawns = any(self._is_spawn(n) for n in ast.walk(fn))
            if not spawns:
                continue
            # 「拉起内置脚本」的判据：源码里出现 scripts 路径或脚本文件名
            touches_script = ('"scripts"' in body_src or "'scripts'" in body_src
                              or '.py"' in body_src)
            if not touches_script:
                continue
            handled = ('_signin_argv' in body_src or 'frozen' in body_src
                       or 'shim' in body_src.lower())
            if not handled:
                bad.append(f'{os.path.basename(path)}:{fn.lineno} {fn.name}()')
        return bad

    def test_admin_api_spawn_sites_handle_frozen(self):
        bad = self._audit(os.path.join(PROJECT_ROOT, 'admin_api.py'))
        self.assertEqual(
            bad, [],
            '以下函数 spawn 了 scripts/*.py，却没有区分打包态（安装包里没有 .py 源码、'
            '也没有 .venv）—— 装机后必定失败。请改用 _signin_argv / launch_login 的写法：\n  '
            + '\n  '.join(bad))


if __name__ == '__main__':
    unittest.main()
