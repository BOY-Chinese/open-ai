# -*- coding: utf-8 -*-
"""
ipc.py — open-ai 标准化 IPC: 命名管道协议 + 心跳 + 优雅退出
============================================================
传输: Windows named pipe \\\\.\\pipe\\open-ai.broker
帧格式: 4 字节小端长度 + UTF-8 JSON 载荷 (单帧 ≤ 64KB)

角色分工:
  - Broker (daemon.py/app_runtime.py) 是服务端: 接受注册、收心跳、下发指令
  - 子进程 (gateway/trae/task/...) 是客户端: 启动后发 HELLO, 周期发 HEARTBEAT,
    收到 SHUTDOWN 后自行清理退出 (优雅退出协议)

消息类型:
  客户端 → 服务端:
    hello       {role, pid, port?}          注册 (首帧, 必发)
    heartbeat   {role, pid, cpu?, mem?}     心跳 (默认 10s 一跳)
    bye         {role, pid, reason?}        主动退出前告知
  服务端 → 客户端:
    welcome     {job_assigned: bool}        注册应答
    shutdown    {graceful: true, reason}    优雅退出指令
    ping        {}                          服务端探活

设计要点:
  - 客户端写、服务端读为主方向; 服务端在心跳应答里"捎带"指令, 避免服务端
    主动写阻塞。客户端每跳收一次应答, 无应答即视为断管。
  - 全部非阻塞式短读写 + 超时, 任何异常都不让调用方崩溃 (IPC 是尽力而为)。
"""
import os
import sys
import json
import time
import struct
import ctypes
import threading

PIPE_NAME = r'\\.\pipe\open-ai.broker'
MAX_FRAME = 64 * 1024
HEARTBEAT_INTERVAL = 10.0        # 客户端心跳周期 (秒)
DEAD_AFTER = 3.0                 # 心跳缺失判定死亡: interval * 3 - 1s 余量
DEFAULT_CONNECT_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# 帧编解码
# ---------------------------------------------------------------------------
def encode_frame(obj):
    raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_FRAME - 4:
        raw = raw[:MAX_FRAME - 4]
    return struct.pack('<I', len(raw)) + raw


class FrameReader:
    """流式帧读取: feed() 喂字节, frames() 吐完整帧。"""
    def __init__(self):
        self._buf = b''

    def feed(self, data):
        self._buf += data
        out = []
        while len(self._buf) >= 4:
            (n,) = struct.unpack_from('<I', self._buf, 0)
            if n > MAX_FRAME:            # 协议错乱, 丢弃重同步
                self._buf = b''
                break
            if len(self._buf) < 4 + n:
                break
            payload = self._buf[4:4 + n]
            self._buf = self._buf[4 + n:]
            try:
                out.append(json.loads(payload.decode('utf-8')))
            except Exception:
                pass
        return out


# ---------------------------------------------------------------------------
# Win32 命名管道 (纯 ctypes: CreateFile / ReadFile / WriteFile)
# ---------------------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232


def _k32():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p]
    k.ReadFile.restype = ctypes.c_int
    k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
                           ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k.WriteFile.restype = ctypes.c_int
    k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.PeekNamedPipe.restype = ctypes.c_int
    k.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    return k


def _wait_pipe(timeout):
    """WaitNamedPipe 轮询等待管道可用。"""
    k = _k32()
    k.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        if k.WaitNamedPipeW(PIPE_NAME, 500):
            return True
        err = ctypes.get_last_error()
        last_err = err
        if err not in (ERROR_PIPE_BUSY, 2):   # 2=pipe not yet created
            if os.environ.get('BOOTSTRAP_DEBUG'):
                print('[debug] WaitNamedPipe err=%d' % err)
            return False
        time.sleep(0.15)
    if os.environ.get('BOOTSTRAP_DEBUG'):
        print('[debug] WaitNamedPipe timeout (last err=%s)' % last_err)
    return False


class PipeClient:
    """IPC 客户端: 连接 Broker, 收发帧。线程安全 (内部锁串行化写)。"""
    def __init__(self, timeout=DEFAULT_CONNECT_TIMEOUT):
        self._k = _k32()
        self._h = None
        self._lock = threading.Lock()
        self._connect(timeout)

    def _connect(self, timeout):
        if not _wait_pipe(timeout):
            raise ConnectionError('pipe not available: %s' % PIPE_NAME)
        access = GENERIC_READ | GENERIC_WRITE
        h = self._k.CreateFileW(PIPE_NAME, access, 0, None, OPEN_EXISTING,
                                0, None)
        if h in (None, INVALID_HANDLE_VALUE):
            raise ConnectionError('CreateFileW failed: %d' % ctypes.get_last_error())
        self._h = h

    @property
    def connected(self):
        return self._h is not None

    def send(self, obj):
        """发送一帧; 失败返回 False (不抛异常)。"""
        if not self._h:
            return False
        frame = encode_frame(obj)
        with self._lock:
            written = ctypes.c_uint32(0)
            ok = self._k.WriteFile(self._h, frame, len(frame),
                                   ctypes.byref(written), None)
            if not ok or written.value != len(frame):
                self.close()
                return False
        return True

    def recv(self, timeout=0.5):
        """尝试读一帧 (Peek+Read 非阻塞轮询); 无数据返回 None。"""
        if not self._h:
            return None
        deadline = time.time() + timeout
        reader = getattr(self, '_reader', None)
        if reader is None:
            reader = self._reader = FrameReader()
        while time.time() < deadline:
            k = self._k
            avail = ctypes.c_uint32(0)
            if not k.PeekNamedPipe(self._h, None, 0, None, ctypes.byref(avail),
                                   None):
                err = ctypes.get_last_error()
                # 管道断开/对端关闭
                self.close()
                return None
            if avail.value:
                chunk = ctypes.create_string_buffer(min(avail.value, 65536))
                got = ctypes.c_uint32(0)
                if k.ReadFile(self._h, chunk, len(chunk), ctypes.byref(got),
                              None) and got.value:
                    frames = reader.feed(chunk.raw[:got.value])
                    if frames:
                        return frames[0]
                    continue
            time.sleep(0.05)
        return None

    def close(self):
        if self._h:
            try:
                self._k.CloseHandle(self._h)
            except Exception:
                pass
            self._h = None


# ---------------------------------------------------------------------------
# 客户端心跳线程 (子进程侧一键接入)
# ---------------------------------------------------------------------------
class HeartbeatClient:
    """子进程侧: 连接 + hello + 周期心跳 + 监听 shutdown 指令。

    用法:
        hb = HeartbeatClient(role='gateway', port=8000)
        hb.start()                      # 后台线程
        ...
        if hb.shutdown_requested():     # 主循环里轮询
            do_cleanup(); sys.exit(0)
        ...
        hb.stop()
    """
    def __init__(self, role, pid=None, port=None, on_shutdown=None):
        self.role = role
        self.pid = pid if pid is not None else os.getpid()
        self.port = port
        self.on_shutdown = on_shutdown
        self._client = None
        self._thread = None
        self._stop_evt = threading.Event()
        self._shutdown_evt = threading.Event()
        self._registered = threading.Event()

    # -- 生命周期 --
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='ipc-heartbeat-%s' % self.role)
        self._thread.start()
        return self

    def stop(self, reason='stop'):
        try:
            if self._client and self._client.connected:
                self._client.send({'type': 'bye', 'role': self.role,
                                   'pid': self.pid, 'reason': reason})
        except Exception:
            pass
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)

    # -- 状态 --
    def shutdown_requested(self):
        return self._shutdown_evt.is_set()

    def is_registered(self, timeout=None):
        if timeout is None:
            return self._registered.is_set()
        return self._registered.wait(timeout)

    # -- 内部 --
    def _send(self, obj):
        try:
            return self._client.send(obj) if self._client else False
        except Exception:
            return False

    def _run(self):
        backoff = 1.0
        while not self._stop_evt.is_set():
            # 连接 (指数退避重连)
            if not self._client or not self._client.connected:
                try:
                    self._client = PipeClient()
                    self._registered.clear()
                    self._send({'type': 'hello', 'role': self.role,
                                'pid': self.pid, 'port': self.port})
                except Exception:
                    self._client = None
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            backoff = 1.0
            # 心跳
            self._send({'type': 'heartbeat', 'role': self.role,
                        'pid': self.pid,
                        'ts': time.time()})
            # 收应答/指令
            deadline = time.time() + HEARTBEAT_INTERVAL
            while time.time() < deadline and not self._stop_evt.is_set():
                try:
                    msg = self._client.recv(timeout=0.5)
                except Exception:
                    msg = None
                if msg:
                    mtype = msg.get('type')
                    if mtype == 'welcome':
                        self._registered.set()
                    elif mtype == 'shutdown':
                        self._shutdown_evt.set()
                        cb = self.on_shutdown
                        if cb:
                            try:
                                cb(msg)
                            except Exception:
                                pass
                        return
                    elif mtype == 'ping':
                        pass
                if self._shutdown_evt.is_set():
                    return
                self._stop_evt.wait(0.2)


# ---------------------------------------------------------------------------
# 服务端会话 (Broker 侧; 配合重叠 IO 管道服务端使用)
# ---------------------------------------------------------------------------
class Session:
    """一条已连接管道的会话状态。"""
    def __init__(self, handle):
        self.h = handle
        self.reader = FrameReader()
        self.role = None
        self.pid = None
        self.last_beat = time.time()
        self.connected_at = time.time()

    def read_frames(self):
        """非阻塞读; 返回帧列表。管道断开返回 None。"""
        k = _k32()
        # PeekNamedPipe 判断有无数据
        k.PeekNamedPipe.restype = ctypes.c_int
        k.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_uint32),
                                    ctypes.c_void_p]
        avail = ctypes.c_uint32(0)
        if not k.PeekNamedPipe(self.h, None, 0, None, ctypes.byref(avail), None):
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, 233):   # 233=no process on other end
                return None
            return None
        if not avail.value:
            return []
        chunk = ctypes.create_string_buffer(min(avail.value, MAX_FRAME))
        got = ctypes.c_uint32(0)
        if not k.ReadFile(self.h, chunk, len(chunk), ctypes.byref(got), None):
            return None
        return self.reader.feed(chunk.raw[:got.value])

    def write_frame(self, obj):
        k = _k32()
        frame = encode_frame(obj)
        written = ctypes.c_uint32(0)
        ok = k.WriteFile(self.h, frame, len(frame), ctypes.byref(written), None)
        return bool(ok and written.value == len(frame))

    def close(self):
        try:
            _k32().CloseHandle(self.h)
        except Exception:
            pass


# 控制台辅助: 让子进程在退出时自动 bye
def install_exit_notifier(client, role):
    import atexit
    atexit.register(lambda: client.stop(reason='atexit'))
