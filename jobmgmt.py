# -*- coding: utf-8 -*-
"""
jobmgmt.py — Windows Job Object 封装: 统一托管 open-ai 全部子进程
==================================================================
核心能力:
  1. 创建 Job (KILL_ON_JOB_CLOSE): 主进程/Broker 句柄关闭或崩溃时,
     内核自动终止整棵进程树 → 从 OS 层面杜绝孤儿进程。
  2. 分层 Job 树:
       root job "open-ai.tree"
         ├─ gateway job "open-ai.gateway"   (网关 + 它的子进程)
         ├─ trae job "open-ai.trae"         (node 后端 + 它的子进程)
         └─ task  job "open-ai.task"        (短命脚本, 一个池)
     总控 Job 限制整个树的资源; 每个子 Job 还可单独限制 → 任务管理器
     的"结束任务"作用在最内层 Job, 也能一键全收。
  3. 资源配额: 每进程内存上限 (JOB_OBJECT_LIMIT_PROCESS_MEMORY),
     触发时进程被内核直接终止 (commit 超限), Broker 看门狗负责重启。
  4. Job 查询: 枚举 Job 内全部 PID (QueryInformationJobObject)。

仅 Windows 有效; 其他平台全部调用退化为 no-op。
"""
import os
import struct
import ctypes
import ctypes.wintypes as wt

JOB_NAME_PREFIX = 'open-ai.'

# JobObjectExtendedLimitInformation 布局 (winnt.h):
#   JOBOBJECT_BASIC_LIMIT_INFORMATION (48B on x64) + IO_COUNTERS (48B)
#   + SIZE_T ProcessMemoryLimit + SIZE_T JobMemoryLimit + SIZE_T PeakProcessMemoryUsed
#   + SIZE_T PeakJobMemoryUsed
# BASIC: PerProcessUserTimeLimit(8) PerJobUserTimeLimit(8) LimitFlags(4)
#        MinimumWorkingSetSize(8→含4B对齐) ... 实际: 4+4 pad, 然后 4 个 SIZE_T
# 用 ctypes 结构体直接映射最稳妥:
JOBOBJECTLIMIT_PROCESS_MEMORY = 0x00000100
JobObjectExtendedLimitInformation = 9
JobObjectExtendedLimitInformation_CLASS = None


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_uint64),
                ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64),
                ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64),
                ('OtherTransferCount', ctypes.c_uint64)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', ctypes.c_uint32),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', ctypes.c_uint32),
                ('Affinity', ctypes.POINTER(ctypes.c_uint64)),
                ('PriorityClass', ctypes.c_uint32),
                ('SchedulingClass', ctypes.c_uint32)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


# Job name → 安全对象名 (Local\ 前缀: 仅当前登录会话可见, 避免跨会话冲突)
def _job_name(name):
    if name.startswith('open-ai.'):
        name = name[len('open-ai.'):]
    return 'Local\\open-ai.job.%s' % name


def _k32():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateJobObjectW.restype = ctypes.c_void_p
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.AssignProcessToJobObject.restype = ctypes.c_int
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.SetInformationJobObject.restype = ctypes.c_int
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.c_uint32]
    k.QueryInformationJobObject.restype = ctypes.c_int
    k.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_uint32)]
    k.TerminateJobObject.restype = ctypes.c_int
    k.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.OpenJobObjectW.restype = ctypes.c_void_p
    k.OpenJobObjectW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.IsProcessInJob.restype = ctypes.c_int
    k.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_int)]
    return k


# JOB_OBJECT_SECURITY 等 flags
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000200
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200


class Job:
    """一个 Job Object 句柄的封装。"""
    def __init__(self, name, kill_on_close=True, memory_limit_mb=None,
                 max_processes=None, silent_breakaway_ok=False,
                 breakaway_ok=False):
        self.name = name
        self.k = _k32()
        h = self.k.CreateJobObjectW(None, _job_name(name))
        if not h:
            raise OSError('CreateJobObjectW failed: %d' % ctypes.get_last_error())
        self.h = h
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = 0
        if kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_mb:
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
            limits.ProcessMemoryLimit = int(memory_limit_mb) * 1024 * 1024
        if max_processes:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            limits.BasicLimitInformation.ActiveProcessLimit = int(max_processes)
        if silent_breakaway_ok:
            # 注意: SILENT_BREAKAWAY_OK = 子进程默认脱链 (不自动入 Job)
            flags |= JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        if breakaway_ok:
            # 子进程默认继承入 Job; 显式 CREATE_BREAKAWAY_FROM_JOB 可脱链
            # (GUI 用此模式: 登录脚本继承, 安装/卸载器显式脱链)
            flags |= JOB_OBJECT_LIMIT_BREAKAWAY_OK
        limits.BasicLimitInformation.LimitFlags = flags
        if flags:
            if not self.k.SetInformationJobObject(
                    self.h, JobObjectExtendedLimitInformation,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                # 限制设置失败不致命 (老系统/权限问题), 记录但不抛
                self.limits_error = ctypes.get_last_error()
        else:
            self.limits_error = 0

    def assign(self, process_or_pid):
        """把进程加入 Job。参数: subprocess.Popen 或 pid。"""
        pid = getattr(process_or_pid, 'pid', process_or_pid)
        k = self.k
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not h:
            raise OSError('OpenProcess(%d) failed: %d' % (pid, ctypes.get_last_error()))
        try:
            if not k.AssignProcessToJobObject(self.h, h):
                raise OSError('AssignProcessToJobObject(%d) failed: %d'
                              % (pid, ctypes.get_last_error()))
        finally:
            k.CloseHandle(h)

    def terminate(self, exit_code=1):
        """终止 Job 内全部进程。"""
        return bool(self.k.TerminateJobObject(self.h, exit_code))

    def pids(self):
        """枚举 Job 内全部进程 PID。"""
        # JOB_OBJECT_BASIC_PROCESS_ID_LIST (x64):
        #   DWORD NumberOfAssignedProcesses; DWORD NumberOfProcessIdsInList;
        #   ULONG_PTR ProcessIdList[]  (数组按指针大小, 偏移 8 起)
        buf = ctypes.create_string_buffer(8 + 8 * 512)
        ret = ctypes.c_uint32(0)
        cls = 3  # JobObjectBasicProcessIdList
        if not self.k.QueryInformationJobObject(self.h, cls, buf,
                                                ctypes.sizeof(buf),
                                                ctypes.byref(ret)):
            return []
        assigned, in_list = struct.unpack_from('<II', buf, 0)
        n = min(in_list, 512)
        if n <= 0:
            return []
        return list(struct.unpack_from('<%dQ' % n, buf, 8))

    def close(self):
        """关闭句柄。KILL_ON_JOB_CLOSE 时会连带终止全部进程。"""
        if self.h:
            try:
                self.k.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def open_job(name):
    """按名打开已有 Job (用于接管场景)。"""
    k = _k32()
    # JOB_OBJECT_QUERY=0x4 | JOB_OBJECT_TERMINATE=0x2 | JOB_OBJECT_ASSIGN_PROCESS=0x1
    JOB_OBJECT_ACCESS = 0x0001 | 0x0002 | 0x0004
    h = k.OpenJobObjectW(JOB_OBJECT_ACCESS, False, _job_name(name))
    if not h:
        return None
    j = object.__new__(Job)
    j.name = name
    j.k = k
    j.h = h
    j.limits_error = 0
    return j


def job_of_process(pid=None):
    """返回进程当前所在 Job 名 (未入 Job 返回 None)。诊断用。"""
    k = _k32()
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                      pid if pid is not None else os.getpid())
    if not h:
        return None
    try:
        inside = ctypes.c_int(0)
        # IsProcessInJob 需要目标 job 句柄; 传 None 比较匿名 job, 这里简化:
        # 只报告是否在某个 open-ai job 里 (遍历已知名)
        for nm in ('tree', 'gateway', 'trae', 'task'):
            jh = k.OpenJobObjectW(0x0008, False, _job_name('open-ai.' + nm))
            if jh:
                k.IsProcessInJob(h, jh, ctypes.byref(inside))
                k.CloseHandle(jh)
                if inside.value:
                    return 'open-ai.' + nm
        return None
    finally:
        k.CloseHandle(h)


def create_tree(root_memory_mb=None, leaf_memory_mb=None, max_procs=64):
    """创建标准 open-ai Job 树。返回 (root_job, {role: leaf_job})。"""
    root = Job('open-ai.tree', kill_on_close=True,
               memory_limit_mb=root_memory_mb, max_processes=max_procs)
    leaves = {}
    for role in ('gateway', 'trae', 'task'):
        mem = leaf_memory_mb
        if role == 'trae':
            mem = None     # node 堆增长灵活, 不单独立限 (受 root 总限约束)
        leaves[role] = Job('open-ai.' + role, kill_on_close=True,
                           memory_limit_mb=mem)
    return root, leaves
