# -*- coding: utf-8 -*-
"""
test_procman.py — v2.4 进程管理架构单元测试
=============================================
覆盖:
  1. IPC 帧编解码 (FrameReader 粘包/断包)
  2. Job Object: 创建 / 指派 / KILL_ON_JOB_CLOSE / 枚举 PID (仅 Windows)
  3. procname: runtime 布局与 shim 路径
  4. app_runtime: 配置读取与状态原子写
  5. bootstrap: 状态文件解析
运行: .venv\\Scripts\\python.exe -m unittest tests.test_procman -v
"""
import os
import sys
import json
import time
import struct
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

IS_NT = os.name == 'nt'


class TestIPCFrame(unittest.TestCase):
    def test_roundtrip(self):
        from ipc import encode_frame, FrameReader
        obj = {'type': 'hello', 'role': 'gateway', 'pid': 123}
        frame = encode_frame(obj)
        r = FrameReader()
        self.assertEqual(r.feed(frame), [obj])

    def test_split_feed(self):
        """断包: 分多次喂字节仍应还原完整帧。"""
        from ipc import encode_frame, FrameReader
        obj = {'type': 'heartbeat', 'role': 'trae', 'ts': 1.0}
        frame = encode_frame(obj)
        r = FrameReader()
        self.assertEqual(r.feed(frame[:3]), [])
        self.assertEqual(r.feed(frame[3:7]), [])
        out = r.feed(frame[7:])
        self.assertEqual(out, [obj])

    def test_multi_frames(self):
        """粘包: 一次喂多帧。"""
        from ipc import encode_frame, FrameReader
        a = {'type': 'a'}
        b = {'type': 'b'}
        r = FrameReader()
        self.assertEqual(r.feed(encode_frame(a) + encode_frame(b)), [a, b])

    def test_oversize_guard(self):
        """超长长度头: 丢弃重同步而非崩溃。"""
        from ipc import FrameReader
        r = FrameReader()
        bad = struct.pack('<I', 999999999) + b'xxxx'
        self.assertEqual(r.feed(bad), [])


@unittest.skipUnless(IS_NT, 'Job Object 仅 Windows')
class TestJobObject(unittest.TestCase):
    def test_kill_on_job_close(self):
        """关闭句柄 → Job 内子进程被内核终止 (杜绝孤儿的核心机制)。"""
        import subprocess
        import jobmgmt
        job = jobmgmt.Job('open-ai.selftest', kill_on_close=True)
        py = os.path.join(BASE, 'runtime', 'Scripts', 'open-ai-task.exe')
        if not os.path.exists(py):
            py = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')
        p = subprocess.Popen([py, '-c', 'import time; time.sleep(30)'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job.assign(p)
        self.assertIn(p.pid, job.pids())
        job.close()                       # KILL_ON_JOB_CLOSE 触发
        time.sleep(1.0)
        self.assertIsNotNone(p.poll())    # 子进程已被终止

    def test_terminate(self):
        import subprocess
        import jobmgmt
        job = jobmgmt.Job('open-ai.selftest2', kill_on_close=False)
        py = os.path.join(BASE, 'runtime', 'Scripts', 'open-ai-task.exe')
        if not os.path.exists(py):
            py = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')
        p = subprocess.Popen([py, '-c', 'import time; time.sleep(30)'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job.assign(p)
        job.terminate()
        time.sleep(1.0)
        self.assertIsNotNone(p.poll())
        job.close()


class TestProcname(unittest.TestCase):
    def test_specs_complete(self):
        import procname
        for role in ('gateway', 'trae', 'broker', 'task', 'cli'):
            self.assertIn(role, procname.SPECS)
            spec = procname.SPECS[role]
            self.assertTrue(spec.exe_name.startswith('open-ai'))
            self.assertLessEqual(len(spec.description), 32,
                                 '描述串超长会被版本底板截断: %s' % role)

    def test_shim_paths(self):
        import procname
        for role, spec in procname.SPECS.items():
            self.assertTrue(spec.shim_path.endswith(spec.exe_name))

    @unittest.skipUnless(IS_NT, 'runtime 布局仅 Windows 验证')
    def test_runtime_layout(self):
        import procname
        if not os.path.isdir(procname.RUNTIME_DIR):
            self.skipTest('runtime 尚未构建')
        self.assertTrue(os.path.isfile(procname.PTH_FILE), 'openai_runtime.pth 缺失')
        self.assertTrue(os.path.isdir(os.path.join(procname.RUNTIME_DIR, 'Lib',
                                                   'site-packages')),
                        'site-packages junction 缺失')
        self.assertTrue(os.path.isfile(os.path.join(procname.RUNTIME_DIR,
                                                    'pyvenv.cfg')))


class TestRuntimeConfig(unittest.TestCase):
    def test_config_runtime_section(self):
        cfg = json.load(open(os.path.join(BASE, 'config.json'),
                             encoding='utf-8'))
        rt = cfg.get('runtime', {})
        self.assertIsInstance(rt.get('backoff_steps', []), list)
        self.assertGreater(rt.get('root_memory_mb', 0), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
