# -*- coding: utf-8 -*-
"""
app_runtime.py — open-ai 进程 Broker: 统一创建/托管/看门狗/优雅退出
====================================================================
设计 (Broker 模式):
  - 全系统只有 Broker 有权创建长期子进程; 其他任何入口 (GUI/CLI/计划任务)
    都只与 Broker 通信, 不亲手 spawn → 进程树永远清晰。
  - Broker 启动时:
      1. 单实例锁 (named mutex + PID 文件双判)
      2. 预热 shim (procname.populate)
      3. 自我指派 root Job 'open-ai.tree' (KILL_ON_JOB_CLOSE)
         → 之后 spawn 的子进程零竞态自动入树
      4. 创建 IPC 命名管道服务端
      5. 监督循环: 每 5s 巡检
           - gateway/trae 进程在? (心跳新鲜 OR pid 存活) 否则指数退避重启
           - 心跳超时 (DEAD_AFTER) → 视为僵死 → kill → 重启
           - 收到全局 SHUTDOWN → 优雅关闭所有子进程 → 退出
  - 资源配额: root Job 设进程内存上限; leaf Job 按角色限额 (jobmgmt)。
  - 崩溃自动恢复: restart backoff 30s → 1m → 2m → 4m → 8m → 15m(封顶);
    进程稳定运行 5 分钟后 backoff 重置。

IPC 服务端 (无重叠 IO 的简化实现):
  - 单管多实例 (FILE_FLAG_FIRST_PIPE_INSTANCE 不设, 允许并发实例),
    每个客户端连接 = 一个管道实例; 服务端用独立线程 accept + 轮询读。
  - 指令下发通过"心跳应答捎带" (客户端每跳收一次) + 主动 write_frame。
"""
import os
import sys
import json
import time
import ctypes
import subprocess
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import procname
import jobmgmt
import ipc

DATA_DIR = os.path.join(BASE, 'data')
LOGS_DIR = os.path.join(BASE, 'logs')
PIDFILE = os.path.join(DATA_DIR, '.daemon.pid')
RUNTIME_STATE = os.path.join(DATA_DIR, 'runtime_state.json')
# watchdog 抑制标记: GUI 托盘「彻底退出」(或 bootstrap stop) 时写时间戳,
# watchdog_boot.py 据此在抑制窗口内不复活 Broker (防止"退出后又被拉起")。
WATCHDOG_SUPPRESS_FLAG = os.path.join(DATA_DIR, '.gui_exit_suppress')
WATCHDOG_SUPPRESS_SECONDS = 600
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOGS_DIR, 'broker.log')


def _load_runtime_cfg():
    """config.json 的 runtime 节 (可选; 缺省用内置值)。"""
    try:
        with open(os.path.join(BASE, 'config.json'), 'r', encoding='utf-8') as f:
            import json
            return json.load(f).get('runtime', {}) or {}
    except Exception:
        return {}


RUNTIME_CFG = _load_runtime_cfg()
CHECK_INTERVAL = int(RUNTIME_CFG.get('check_interval', 5))
STABLE_RESET = 300            # 连续稳定 N 秒后重置 backoff
BACKOFF_STEPS = RUNTIME_CFG.get('backoff_steps') or [30, 60, 120, 240, 480, 900]
GRACEFUL_SHUTDOWN_TIMEOUT = int(RUNTIME_CFG.get('graceful_timeout', 15))
ROOT_MEMORY_MB = int(RUNTIME_CFG.get('root_memory_mb', 3072))
LEAF_MEMORY_MB = RUNTIME_CFG.get('leaf_memory_mb') or {'gateway': 1024,
                                                       'task': 1024}

SINGLE_MUTEX = 'Local\\open-ai.broker.mutex'

# 监督目标: role → (脚本, shim 角色, 端口)
SUPERVISED = {
    'gateway': {
        'argv': [procname.SPECS['gateway'].shim_path,
                 os.path.join(BASE, 'main.py'), '--from-broker'],
        'shim_role': 'gateway', 'port': 8000,
        'leaf_mem_mb': LEAF_MEMORY_MB.get('gateway'),
        'cwd': BASE,
    },
    'trae': {
        'argv': [procname.SPECS['trae'].shim_path,
                 os.path.join(BASE, 'trae', 'server.js')],
        'shim_role': 'trae', 'port': 18787,
        'leaf_mem_mb': None,          # node 堆灵活, 不单独立限
        'cwd': os.path.join(BASE, 'trae'),
    },
}

# 所有受管子进程的环境标记: 开启 IPC 心跳/优雅退出协议
CHILD_ENV = dict(os.environ)
CHILD_ENV['OPEN_AI_FROM_BROKER'] = '1'
CHILD_ENV['PYTHONUNBUFFERED'] = '1'

# ---------------------------------------------------------------------------
# 定时任务 (原 daemon.py 的业务职责并入 Broker):
#   每日签到 signin_all.py (9 点; TRAE 9074 繁忙时每 30 分钟补试)
#   逐笔流水 usage_collector.py (每 5 分钟增量采集)
# ---------------------------------------------------------------------------
TASK_SHIM = lambda: procname.shim_for('task')
SIGNIN_SCRIPT = os.path.join(BASE, 'scripts', 'signin_all.py')
COLLECTOR_SCRIPT = os.path.join(BASE, 'scripts', 'usage_collector.py')
SIGNIN_STATE = os.path.join(DATA_DIR, '.daemon_last_signin')
SIGNIN_HOUR = 9
TRAE_STATE = os.path.join(DATA_DIR, '.trae_signin_state')
TRAE_RETRY_STATE = os.path.join(DATA_DIR, '.daemon_last_trae_retry')
TRAE_RETRY_INTERVAL = 30 * 60
USAGE_STATE = os.path.join(DATA_DIR, '.collector_last_run')
USAGE_INTERVAL = 5 * 60
TASK_TIMEOUT = 300


def _read_state(path, default=''):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return default


def _write_state(path, val):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(str(val))
    except Exception:
        pass


class TaskScheduler:
    """短命脚本调度器: 用 task shim 拉起, 计入 task leaf job, 跑完即收。"""

    def __init__(self, broker):
        self.broker = broker
        self._running = {}          # tag -> True (防重入)

    def _busy(self, tag):
        return self._running.get(tag)

    def _run(self, args, tag):
        shim = TASK_SHIM()
        if not shim:
            log('[task] %s: task shim 不可用, 跳过' % tag)
            return -1
        self._running[tag] = True
        try:
            try:
                # DETACHED_PROCESS: 无控制台 → 不产生 conhost.exe
                flags = getattr(subprocess, 'DETACHED_PROCESS', 0x8)
                p = subprocess.Popen([shim] + args, cwd=BASE,
                                     creationflags=flags,
                                     env=CHILD_ENV,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL)
            except Exception as e:
                log('[task] %s 启动失败: %s' % (tag, e))
                return -1
            leaf = self.broker.leaf_jobs.get('task')
            if leaf:
                try:
                    leaf.assign(p)
                except Exception:
                    pass
            log('[task] %s 已启动 pid=%s' % (tag, p.pid))
            try:
                rc = p.wait(timeout=TASK_TIMEOUT)
                log('[task] %s 完成 (exit=%s)' % (tag, rc))
                return rc
            except subprocess.TimeoutExpired:
                log('[task] %s 超时(%ds), 终止' % (tag, TASK_TIMEOUT))
                try:
                    p.kill()
                except Exception:
                    pass
                return -2
        finally:
            self._running.pop(tag, None)

    def _run_async(self, args, tag):
        """后台线程执行 (不阻塞巡检循环); 同名任务运行中则跳过。"""
        if self._busy(tag):
            log('[task] %s 上一轮仍在跑, 跳过本轮' % tag)
            return
        threading.Thread(target=self._run, args=(args, tag),
                         daemon=True, name='task-%s' % tag).start()

    def run_signin(self, args=None):
        self._run_async([SIGNIN_SCRIPT] + (args or []), 'signin')

    def run_collect(self, force=False):
        now = time.time()
        last = float(_read_state(USAGE_STATE, '0') or 0)
        if not force and now - last < USAGE_INTERVAL:
            return
        _write_state(USAGE_STATE, now)
        self._run_async([COLLECTOR_SCRIPT, '--collect'], 'usage')

    def tick(self):
        """Broker 巡检循环里每轮调用。"""
        today = time.strftime('%Y-%m-%d')
        # 每日签到
        if _read_state(SIGNIN_STATE) != today:
            log('[task] 每日签到开始 (%s)' % today)
            self.run_signin()
            _write_state(SIGNIN_STATE, today)
            _write_state(TRAE_RETRY_STATE, time.time())
        elif _read_state(TRAE_STATE) != 'done':
            # TRAE 9074 繁忙补试
            if time.time() - float(_read_state(TRAE_RETRY_STATE, '0') or 0) >= TRAE_RETRY_INTERVAL:
                log('[task] TRAE 仍未签上, 补试 (--trae-only)')
                self.run_signin(['--trae-only'])
                _write_state(TRAE_RETRY_STATE, time.time())
        # WB 补签: 与 TRAE 补试同节奏 (--wb-only 幂等: 今日资源包已入账则秒退)
        # 防 0 点跨天边界 daily-checkin 误报 10001 导致的漏签 (实测漏 100 分/账号)
        if time.time() - float(_read_state(TRAE_RETRY_STATE, '0') or 0) >= TRAE_RETRY_INTERVAL:
            self.run_signin(['--wb-only'])
        # 逐笔流水
        try:
            self.run_collect()
        except Exception as e:
            log('[task] 流水采集异常: %s' % e)

_winsvc = None


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    try:
        sys.stderr.write(line + '\n')
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
_mutex_h = None


def acquire_single_instance():
    global _mutex_h
    # 1) mutex
    try:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k.CreateMutexW(None, False, SINGLE_MUTEX)
        if h and ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(h)
            return False
        _mutex_h = h
    except Exception:
        pass
    # 2) PID 文件存活校验
    try:
        with open(PIDFILE, 'r') as f:
            old = int(f.read().strip())
        k = ctypes.WinDLL('kernel32')
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hp = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, old)
        if hp:
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(hp, ctypes.byref(code))
            k.CloseHandle(hp)
            if code.value == 259:   # STILL_ACTIVE
                return False
    except Exception:
        pass
    try:
        with open(PIDFILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def release_single_instance():
    global _mutex_h
    if _mutex_h:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_mutex_h)
            ctypes.windll.kernel32.CloseHandle(_mutex_h)
        except Exception:
            pass
        _mutex_h = None
    try:
        if os.path.exists(PIDFILE):
            with open(PIDFILE, 'r') as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PIDFILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# IPC 服务端
# ---------------------------------------------------------------------------
class BrokerIPC:
    """命名管道服务端: accept 线程 + 会话轮询。"""

    def __init__(self, broker):
        self.broker = broker
        self.k = ipc._k32()
        self.sessions = {}          # handle -> Session
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name='ipc-accept')

    # --- 管道实例创建 (服务端) ---
    PIPE_ACCESS_DUPLEX = 0x3
    PIPE_TYPE_BYTE = 0x0
    PIPE_READMODE_BYTE = 0x0
    PIPE_WAIT = 0x0

    def _create_pipe_instance(self):
        k = self.k
        k.CreateNamedPipeW.restype = ctypes.c_void_p
        k.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_void_p]
        INVALID = ctypes.c_void_p(-1).value
        h = k.CreateNamedPipeW(
            ipc.PIPE_NAME,
            self.PIPE_ACCESS_DUPLEX | ipc.FILE_FLAG_FIRST_PIPE_INSTANCE * 0,
            self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT,
            32,                       # 最大并发实例
            8192, 8192,
            0, None)
        if h in (None, INVALID):
            err = ctypes.get_last_error()
            if err == 183:            # already exists (第一实例已被本进程建过)
                return None
            raise OSError('CreateNamedPipeW: %d' % err)
        return h

    def _accept_loop(self):
        pending = None
        while not self._stop.is_set():
            h = self._create_pipe_instance()
            if h is None:
                self._stop.wait(0.5)
                continue
            # ConnectNamedPipe (阻塞直到客户端连接; 用小循环 + 超时检查)
            k = self.k
            k.ConnectNamedPipe.restype = ctypes.c_int
            k.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
            ok = k.ConnectNamedPipe(h, None)
            err = ctypes.get_last_error()
            if not ok and err == 535:      # ERROR_PIPE_CONNECTED: 客户端已抢先连上
                ok = True
            if not ok:
                k.DisconnectNamedPipe(h)
                k.CloseHandle(h)
                continue
            s = ipc.Session(h)
            with self._lock:
                self.sessions[h] = s
            # 会话读线程
            threading.Thread(target=self._session_loop, args=(s,),
                             daemon=True, name='ipc-session').start()

    def _session_loop(self, s):
        k = self.k
        while not self._stop.is_set():
            frames = s.read_frames()
            if frames is None:      # 断开
                break
            for m in frames:
                self._handle(s, m)
            # 检查死亡: 30s 无任何消息且未注册 → 踢
            if s.role is None and time.time() - s.connected_at > 30:
                break
            time.sleep(0.2)
        with self._lock:
            self.sessions.pop(s.h, None)
        s.close()
        if s.role:
            log('[ipc] 会话断开: role=%s pid=%s' % (s.role, s.pid))
            self.broker.on_client_leave(s.role, s.pid)

    def _handle(self, s, m):
        mtype = m.get('type')
        if mtype == 'hello':
            s.role = m.get('role')
            s.pid = m.get('pid')
            log('[ipc] 客户端注册: role=%s pid=%s port=%s'
                % (s.role, s.pid, m.get('port')))
            s.write_frame({'type': 'welcome', 'job_assigned': True})
            self.broker.on_client_hello(s.role, s.pid, m.get('port'))
        elif mtype == 'heartbeat':
            s.role = m.get('role') or s.role
            s.pid = m.get('pid') or s.pid
            s.last_beat = time.time()
            self.broker.on_heartbeat(s.role, s.pid)
            # 捎带指令
            cmd = self.broker.pending_command(s.role)
            if cmd:
                s.write_frame(cmd)
        elif mtype == 'bye':
            log('[ipc] 客户端告别: role=%s pid=%s reason=%s'
                % (m.get('role'), m.get('pid'), m.get('reason')))
            self.broker.on_client_leave(m.get('role'), m.get('pid'),
                                        reason=m.get('reason'))
        elif mtype == 'request-shutdown':
            # CLI (bootstrap.py stop) / GUI 托盘 请求全局优雅关闭
            log('[ipc] 收到全局关闭请求 (来自 %s pid=%s): %s'
                % (m.get('role'), m.get('pid'), m.get('reason')))
            s.write_frame({'type': 'welcome', 'job_assigned': True})
            # 注意: 元组逗号必须在括号内 —— args=(expr), 会被解析成
            # args=expr (字符串被 * 拆成单个字符当位置参数),
            # shutdown 线程 TypeError 即死 → 请求收到但永不执行
            # (历史 bug: 托盘/bootstrap 优雅退出全部静默失效的根因)。
            def _safe_shutdown():
                try:
                    self.broker.shutdown(m.get('reason') or 'cli request')
                except Exception:
                    import traceback
                    log('[shutdown] 线程异常:\n' + traceback.format_exc())
            threading.Thread(target=_safe_shutdown, daemon=True,
                             name='broker-shutdown').start()

    def broadcast_shutdown(self, reason='broker stop'):
        with self._lock:
            for s in list(self.sessions.values()):
                try:
                    s.write_frame({'type': 'shutdown', 'graceful': True,
                                   'reason': reason})
                except Exception:
                    pass

    def stop(self):
        self._stop.set()


# FILE_FLAG_FIRST_PIPE_INSTANCE 未用到的常量占位 (保持与 ipc.py 对齐)
ipc.FILE_FLAG_FIRST_PIPE_INSTANCE = getattr(ipc, 'FILE_FLAG_FIRST_PIPE_INSTANCE', 0)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------
class Broker:
    def __init__(self, respawn_enabled=True):
        self.respawn_enabled = respawn_enabled
        self.procs = {}             # role -> dict(proc=Popen, backoff_idx, started_at, last_beat)
        self.shutdown_flag = threading.Event()
        self.root_job = None
        self.leaf_jobs = {}
        self.ipc = None
        self.scheduler = TaskScheduler(self)
        self._pending_cmds = {}     # role -> cmd
        self._seen_heartbeat = {}   # role -> ts
        self._lock = threading.Lock()

    # ---------------- 启动 ----------------
    def start(self):
        log('==== open-ai Broker 启动 (pid=%s) ====' % os.getpid())
        procname.populate()
        # Job 树
        try:
            self.root_job = jobmgmt.Job('open-ai.tree', kill_on_close=True,
                                        memory_limit_mb=ROOT_MEMORY_MB)
            self.root_job.assign(os.getpid())
            log('[job] root job 就绪并已自指派 (KILL_ON_JOB_CLOSE)')
        except Exception as e:
            log('[job] root job 初始化失败 (降级为无 Job 运行): %s' % e)
            self.root_job = None
        try:
            self.leaf_jobs = {
                'gateway': jobmgmt.Job('open-ai.gateway', kill_on_close=True,
                                       memory_limit_mb=LEAF_MEMORY_MB.get('gateway')),
                'trae': jobmgmt.Job('open-ai.trae', kill_on_close=True),
                'task': jobmgmt.Job('open-ai.task', kill_on_close=True,
                                    memory_limit_mb=LEAF_MEMORY_MB.get('task')),
            }
            log('[job] leaf jobs 就绪: %s' % sorted(self.leaf_jobs))
        except Exception as e:
            log('[job] leaf jobs 初始化失败: %s' % e)
            self.leaf_jobs = {}
        # IPC
        self.ipc = BrokerIPC(self)
        self.ipc._thread.start()
        log('[ipc] 服务端就绪: %s' % ipc.PIPE_NAME)
        # 初始拉起
        for role in SUPERVISED:
            self.spawn(role)

    # ---------------- 子进程管理 ----------------
    def spawn(self, role):
        cfg = SUPERVISED[role]
        shim = procname.shim_for(cfg['shim_role'])
        argv = [shim] + cfg['argv'][1:]
        try:
            # DETACHED_PROCESS: 不分配控制台 → 不产生 conhost.exe (任务管理器更干净)
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0x8)
            # 子进程输出落盘 (崩溃诊断用)
            out_log = open(os.path.join(LOGS_DIR, '%s_out.log' % role),
                           'a', encoding='utf-8', errors='replace')
            err_log = open(os.path.join(LOGS_DIR, '%s_err.log' % role),
                           'a', encoding='utf-8', errors='replace')
            p = subprocess.Popen(argv, cwd=cfg['cwd'],
                                 creationflags=flags,
                                 env=CHILD_ENV,
                                 stdout=out_log,
                                 stderr=err_log,
                                 stdin=subprocess.DEVNULL)
        except Exception as e:
            log('[spawn] %s 失败: %s' % (role, e))
            return None
        leaf = self.leaf_jobs.get(role)
        job_note = ''
        if leaf:
            try:
                leaf.assign(p)
                job_note = ' (leaf job OK)'
            except Exception as e:
                job_note = ' (leaf job failed: %s; 已随 root 继承)' % e
        with self._lock:
            self.procs[role] = {
                'proc': p, 'pid': p.pid,
                'started_at': time.time(),
                'backoff_idx': self.procs.get(role, {}).get('backoff_idx', 0),
                'last_beat': 0.0,
            }
        log('[spawn] %s pid=%s%s argv=%s' % (role, p.pid, job_note, argv))
        self._write_state()
        return p

    def kill_child(self, role, graceful=True):
        """先 IPC 优雅关闭, 超时后 leaf job terminate。"""
        info = self.procs.get(role)
        if not info or info['proc'].poll() is not None:
            return
        if graceful:
            self._pending_cmds[role] = {'type': 'shutdown', 'graceful': True,
                                        'reason': 'broker restart'}
            deadline = time.time() + GRACEFUL_SHUTDOWN_TIMEOUT
            while time.time() < deadline:
                if info['proc'].poll() is not None:
                    log('[kill] %s pid=%s 优雅退出 (rc=%s)'
                        % (role, info['pid'], info['proc'].returncode))
                    return
                time.sleep(0.5)
        leaf = self.leaf_jobs.get(role)
        if leaf:
            leaf.terminate()
            log('[kill] %s pid=%s leaf job terminate' % (role, info['pid']))
        else:
            try:
                info['proc'].kill()
            except Exception:
                pass

    # ---------------- IPC 回调 ----------------
    def on_client_hello(self, role, pid, port):
        with self._lock:
            info = self.procs.get(role)
            if info and info['pid'] == pid:
                info['last_beat'] = time.time()

    def on_heartbeat(self, role, pid):
        with self._lock:
            info = self.procs.get(role)
            if info:
                info['last_beat'] = time.time()

    def on_client_leave(self, role, pid, reason=None):
        # 短命任务类客户端离开无需处理; 监督角色由巡检处理
        pass

    def pending_command(self, role):
        with self._lock:
            return self._pending_cmds.pop(role, None)

    # ---------------- 监督循环 ----------------
    def supervise(self):
        # 首轮: 立即补签 + 流水采集
        try:
            self.scheduler.tick()
        except Exception as e:
            log('[task] 首轮调度异常: %s' % e)
        while not self.shutdown_flag.is_set():
            for role, cfg in SUPERVISED.items():
                try:
                    self._check(role)
                except Exception as e:
                    log('[supervise] %s 巡检异常: %s' % (role, e))
            try:
                self.scheduler.tick()
            except Exception as e:
                log('[task] 调度异常: %s' % e)
            self._write_state()
            self.shutdown_flag.wait(CHECK_INTERVAL)

    def _check(self, role):
        info = self.procs.get(role)
        now = time.time()
        if not info:
            self.spawn(role)
            return
        p = info['proc']
        rc = p.poll()
        # 心跳新鲜度 (有 IPC 注册才算; 老进程没有 IPC 时以 pid 存活为准)
        beat_age = (now - info['last_beat']) if info['last_beat'] else None
        fresh = beat_age is not None and beat_age < ipc.DEAD_AFTER * 3
        if rc is not None:
            wait = BACKOFF_STEPS[min(info['backoff_idx'], len(BACKOFF_STEPS) - 1)]
            stable = now - info['started_at'] > STABLE_RESET
            if stable:
                info['backoff_idx'] = 0
            log('[supervise] %s pid=%s 退出 (rc=%s, 存活 %.0fs) → %ds 后重启'
                % (role, info['pid'], rc, now - info['started_at'], wait))
            self.shutdown_flag.wait(wait)
            if self.shutdown_flag.is_set():
                return
            with self._lock:
                self.procs[role]['backoff_idx'] = min(
                    info['backoff_idx'] + 1, len(BACKOFF_STEPS) - 1)
            self.spawn(role)
            return
        # 僵死: 心跳超时 (仅当客户端曾注册过心跳)
        if info['last_beat'] and beat_age and beat_age > ipc.HEARTBEAT_INTERVAL * 3 + 10:
            log('[supervise] %s pid=%s 心跳超时 (%.0fs) → 判定僵死, 强制重启'
                % (role, info['pid'], beat_age))
            self.kill_child(role, graceful=True)
            with self._lock:
                self.procs[role]['backoff_idx'] = min(
                    info['backoff_idx'] + 1, len(BACKOFF_STEPS) - 1)
            self.spawn(role)

    # ---------------- 状态落盘 ----------------
    def _write_state(self):
        try:
            st = {'broker_pid': os.getpid(), 'ts': time.time(), 'children': {}}
            for role, info in self.procs.items():
                p = info['proc']
                st['children'][role] = {
                    'pid': info['pid'],
                    'alive': p.poll() is None if p else False,
                    'started_at': info['started_at'],
                    'last_beat': info['last_beat'],
                }
            tmp = RUNTIME_STATE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
            os.replace(tmp, RUNTIME_STATE)   # 原子替换, 避免读到半截文件
        except Exception:
            pass

    # ---------------- 全局关闭 ----------------
    def shutdown(self, reason='stop'):
        if self.shutdown_flag.is_set():
            return
        log('[shutdown] 开始 (%s)' % reason)
        self.shutdown_flag.set()
        try:
            self.ipc.broadcast_shutdown(reason)
        except Exception:
            pass
        # 优雅退出属于用户主动关闭 → 写 watchdog 抑制标记,
        # 计划任务在抑制窗口内不再拉起 (崩溃/被杀不写标记, 仍会自动复活)
        try:
            with open(WATCHDOG_SUPPRESS_FLAG, 'w', encoding='utf-8') as f:
                f.write(str(time.time()))
        except Exception:
            pass
        # 等优雅退出
        deadline = time.time() + GRACEFUL_SHUTDOWN_TIMEOUT
        for role in SUPERVISED:
            info = self.procs.get(role)
            if info:
                self.kill_child(role, graceful=True)
        # 兜底: root job 整树清零
        if self.root_job:
            self.root_job.terminate()
        log('[shutdown] 完成')
        self._write_state()


# ---------------------------------------------------------------------------
# 入口: python app_runtime.py  → 常驻 Broker
# ---------------------------------------------------------------------------
def main():
    if not acquire_single_instance():
        log('[boot] 已有 Broker 运行, 本进程退出')
        return 0
    try:
        broker = Broker()
        broker.start()
        try:
            broker.supervise()
        except KeyboardInterrupt:
            pass
        broker.shutdown('main loop exit')
        return 0
    finally:
        release_single_instance()


if __name__ == '__main__':
    sys.exit(main())
