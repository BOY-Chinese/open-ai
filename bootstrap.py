# -*- coding: utf-8 -*-
"""
bootstrap.py — open-ai 统一控制 CLI
====================================
所有启动入口 (start.bat / 计划任务 / GUI / 快捷方式) 最终都汇到这里;
唯一有权创建长期子进程的是 Broker (app_runtime.py)。

用法:
    bootstrap.py start       启动全部 (若已在跑则直接返回, 幂等)
    bootstrap.py stop        优雅停止全部 (IPC 广播 → Job 兜底)
    bootstrap.py restart     重启全部
    bootstrap.py status      查看状态 (各角色 pid / 心跳 / shim)
    bootstrap.py doctor      诊断: shim / Job / 端口 / 管道
    bootstrap.py daemon      前台常驻运行 Broker (供计划任务/调试)

设计:
  start:
    若已有 Broker 在跑 → 幂等返回 (单实例锁保证);
    否则用 broker shim (open-ai-daemon.exe) 以 DETACHED 拉起后台 Broker,
    等待其 IPC 管道就绪 (最长 30s), 打印各角色状态。
  stop:
    连接 Broker 管道无法直接下发全局关闭 (协议是客户端单向),
    因此按序: 1) 尝试管道优雅关闭 (预留)  2) 关闭各 leaf Job (优雅超时)
    3) 打开 root Job terminate (整树清零)  4) 收尾 Broker 进程本体。
    全程不使用 taskkill /F /T。
"""
import os
import sys
import time
import json
import ctypes
import subprocess

import app_paths
BASE = app_paths.ROOT          # 安装根 (打包态 = exe 所在目录)
sys.path.insert(0, BASE)
sys.path.insert(0, app_paths.SCRIPTS_DIR)

import procname
import ipc
import jobmgmt

DATA_DIR = os.path.join(BASE, 'data')
RUNTIME_STATE = os.path.join(DATA_DIR, 'runtime_state.json')
PIDFILE = os.path.join(DATA_DIR, '.daemon.pid')
LOGS_DIR = os.path.join(BASE, 'logs')


def _console_utf8():
    """Windows 控制台切 UTF-8, 避免中文乱码。"""
    if os.name == 'nt':
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def _pid_alive(pid):
    try:
        k = ctypes.WinDLL('kernel32')
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return ok != 0 and code.value == 259
    except Exception:
        return False


def broker_pid():
    """从状态文件读当前 Broker pid (校验存活)。"""
    try:
        with open(RUNTIME_STATE, 'r', encoding='utf-8') as f:
            st = json.load(f)
        pid = st.get('broker_pid')
        if pid and _pid_alive(pid):
            return pid
    except Exception:
        pass
    return None


def spawn_broker(wait_ready=30):
    """后台拉起 Broker (DETACHED), 等待 IPC 管道就绪。"""
    shim = procname.shim_for('broker') or procname.populate().get('broker')
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | \
            getattr(subprocess, 'DETACHED_PROCESS', 0) | \
            getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    argv = [shim, os.path.join(BASE, 'daemon.py')]
    try:
        p = subprocess.Popen(argv, cwd=BASE, creationflags=flags,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             close_fds=True)
    except Exception as e:
        print('[start] Broker 启动失败: %s' % e)
        return None
    print('[start] Broker 已拉起 (pid=%s, %s)' % (p.pid, os.path.basename(shim)))
    # 等管道就绪
    deadline = time.time() + wait_ready
    while time.time() < deadline:
        if _pipe_ready():
            break
        time.sleep(0.5)
    return p


def _pipe_ready():
    try:
        c = ipc.PipeClient(timeout=1.0)
        c.close()
        return True
    except Exception as e:
        if os.environ.get('BOOTSTRAP_DEBUG'):
            print('[debug] pipe probe fail: %s' % e)
        return False


def cmd_start():
    _console_utf8()
    if _pipe_ready():
        print('[start] Broker 已在运行, 幂等返回')
        return 0
    # 双保险: 状态文件里有活着的 Broker 但管道不通 (刚崩/正在起) → 等待
    pid = broker_pid()
    if pid:
        print('[start] 检测到 Broker pid=%s 但管道未就绪, 等待 15s...' % pid)
        deadline = time.time() + 15
        while time.time() < deadline:
            if _pipe_ready():
                break
            time.sleep(0.5)
        if _pipe_ready():
            print('[start] Broker 恢复就绪')
            return 0
        print('[start] pid=%s 已僵死, 清理后重新拉起' % pid)
        _kill_pid_tree(pid)
    p = spawn_broker()
    if not p:
        return 1
    # 等新 Broker 把自己的状态写入 (避免显示上一代残留状态)
    deadline = time.time() + 8
    while time.time() < deadline:
        if broker_pid() == p.pid:
            break
        time.sleep(0.3)
    _print_status()
    return 0


def _terminate_pid(pid):
    """TerminateProcess 单个进程。"""
    try:
        k = ctypes.WinDLL('kernel32')
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        h = k.OpenProcess(0x0001, False, int(pid))   # PROCESS_TERMINATE
        if h:
            k.TerminateProcess(h, 1)
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _kill_pid_tree(pid):
    """整树收: Job 终止优先, 逐进程 TerminateProcess 兜底 (防 Job 内混入
    系统令牌进程导致 TerminateJobObject 失败)。"""
    victims = set()
    for nm in ('open-ai.tree', 'open-ai.gateway', 'open-ai.trae', 'open-ai.task'):
        try:
            j = jobmgmt.open_job(nm)
            if j:
                victims.update(j.pids())
                j.terminate()
                j.close()
        except Exception:
            pass
    try:
        victims.add(int(pid))
    except Exception:
        pass
    import time as _t
    _t.sleep(0.8)
    for v in victims:
        if _pid_alive(v):
            _terminate_pid(v)


def _wait_jobs_gone(names, timeout=12.0):
    """轮询等待这些 Job 里的进程自己退干净, 返回还剩的 pid。

    ★ 为什么不能"发完优雅关闭请求就睡 3 秒然后 TerminateJobObject":
      品牌 exe 是 PyInstaller **onefile** —— Popen 起来的其实是"父引导进程",
      真跑业务的是它解包到 %TEMP%\\_MEIxxxxxx 后再生出的子进程。父进程必须
      **活着等到子进程退出**, 才有机会把 _MEI 目录删掉。
      TerminateJobObject 会把父子**一起**干掉 → 父进程被剥夺执行机会 →
      _MEI 目录永久遗留。每次 stop 漏一个, 实测一台机器攒了 11~28 个。
      (对应 issue: %TEMP% 下 _MEI* 堆积)

    所以这里先等, 等不动了才强杀 —— 强杀仍保留为兜底, 不能因为怕留垃圾
    就把停止流程变成"可能永远停不下来"。
    """
    deadline = time.time() + timeout
    leftover = []
    while time.time() < deadline:
        leftover = []
        for nm in names:
            try:
                j = jobmgmt.open_job(nm)
                if j:
                    leftover.extend(j.pids())
                    j.close()
            except Exception:
                pass
        if not leftover:
            return []
        time.sleep(0.5)
    return leftover


def cmd_stop():
    _console_utf8()
    print('[stop] 开始优雅关闭...')
    # 0) 优雅协议: 连入管道请求 Broker 广播 shutdown (子进程自行清理退出)
    try:
        c = ipc.PipeClient(timeout=3.0)
        c.send({'type': 'request-shutdown', 'role': 'cli',
                'pid': os.getpid(), 'reason': 'bootstrap stop'})
        c.close()
        print('[stop] 已向 Broker 发送优雅关闭请求')
    except Exception:
        print('[stop] 管道不可用 (Broker 可能未运行), 走 Job 清理')
    # 1) leaf job 收尾: **先等子进程自然退出**, 让 onefile 父进程能清理 _MEI;
    #    超过耐心值仍赖着不走的才强杀 (兜底, 宁可留一个临时目录也不能停不掉)。
    leaf_map = {'gateway': 'open-ai.gateway', 'trae': 'open-ai.trae',
                'task': 'open-ai.task'}
    stuck = _wait_jobs_gone(list(leaf_map.values()), timeout=12.0)
    if stuck:
        print('[stop] 优雅退出超时, 仍存活: %s — 强制终止' % stuck)
    for role, name in leaf_map.items():
        try:
            j = jobmgmt.open_job(name)
            if j:
                if j.pids():
                    j.terminate()
                j.close()
        except Exception as e:
            print('[stop] %s leaf job: %s' % (role, e))
    time.sleep(1.0)
    # 2) root job 整树清零 (覆盖: Broker 本体 + 一切残留)
    try:
        j = jobmgmt.open_job('open-ai.tree')
        if j:
            pids = j.pids()
            if pids:
                print('[stop] root job: %s' % pids)
            j.terminate()
            j.close()
    except Exception as e:
        print('[stop] root job: %s' % e)
    # 3) Broker 本体 (状态文件里的 pid)
    pid = broker_pid()
    if pid:
        _kill_pid_tree(pid)
        print('[stop] Broker pid=%s 已终止' % pid)
    # 4) 清理状态
    try:
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)
        if os.path.exists(RUNTIME_STATE):
            os.remove(RUNTIME_STATE)
    except Exception:
        pass
    print('[stop] 完成')
    return 0


def cmd_status():
    _console_utf8()
    _print_status()
    return 0


def _print_status():
    pid = broker_pid()
    print('=' * 58)
    print('  open-ai 运行状态')
    print('=' * 58)
    if pid:
        print('  Broker: pid=%s' % pid)
    else:
        print('  Broker: 未运行')
    try:
        with open(RUNTIME_STATE, 'r', encoding='utf-8') as f:
            st = json.load(f)
        children = st.get('children', {})
        if children:
            print('  %-10s %-8s %-10s %s' % ('角色', 'PID', '存活', '最后心跳'))
            for role, c in sorted(children.items()):
                alive = _pid_alive(c.get('pid', 0)) if c.get('pid') else False
                beat = ('%.0fs 前' % (time.time() - c['last_beat'])
                        if c.get('last_beat') else '从未')
                print('  %-10s %-8s %-10s %s' % (role, c.get('pid', '-'),
                                                 '是' if alive else '否', beat))
    except Exception:
        pass
    print('  IPC 管道: %s' % ('就绪' if _pipe_ready() else '不可用'))
    print('-' * 58)
    print('  Shim 状态:')
    for role, exe, ok in procname.shim_status():
        print('    %-8s %-24s %s' % (role, exe, '就绪' if ok else '未生成'))
    print('=' * 58)


def cmd_doctor():
    _console_utf8()
    print('== open-ai 诊断 ==')
    # 1) shim
    print('[1] shim 检查')
    for role, exe, ok in procname.shim_status():
        print('    %-8s %-24s %s' % (role, exe, 'OK' if ok else 'MISSING'))
    # 2) job
    print('[2] Job Object')
    try:
        import jobmgmt as jm
        for nm in ('open-ai.tree', 'open-ai.gateway', 'open-ai.trae', 'open-ai.task'):
            j = jm.open_job(nm)
            if j:
                print('    %-22s 存在, pids=%s' % (nm, j.pids()))
                j.close()
            else:
                print('    %-22s 不存在 (未运行属正常)' % nm)
    except Exception as e:
        print('    检查失败: %s' % e)
    # 3) 端口
    print('[3] 端口监听')
    import socket
    for port, name in ((8000, 'gateway'), (18787, 'trae')):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(('127.0.0.1', port))
            print('    :%-6d %-10s 在线' % (port, name))
        except Exception:
            print('    :%-6d %-10s 离线' % (port, name))
        finally:
            s.close()
    # 4) 管道
    print('[4] IPC 管道')
    print('    %s: %s' % (ipc.PIPE_NAME,
                          '就绪' if _pipe_ready() else '不可用'))
    # 5) 进程树
    print('[5] open-ai 进程')
    try:
        import subprocess as sp
        out = sp.check_output(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -like 'open-ai*' -or "
             "($_.CommandLine -like '*open-ai*' -and $_.Name -match 'python|node') } | "
             "Select-Object ProcessId,ParentProcessId,Name | "
             "ConvertTo-Json -Compress"],
            timeout=30, creationflags=getattr(sp, 'CREATE_NO_WINDOW', 0))
        data = json.loads(out.decode('utf-8', 'ignore'))
        if isinstance(data, dict):
            data = [data]
        for p in data or []:
            print('    pid=%-7s ppid=%-7s %s' % (p.get('ProcessId'),
                                                 p.get('ParentProcessId'),
                                                 p.get('Name')))
    except Exception as e:
        print('    (无法枚举: %s)' % e)
    print('== 完成 ==')
    return 0


def cmd_daemon():
    """前台运行 Broker (计划任务/调试用)。"""
    _console_utf8()
    import app_runtime
    return app_runtime.main()


def _normalise_argv():
    """丢掉 argv[1] 里那个「路由用的脚本路径」。

    源码形态 `python bootstrap.py start`: argv = ['bootstrap.py', 'start'],
    argv[1] 就是子命令, 一切正常。

    打包形态 `open-ai-daemon.exe <安装根>\bootstrap.py start`: exe 本身就是
    bootstrap.py, 但所有调用方 (桌面端 lib.rs 的 stop_all_backends、计划任务、
    start.bat) 沿用「解释器 + 脚本路径」这套 argv, 于是那个路径落到 argv[1],
    子命令被挤到 argv[2] —— 结果是把路径当命令名, 打印用法后返回 2,
    表现为「托盘退出没反应」「开机自启没起来」, 而且完全不报错。

    判据很稳: 真正的子命令 (start|stop|restart|status|doctor|daemon)
    永远不会以 .py 结尾, 所以遇到 .py 就当成路由标记丢掉。
    """
    while len(sys.argv) > 1 and sys.argv[1].lower().endswith('.py'):
        sys.argv.pop(1)


def main():
    _normalise_argv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    fn = {'start': cmd_start, 'stop': cmd_stop, 'restart': None,
          'status': cmd_status, 'doctor': cmd_doctor, 'daemon': cmd_daemon}.get(cmd)
    if cmd == 'restart':
        cmd_stop()
        time.sleep(2)
        return cmd_start()
    if fn is None:
        print('用法: bootstrap.py {start|stop|restart|status|doctor|daemon}')
        return 2
    return fn()


if __name__ == '__main__':
    sys.exit(main())
