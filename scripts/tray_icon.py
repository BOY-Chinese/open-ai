# -*- coding: utf-8 -*-
"""
tray_icon.py — open-ai 系统托盘组件 (纯 Win32, 零第三方依赖)
=============================================================
抖音式托盘应用行为的"托盘"部分:

  - 常驻托盘图标 (Shell_NotifyIconW, NOTIFYICON_VERSION_4)
  - 左键单击托盘图标 → on_restore (恢复主窗口)
  - 右键托盘图标 → 声明式可扩展菜单 (MenuItem, 见下方说明)
  - 主窗口 WM_CLOSE (X 按钮) 子类化拦截 → on_close_request (隐藏而非销毁)
  - Explorer 重启 (TaskbarCreated 广播) 后自动重新添加图标

右键菜单 —— 可扩展功能模块:
  菜单用 MenuItem 声明式列表描述, 支持静态值与 callable 动态值
  (label/checked/enabled/visible 每次右键实时求值), 支持任意深度
  子菜单、勾选、灰显、顶层默认加粗项 (SetMenuDefaultItem)。
  扩展方式 (GUI 侧直接传 menu=, 或运行期 tray.menu.append(...)):
      menu=[
          MenuItem(label='显示主窗口', default=True, on_click=self.restore),
          MenuItem(label=f'网关 :8000', on_click=self.open_gateway),
          MenuItem(kind='checkbox', label='开机自启',
                   checked=self._is_autostart, on_click=self._toggle_autostart),
          MenuItem(label='更多', submenu=[
              MenuItem(label='日志目录', on_click=self.open_logs),
              MenuItem(kind='separator'),
              MenuItem(label='关于', on_click=self.about),
          ]),
          MenuItem(kind='separator'),
          MenuItem(label='退出', on_click=self.exit_app),
      ]
  未传 menu= 时使用内置默认菜单 (显示主窗口 / 退出, 兼容 on_restore/on_exit)。

实现方式:
  托盘图标跑在独立 UI 线程的一个隐藏顶级窗口上; 主窗口用
  SetWindowLongPtrW 子类化在原窗口过程之前拦截 WM_CLOSE。
  两个 UI 线程互不阻塞, 所有用户回调统一经 root.after(0, ...)
  编组回 Tk 主线程执行。

  生命周期: start() 起线程 → 加图标; destroy() 幂等:
  NIM_DELETE → 恢复主窗口原窗口过程 → 销毁消息窗口 (同线程) → 线程退出。

  错误处理: 窗口过程的异常会被系统吞掉, 因此菜单/托盘故障一律
  落盘 logs/tray_icon.err.log (troubleshoot 首查此文件)。
"""
import os
import sys
import ctypes
import ctypes.wintypes as wt
import threading
import traceback
if os.name != 'nt':
    # 非 Windows (如 WSL 下跑单元测试): 抛 ImportError, 由调用方降级为普通窗口
    raise ImportError('tray_icon 仅支持 Windows (需要 user32/shell32)')

# ============================== Win32 常量 ==============================
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 0x517          # 自定义托盘回调消息 (≥ WM_APP)

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
NIF_SHOWTIP = 0x80                    # Vista+: szTip 精确显示
NOTIFYICON_VERSION_4 = 4

NIN_SELECT = 0x0400                   # uVersion=4: 左键选择事件
NIN_LUP, NIN_RUP = 0x0001, 0x0002     # uVersion=4: 左/右键抬起旗标 (lParam 低位)

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
GWL_WNDPROC = -4

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_POPUP = 0x0010                     # 子菜单 (wID 参数为 HMENU)
MF_CHECKED = 0x0008
MF_GRAYED = 0x0001
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

_INVALID = ctypes.c_void_p(-1).value
_PTR64 = ctypes.sizeof(ctypes.c_void_p) == 8

LRESULT_T = ctypes.c_longlong if _PTR64 else ctypes.c_long

_MENU_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'logs', 'tray_icon.err.log')


def _log_err(text):
    """托盘组件错误落盘 (logs/tray_icon.err.log) —— 窗口过程会吞异常, 必须留痕。"""
    try:
        os.makedirs(os.path.dirname(_MENU_LOG), exist_ok=True)
        import time as _t
        with open(_MENU_LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (_t.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Win32 绑定 (声明一次, 全模块共用)
# ---------------------------------------------------------------------------
def _u32():
    u = ctypes.WinDLL('user32', use_last_error=True)
    # 窗口类 / 窗口
    u.RegisterClassExW.restype = wt.ATOM
    u.RegisterClassExW.argtypes = [ctypes.c_void_p]
    u.CreateWindowExW.restype = ctypes.c_void_p
    u.CreateWindowExW.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                  wt.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND,
                                  ctypes.c_void_p, wt.HINSTANCE,
                                  ctypes.c_void_p]
    u.DefWindowProcW.restype = LRESULT_T
    u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.CallWindowProcW.restype = LRESULT_T
    u.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT,
                                  wt.WPARAM, wt.LPARAM]
    # GetWindowLongPtrW 仅 64 位 user32 导出; 32 位回落 GetWindowLongW
    if hasattr(u, 'GetWindowLongPtrW'):
        u.GetWindowLongPtrW.restype = LRESULT_T
        u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowLongPtrW.restype = LRESULT_T
        u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, LRESULT_T]
        u._getwndproc = u.GetWindowLongPtrW
        u._setwndproc = u.SetWindowLongPtrW
    else:
        u.GetWindowLongW.restype = LRESULT_T
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowLongW.restype = LRESULT_T
        u.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, LRESULT_T]
        u._getwndproc = u.GetWindowLongW
        u._setwndproc = u.SetWindowLongW
    u.DestroyWindow.argtypes = [wt.HWND]
    u.IsWindow.restype = wt.BOOL
    u.IsWindow.argtypes = [wt.HWND]
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.GetCursorPos.argtypes = [ctypes.c_void_p]
    u.SetMenuDefaultItem.restype = ctypes.c_int
    u.SetMenuDefaultItem.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_uint]
    u.RegisterWindowMessageW.restype = wt.UINT
    u.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.GetWindowThreadProcessId.restype = wt.DWORD
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.c_void_p]
    u.EnumWindows.restype = wt.BOOL
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND,
                                                 wt.LPARAM), wt.LPARAM]
    u.PeekMessageW.restype = wt.BOOL
    u.PeekMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT,
                               wt.UINT]
    u.FindWindowW.restype = ctypes.c_void_p
    u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    # 消息循环
    u.GetMessageW.restype = ctypes.c_int
    u.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
    u.TranslateMessage.argtypes = [ctypes.c_void_p]
    u.DispatchMessageW.argtypes = [ctypes.c_void_p]
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.PostMessageW.restype = wt.BOOL
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    # 图标 / 菜单
    u.LoadImageW.restype = ctypes.c_void_p
    u.LoadImageW.argtypes = [wt.HINSTANCE, ctypes.c_void_p, wt.UINT,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
    u.LoadIconW.restype = ctypes.c_void_p
    u.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    u.DestroyIcon.argtypes = [wt.HICON]
    u.CreatePopupMenu.restype = ctypes.c_void_p
    u.CreatePopupMenu.argtypes = []
    u.AppendMenuW.restype = wt.BOOL
    u.AppendMenuW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
                              ctypes.c_wchar_p]
    u.TrackPopupMenuEx.restype = ctypes.c_int
    u.TrackPopupMenuEx.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, ctypes.c_void_p]
    u.DestroyMenu.argtypes = [ctypes.c_void_p]
    return u


def _k32():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetModuleHandleW.restype = ctypes.c_void_p
    k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k.GetCurrentThreadId.restype = wt.DWORD
    return k


def _s32():
    s = ctypes.WinDLL('shell32', use_last_error=True)
    s.Shell_NotifyIconW.restype = wt.BOOL
    s.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]
    return s


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
class NOTIFYICONDATAW(ctypes.Structure):
    """完整 NOTIFYICONDATAW (Vista+ 布局; x64 下 976 字节, 与系统 ABI 一致)。"""
    class _UNION(ctypes.Union):
        _fields_ = [('uTimeout', wt.UINT), ('uVersion', wt.UINT)]

    _anonymous_ = ('u',)
    _fields_ = [('cbSize', wt.DWORD),
                ('hWnd', wt.HWND),        # x64: 指针成员使结构 8 字节对齐
                ('uID', wt.UINT),
                ('uFlags', wt.UINT),
                ('uCallbackMessage', wt.UINT),
                ('hIcon', wt.HICON),
                ('szTip', wt.WCHAR * 128),
                ('dwState', wt.DWORD),
                ('dwStateMask', wt.DWORD),
                ('szInfo', wt.WCHAR * 256),
                ('u', _UNION),
                ('szInfoTitle', wt.WCHAR * 64),
                ('dwInfoFlags', wt.DWORD),
                ('guidItem', ctypes.c_byte * 16),
                ('hBalloonIcon', wt.HICON)]


class _MSG(ctypes.Structure):
    _fields_ = [('hwnd', wt.HWND), ('message', wt.UINT),
                ('wParam', wt.WPARAM), ('lParam', wt.LPARAM),
                ('time', wt.DWORD), ('pt', wt.POINT)]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('style', wt.UINT),
                ('lpfnWndProc', ctypes.WINFUNCTYPE(LRESULT_T, wt.HWND,
                                                   wt.UINT, wt.WPARAM,
                                                   wt.LPARAM)),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', ctypes.c_void_p), ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', ctypes.c_void_p),
                ('lpszClassName', ctypes.c_wchar_p),
                ('hIconSm', wt.HICON)]


assert ctypes.sizeof(_WNDCLASSEXW) == (80 if _PTR64 else 48)
# NOTIFYICONDATAW: x64=976 (指针成员 8 字节对齐), x86=952
assert ctypes.sizeof(NOTIFYICONDATAW) == (976 if _PTR64 else 952)


# ---------------------------------------------------------------------------
# MenuItem — 声明式菜单模型 (可扩展功能模块)
# ---------------------------------------------------------------------------
class MenuItem:
    """托盘右键菜单条目 (声明式, 支持静态值或 callable 动态值)。

    字段 (除 kind 外全部可传 callable, 菜单弹出时实时求值):
        kind      'command' | 'separator' | 'checkbox'
        label     显示文字 (callable(str) 可动态, 如 f"网关: {'在线' if ...}")
        on_click  点击回调 (checkbox 勾选/取消都会调用)
        submenu   MenuItem 列表 (嵌套子菜单)
        checked   是否勾选 (checkbox 用; callable 可动态)
        enabled   是否可用 (False 灰显)
        visible   是否显示 (False 跳过)
        default   顶层默认项 (加粗, 双击托盘等价点击它)

    示例 (追加新功能只需往 menu 列表加一项):
        MenuItem(label=lambda: f"网关 {8000} 在线",
                 on_click=self.open_dashboard)
        MenuItem(kind='checkbox', label='开机自启',
                 checked=lambda: self._read_autostart(),
                 on_click=lambda: self._toggle_autostart())
        MenuItem(label='更多', submenu=[
            MenuItem(label='日志目录', on_click=self._open_logs),
            MenuItem(kind='separator'),
            MenuItem(label='关于', on_click=self._about),
        ])
    """

    __slots__ = ('kind', 'label', 'on_click', 'submenu', 'checked',
                 'enabled', 'visible', 'default')

    def __init__(self, kind='command', label='', on_click=None, submenu=None,
                 checked=False, enabled=True, visible=True, default=False):
        self.kind = kind
        self.label = label
        self.on_click = on_click
        self.submenu = submenu
        self.checked = checked
        self.enabled = enabled
        self.visible = visible
        self.default = default

    # ---- 求值辅助 (callable → 值, 任何异常按 False/默认处理) ----
    def _eval(self, v):
        if callable(v):
            try:
                return v()
            except Exception:
                return False
        return v

    def is_checked(self):
        return bool(self._eval(self.checked))

    def is_enabled(self):
        return bool(self._eval(self.enabled))

    def is_visible(self):
        return bool(self._eval(self.visible))


# ---------------------------------------------------------------------------
# TrayIcon
# ---------------------------------------------------------------------------
class TrayIcon:
    """抖音式托盘组件: 常驻托盘图标 + 可扩展右键菜单 + 主窗口关闭拦截。

    参数:
        parent           Tk root (用于 .after 编组回调到主线程)
        icon_path        托盘 .ico 路径 (失败回落系统默认图标)
        tooltip          托盘悬浮提示
        on_restore       左键单击托盘 → 恢复主窗口
        on_exit          菜单「退出」(彻底退出: 清理后端→销毁托盘→结束)
        on_close_request 主窗口 X 拦截回调 (隐藏到托盘, 不销毁)
        main_hwnd        主窗口 HWND (X 拦截; None 则只做托盘)
        menu             右键菜单 (MenuItem 列表; None 时用 DEFAULT_MENU)

    可扩展右键菜单 (MenuItem 声明式模型, 详见 MenuItem docstring):
        tray = TrayIcon(root, icon_path=..., tooltip=..., on_restore=...,
                        menu=[
                            MenuItem(label='显示主窗口', default=True,
                                     on_click=self._on_tray_restore),
                            MenuItem(kind='separator'),
                            MenuItem(label='打开网关页',
                                     on_click=self._open_gateway),
                            MenuItem(label='更多', submenu=[
                                MenuItem(label='日志目录', on_click=...),
                            ]),
                            MenuItem(kind='separator'),
                            MenuItem(label='退出', on_click=self._on_tray_exit),
                        ])
        tray.start()
        # 运行期动态增删: tray.menu.append(MenuItem(...)) → 下次右键生效
        ...
        tray.destroy()      # 程序退出前 (幂等)
    """

    MENU_SHOW = 1
    MENU_EXIT = 2
    MENU_ID_DYNAMIC_START = 100     # 动态分配的 cmd id 从这里开始

    def __init__(self, parent, icon_path=None, tooltip='open-ai',
                 on_restore=None, on_exit=None, on_close_request=None,
                 main_hwnd=None, menu=None, event_queue=None):
        self.parent = parent
        self.icon_path = icon_path or ''
        self.tooltip = (tooltip or 'open-ai')[:127]
        self.on_restore = on_restore
        self.on_exit = on_exit
        self.on_close_request = on_close_request
        self.main_hwnd = main_hwnd
        # ---- 右键菜单 (声明式 MenuItem 列表; 可运行期增删改) ----
        self.menu = menu if menu is not None else self._default_menu()
        self._menu_id = self.MENU_ID_DYNAMIC_START
        self._menu_registry = {}       # cmd_id → MenuItem (每次弹出重建)
        self._menu_showing = False
        # ---- 回调通道: 优先线程安全队列 (主线程轮询消费), 回落 after ----
        # 跨线程调用 Tk (parent.after) 在部分 Tcl 线程配置下会抛异常,
        # 队列由 GUI 在主线程轮询消费, 是最可靠的方式。
        self.event_queue = event_queue

        self._u = _u32()
        self._s = _s32()
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._destroyed = False
        self._lock = threading.Lock()
        # 消息窗口线程私有状态
        self._hwnd = None            # 托盘消息窗口
        self._hicon = None
        self._added = False
        self._cls_name = None
        self._taskbar_msg = 0
        self._restore_msg = 0          # 跨进程恢复请求消息号
        # 主窗口子类化状态
        self._old_main_proc = None
        self._main_subclassed = False
        # 窗口过程回调 (必须持有引用防 GC)
        self._wndproc_ref = ctypes.WINFUNCTYPE(
            LRESULT_T, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(self._wnd_proc)

    # ---------------- 默认菜单 ----------------
    def _default_menu(self):
        """内置默认菜单: 显示主窗口 / 退出 (向后兼容 on_restore/on_exit)。"""
        return [
            MenuItem(label='显示主窗口', default=True,
                     on_click=self._cb(self.on_restore)),
            MenuItem(kind='separator'),
            MenuItem(label='退出', on_click=self._cb(self.on_exit)),
        ]

    @staticmethod
    def _cb(fn):
        """包一层调用 (菜单回调统一经 _post_to_tk 编组, 这里只做判空)。"""
        return fn

    # ---------------- 生命周期 ----------------
    def start(self):
        """启动托盘线程并添加图标 (异步, 立即返回)。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._destroyed = False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name='tray-icon')
            self._thread.start()
        return self

    def destroy(self):
        """移除托盘图标 + 恢复主窗口原窗口过程 (幂等)。"""
        with self._lock:
            if self._destroyed:
                return
            self._destroyed = True
        self._stop.set()
        self._ready.wait(timeout=2.0)      # 等消息窗口建好, 便于投递退出消息
        try:
            if self._hwnd and self._u.IsWindow(self._hwnd):
                self._u.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        try:
            if self._thread:
                self._thread.join(timeout=3.0)
        except Exception:
            pass
        self._unsubclass_main()

    def update_tooltip(self, text):
        """更新托盘悬浮提示 (线程安全, 可随时调用)。"""
        self.tooltip = (text or self.tooltip)[:127]
        try:
            if self._added and self._hwnd and self._u.IsWindow(self._hwnd):
                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = self._hwnd
                nid.uID = 1
                nid.uFlags = NIF_TIP | NIF_SHOWTIP
                nid.szTip = self.tooltip
                self._s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    # ---------------- 托盘线程 ----------------
    def _run(self):
        """托盘 UI 线程: 建消息窗口 → 加图标 → 消息循环 → 清理。"""
        try:
            self._create_msg_window()
            if self._hwnd:
                self._taskbar_msg = self._u.RegisterWindowMessageW(
                    'TaskbarCreated')
                self._restore_msg = restore_msg_id()
                self._add_icon()
        except Exception:
            pass
        self._ready.set()
        try:
            msg = _MSG()
            while not self._stop.is_set():
                r = self._u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:                       # WM_QUIT / 错误
                    break
                self._u.TranslateMessage(ctypes.byref(msg))
                self._u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self._cleanup()

    def _create_msg_window(self):
        """创建接收托盘消息的隐藏顶级窗口 (必须与消息循环同线程)。"""
        k = _k32()
        hinst = k.GetModuleHandleW(None)
        self._cls_name = 'openai_tray_%d' % os.getpid()
        cls = _WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        cls.lpfnWndProc = self._wndproc_ref
        cls.hInstance = hinst
        cls.lpszClassName = self._cls_name
        if not self._u.RegisterClassExW(ctypes.byref(cls)):
            return
        self._hwnd = self._u.CreateWindowExW(
            0, self._cls_name, 'open-ai tray', 0, 0, 0, 0, 0,
            None, None, hinst, None)
        if self._hwnd in (None, _INVALID):
            self._hwnd = None

    def _load_icon(self):
        """托盘图标: 项目 .ico 优先, 失败回落系统默认。"""
        try:
            h = self._u.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                   0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h in (None, _INVALID):
                raise OSError('LoadImageW: %d' % ctypes.get_last_error())
            return h
        except Exception:
            return self._u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _add_icon(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.szTip = self.tooltip
        nid.hIcon = self._load_icon()
        if self._s.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._added = True
            # 请求版本 4 回调语义 (必须 NIM_MODIFY 单独设置)
            nid2 = NOTIFYICONDATAW()
            nid2.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid2.hWnd = self._hwnd
            nid2.uID = 1
            nid2.uVersion = NOTIFYICON_VERSION_4
            self._s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid2))

    def _remove_icon(self):
        try:
            if self._added and self._hwnd:
                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = self._hwnd
                nid.uID = 1
                self._s.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        self._added = False

    def _readd_icon(self):
        """Explorer 重启后图标消失 → TaskbarCreated → 重新添加。"""
        self._remove_icon()
        self._add_icon()

    def _cleanup(self):
        self._remove_icon()
        try:
            if self._hwnd and self._u.IsWindow(self._hwnd):
                self._u.DestroyWindow(self._hwnd)   # 同线程销毁
        except Exception:
            pass
        self._hwnd = None

    # ---------------- 窗口过程 ----------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        """统一窗口过程: 按 hwnd 区分托盘消息窗口 / 被子类化的主窗口。"""
        try:
            if hwnd == self.main_hwnd:
                # 主窗口: 拦截 X (WM_CLOSE) → 回调, 其余全部走原过程
                if (msg == WM_CLOSE and self.on_close_request
                        and not self._destroyed):
                    self._post_to_tk(self.on_close_request)
                    return 0
                return self._u.CallWindowProcW(self._old_main_proc, hwnd,
                                               msg, wparam, lparam)
            return self._tray_wnd_proc(hwnd, msg, wparam, lparam)
        except Exception:
            # 兜底: 主窗口异常时务必交还原过程, 避免吞掉消息卡死窗口
            if hwnd == self.main_hwnd and self._old_main_proc:
                try:
                    return self._u.CallWindowProcW(self._old_main_proc, hwnd,
                                                   msg, wparam, lparam)
                except Exception:
                    pass
            return self._u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _tray_wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_CLOSE:
            self._u.PostQuitMessage(0)
            return 0
        if self._taskbar_msg and msg == self._taskbar_msg:
            self._readd_icon()
            return 0
        if self._restore_msg and msg == self._restore_msg:
            # 第二实例发来的恢复请求 (单实例互斥)
            self._post_to_tk(self.on_restore)
            return 0
        if msg == WM_TRAYICON:
            # uVersion=4: LOWORD(lParam)=事件码
            ev = lparam & 0xFFFF
            if ev in (WM_LBUTTONUP, NIN_SELECT, NIN_LUP, WM_LBUTTONDBLCLK):
                self._post_to_tk(self.on_restore)
                return 0
            if ev in (WM_RBUTTONUP, NIN_RUP, WM_CONTEXTMENU):
                self._show_context_menu(hwnd)
                return 0
        return self._u.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------- 右键菜单 (声明式配置, 可扩展) ----------------
    def _next_menu_id(self):
        """分配下一个动态菜单 cmd id (WM_COMMAND 可用范围, 2 字节)。"""
        mid = self._menu_id
        self._menu_id += 1
        if self._menu_id > 0xFFFF:
            self._menu_id = self.MENU_ID_DYNAMIC_START
        return mid

    def _build_menu(self, menu, items, depth=0):
        """递归把 MenuItem 列表写入 Win32 HMENU。

        返回 (本层写入条目数, {cmd_id: MenuItem} 注册表)。
        空子菜单自动降级为灰显普通项; 回调型 label/checked/... 以此
        每次右键的最新值渲染。默认项 (default=True, 仅顶层) 用
        SetMenuDefaultItem 实现 (加粗 + 双击托盘触发)。
        """
        u = self._u
        registry = {}
        count = 0
        default_pos = -1
        for it in items:
            if not it.is_visible():
                continue
            if it.kind == 'separator':
                u.AppendMenuW(menu, MF_SEPARATOR, 0, '')
                count += 1
                continue
            cmd = self._next_menu_id()
            flags = MF_STRING
            if it.is_checked():
                flags |= MF_CHECKED
            label = it.label() if callable(it.label) else it.label
            if it.submenu is not None:
                sub = u.CreatePopupMenu()
                sub_count, sub_reg = self._build_menu(sub, it.submenu,
                                                      depth + 1)
                registry.update(sub_reg)
                if sub_count:
                    u.AppendMenuW(menu, flags | MF_POPUP, int(sub), label)
                    count += 1
                    continue
                u.DestroyMenu(sub)           # 空子菜单 → 灰显普通项
                flags |= MF_GRAYED
            elif not it.is_enabled():
                flags |= MF_GRAYED
            if depth == 0 and it.default and default_pos < 0:
                default_pos = count          # 顶层首个 default 项
            u.AppendMenuW(menu, flags, cmd, label)
            registry[cmd] = it
            count += 1
        if default_pos >= 0:
            u.SetMenuDefaultItem(menu, default_pos, True)   # TRUE=按位置
        return count, registry

    def _dispatch_menu(self, cmd):
        """按 cmd id 找回 MenuItem, 把 on_click 编组回 Tk 主线程。"""
        it = self._menu_registry.get(cmd)
        if it is None or not it.is_enabled():
            return
        self._post_to_tk(it.on_click)

    def _show_context_menu(self, hwnd):
        """弹出右键菜单 (托盘 UI 线程内, TrackPopupMenuEx 模态)。"""
        u = self._u
        if self._menu_showing:
            return
        self._menu_showing = True
        menu = u.CreatePopupMenu()
        if not menu:
            self._menu_showing = False
            return
        try:
            self._menu_id = self.MENU_ID_DYNAMIC_START
            # _build_menu 返回的 registry 必须接住 (点击分发全靠它;
            # 之前丢弃在 _ 里导致所有菜单点击静默失效)
            count, registry = self._build_menu(menu, self.menu)
            self._menu_registry = registry
            if not count:
                return
            # TrackPopupMenuEx 前必须 SetForegroundWindow, 否则点别处菜单不消失
            u.SetForegroundWindow(hwnd)
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            cmd = u.TrackPopupMenuEx(
                menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_NONOTIFY |
                TPM_RETURNCMD, pt.x, pt.y, hwnd, None)
            # 消费掉 FK 队列里残留的激活消息 (MSDN 推荐, 防菜单复显); WM_NULL=0
            u.PostMessageW(hwnd, 0, 0, 0)
            if cmd:
                self._dispatch_menu(cmd)
        except Exception:
            # 菜单故障绝不能静默: 落盘日志 (否则窗口过程会吞掉异常)
            _log_err('context menu failed:\n' + traceback.format_exc())
        finally:
            self._menu_showing = False
            u.DestroyMenu(menu)

    # ---------------- 主窗口子类化 (默认停用) ----------------
    # GUI 的 X 按钮关闭统一走 Tk 原生 WM_DELETE_WINDOW 协议
    # (GUI._on_close, 在 Tk 主线程内触发, 天然线程安全可靠)。
    # 子类化路径保留给需要它的调用方, 但默认不再由 GUI 启用 ——
    # 历史教训: 子类化拦截在托盘线程执行, 其回调若跨线程调 Tk 失败,
    # 兜底分支会把 WM_CLOSE 放行给原窗口过程 → Tk 销毁窗口 → 进程退出
    # (即"点托盘恢复后, 再点 X 连托盘一起退出"的 bug 2 根因)。

    def subclass_main(self, hwnd):
        """对主窗口 (Tk 顶层) 子类化, 拦截 WM_CLOSE。返回成功与否。"""
        u = self._u
        try:
            if not hwnd or not u.IsWindow(hwnd):
                return False
            self.main_hwnd = hwnd
            self._old_main_proc = u._getwndproc(hwnd, GWL_WNDPROC)
            if not self._old_main_proc:
                return False
            u._setwndproc(hwnd, GWL_WNDPROC,
                          ctypes.cast(self._wndproc_ref,
                                      ctypes.c_void_p).value)
            self._main_subclassed = True
            return True
        except Exception:
            self._main_subclassed = False
            return False

    def _unsubclass_main(self):
        """恢复主窗口原窗口过程 (幂等; 窗口已销毁时只清状态)。"""
        try:
            if self._main_subclassed and self.main_hwnd:
                if self._u.IsWindow(self.main_hwnd) and self._old_main_proc:
                    self._u._setwndproc(self.main_hwnd, GWL_WNDPROC,
                                        self._old_main_proc)
                self._main_subclassed = False
        except Exception:
            pass
        self._old_main_proc = None

    # ---------------- 回调编组 ----------------
    def _post_to_tk(self, cb):
        """把回调派发回 Tk 主线程。

        有 event_queue 时放入队列 (GUI 主线程轮询消费 —— 线程安全,
        不直接碰 Tk); 否则回落 parent.after (尽力而为)。
        """
        if cb is None:
            return
        q = self.event_queue
        if q is not None:
            try:
                q.put(('tray', cb))
                return
            except Exception:
                pass
        try:
            self.parent.after(0, cb)
        except Exception:
            _log_err('post_to_tk fallback failed:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# 跨进程恢复请求 (GUI 单实例互斥时, 第二实例 → 已运行实例)
# ---------------------------------------------------------------------------
RESTORE_MSG_NAME = 'openai.gui.restore'   # RegisterWindowMessageW 字符串
RESTORE_WND_CLASS_PREFIX = 'openai_tray_'  # 托盘消息窗口类名前缀


def restore_msg_id():
    """跨进程恢复请求的消息号 (同会话内各进程取到同一值)。"""
    try:
        return _u32().RegisterWindowMessageW(RESTORE_MSG_NAME)
    except Exception:
        return 0


def request_running_instance_restore():
    """请求已运行的 GUI 实例恢复主窗口 (第二实例启动时调用)。

    通过窗口类名前缀枚举查找其他进程的托盘消息窗口, 向其投递
    RESTORE_MSG_NAME 消息; 已运行实例的托盘窗口过程收到后恢复主窗口。

    返回 True 表示已找到并投递 (存在已运行实例); False 表示没有实例在跑。
    """
    try:
        u = _u32()
        my_pid = os.getpid()
        msg = restore_msg_id()
        if not msg:
            return False
        found = [False]

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _enum_cb(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(64)
            if not u.GetClassNameW(hwnd, buf, 64):
                return True
            if not buf.value.startswith(RESTORE_WND_CLASS_PREFIX):
                return True
            pid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == my_pid:
                return True
            u.PostMessageW(hwnd, msg, 0, 0)
            found[0] = True
            return True

        u.EnumWindows(_enum_cb, 0)
        return found[0]
    except Exception:
        return False


def drain_restore_messages(msg_id):
    """排空当前线程消息队列里的恢复请求, 返回是否收到 (窗口未创建时轮询用)。"""
    try:
        u = _u32()
        m = _MSG()
        got = False
        while u.PeekMessageW(ctypes.byref(m), None, msg_id, msg_id, 0x0001):
            got = True
        return got
    except Exception:
        return False
