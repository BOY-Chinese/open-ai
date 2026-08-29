#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 账号管理 - 图形界面版 (Tkinter)
=========================================
页面1「已添加账号」:
  - TRAE 通道 / WorkBuddy 通道 上下分开显示, 每个列表可上下滑动 + 左右滑动
  - 底部 4 个功能按键:
      [1] 刷新积分      — 重新查询 TRAE + WorkBuddy 全部账号积分
      [2] 添加 TRAE 账号 — 打开网页登录, 自动抓 token 入池 (login_trae.py)
      [3] 添加 WorkBuddy 账号 — 打开网页登录, 自动抓 token 入池 (login_workbuddy.py)
      [4] 重新连接      — 验证全部 provider token 是否可用
页面2「API 管理」:
  - 顶部展示网关地址 (OpenAI / Anthropic 兼容端点, 按 config.json 的 host/port 生成),
    每行带「⧉ 复制」按钮; 另有「⧉ 复制地址+密钥」把地址和选中密钥一并复制
  - API 列表: 创建 / 命名 / 复制 / 删除 API 密钥
页面3「模型列表」:
  - 右键模型行: 复制模型名称 / 复制上游模型 id / 固定到顶部(取消固定) / 隐藏(取消隐藏)
  - 固定与隐藏互相独立, 持久化于 data/model_view_state.json (key: TRAE=config_name,
    WorkBuddy=model_id); 上游更新后新模型正常进入列表, 已消失的 key 自动惰性清理
  - 底部「显示已隐藏模型」勾选框: hidden>0 显示计数; 勾选后隐藏行灰显可恢复
页面4「操作日志」: 点击该页签才显示操作日志
"""
# ---- 必须在 import tkinter 之前设置 AppUserModelID ----
# 否则 Microsoft Store 版 Python 的 pythonw.exe 任务栏会显示 Python 默认图标
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('openai.account-manager')
except Exception:
    pass

import json
import os
import subprocess
import sys
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# ---- 复用控制台版账号管理器的积分查询逻辑 ----
import account_manager as am
import api_store

BASE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = am.OPENAI_CFG
VENV_PY = am.OPENAI_VENV_PY if os.path.isfile(am.OPENAI_VENV_PY) else sys.executable

# ---- 模型列表视图状态 (固定/隐藏) 持久化 ----
# 存储文件: data/model_view_state.json, 结构 {"pinned": {"trae": [key..], "workbuddy": [key..]},
#          "hidden": {"trae": [key..], "workbuddy": [key..]}}
# key 为模型唯一标识: TRAE=config_name, WorkBuddy=model_id。
# 固定与隐藏互相独立; 上游更新后仍存在的 key 自动生效, 已消失的 key 惰性清理。
MODEL_STATE_PATH = os.path.join(os.path.dirname(BASE), 'data', 'model_view_state.json')
MODEL_PROVIDERS = ('trae', 'workbuddy')


def load_model_state():
    """读取模型视图状态, 返回 {'pinned': {prov: set()}, 'hidden': {prov: set()}}。"""
    st = {'pinned': {p: set() for p in MODEL_PROVIDERS},
          'hidden': {p: set() for p in MODEL_PROVIDERS}}
    try:
        with open(MODEL_STATE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        for grp in ('pinned', 'hidden'):
            for p in MODEL_PROVIDERS:
                v = (data or {}).get(grp, {}).get(p) or []
                st[grp][p] = set(str(x) for x in v)
    except Exception:
        pass
    return st


def save_model_state(st):
    """持久化模型视图状态 (列表化, 失败仅写日志不影响界面)。"""
    try:
        os.makedirs(os.path.dirname(MODEL_STATE_PATH), exist_ok=True)
        data = {grp: {p: sorted(st[grp].get(p) or set()) for p in MODEL_PROVIDERS}
                for grp in ('pinned', 'hidden')}
        with open(MODEL_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[model_state] 保存失败: {e}')


class Toast:
    """轻量 toast 提示: 无边框小窗, 短暂显示后自动淡出 (非模态)。"""

    def __init__(self, root, text, ms=1400):
        self.root = root
        self.sw = root.winfo_screenwidth()
        self.sh = root.winfo_screenheight()
        self.toplevel = None
        self._after_id = None
        self._show(text, ms)

    def _show(self, text, ms):
        import tkinter.font as tkfont
        try:
            f = tkfont.Font(family='Microsoft YaHei UI', size=10)
            w = min(max(int(f.measure(text)) + 36, 120), 420)
        except Exception:
            w = 200
        h = 34
        x = self.sw // 2 - w // 2
        y = max(self.sh - 140, 40)  # 屏幕右下角上方
        tl = tk.Toplevel(self.root)
        self.toplevel = tl
        tl.overrideredirect(True)
        try:
            tl.attributes('-topmost', True)
        except Exception:
            pass
        try:
            tl.attributes('-alpha', 0.96)
        except Exception:
            pass
        tl.geometry(f'{w}x{h}+{x}+{y}')
        frm = tk.Frame(tl, bg='#323232', bd=0, highlightthickness=1,
                       highlightbackground='#323232')
        frm.pack(fill='both', expand=True)
        tk.Label(frm, text=text, bg='#323232', fg='#ffffff',
                 font=('Microsoft YaHei UI', 10)).pack(expand=True)
        tl.bind('<Button-1>', lambda e: self.close())
        self._after_id = tl.after(ms, self.close)

    def close(self):
        tl = self.toplevel
        if tl is None:
            return
        self.toplevel = None
        try:
            if self._after_id is not None:
                tl.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            tl.destroy()
        except Exception:
            pass


# ================= 账号数据获取 =================

def load_account_groups():
    """读取 config.json, 返回 (trae_info, wb_info)。trae_info / wb_info 为
    (trae_dict, accounts, device_id, invalid_map) 与 (wb_dict, accounts, domain, product),
    无账号时相关 accounts 为空列表。同时返回错误信息(无则 None)。"""
    try:
        with open(OPENAI_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        return None, None, f'config.json 读取失败: {e}'

    prov = cfg.get('providers') or {}

    trae = prov.get('trae') or {}
    device_id = trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id', '')
    try:
        invalid_map = am.server_invalid_map()
    except Exception:
        invalid_map = {}
    trae_info = (trae, trae.get('accounts') or [], device_id, invalid_map)

    wb = prov.get('workbuddy') or {}
    wb_info = (wb, wb.get('accounts') or [], wb.get('domain', 'www.workbuddy.cn'),
               wb.get('product', 'SaaS'))

    if not trae_info[1] and not wb_info[1]:
        return trae_info, wb_info, '尚未添加任何账号'
    return trae_info, wb_info, None


def trae_row_name(trae, a, invalid_map, i):
    name = a.get('name') or a.get('uid') or f'#{i}'
    uid = a.get('uid', '')
    tag = ' [失效]' if invalid_map.get(uid) else ''
    return f'{name}{tag}'


def wb_row_name(wb, a, i):
    uid = a.get('userId', '')
    return a.get('name') or uid or f'#{i}'


def trae_row_detail(a, device_id):
    g, w, err = am.trae_credits(a, device_id)
    if err:
        return f'积分查询失败: {err}'
    return f'积分 {g + w:.2f}（通用 {g:.2f} + Work {w:.2f}）'


def wb_row_detail(a, domain, product):
    total, err = am.wb_credits(a, domain, product)
    if err:
        return f'积分查询失败: {err}'
    return f'积分 {total:.2f}'


# ================= GUI =================

class AccountManagerApp:
    def __init__(self, root):
        self.root = root
        root.title('open-ai 账号管理')
        root.geometry('760x620')
        root.minsize(600, 480)
        # 设置窗口图标 (任务栏/标题栏)
        self._set_icon(root)

        self.busy = False
        self.result_q = queue.Queue()
        # 模型列表视图状态 (固定/隐藏), 持久化于 data/model_view_state.json
        self.model_state = load_model_state()
        self._model_cache = {'trae': None, 'workbuddy': None}  # 最近一次成功拉取的模型行
        self._model_errs = {'trae': None, 'workbuddy': None}
        self._model_rerender_job = None  # 勾选框触发的重渲染防抖
        root.protocol('WM_DELETE_WINDOW', self._on_close)

        self._build_ui()
        self._poll_result_q()
        self.refresh_account_list()
        # 启动后自动拉取一次积分/状态
        self.root.after(300, self.on_refresh)
        # 启动后自动加载模型列表(含积分倍率), 独立线程不锁界面
        self.root.after(800, self.on_model_refresh)
        # 回读开机自启状态
        self._refresh_autostart_state()

    def _on_close(self):
        """窗口关闭: 保存模型视图状态后退出。"""
        save_model_state(self.model_state)
        self.root.destroy()

    def _set_icon(self, root):
        """设置窗口/任务栏图标为软件 logo (多层兜底)。

        Store 版 Python 的 pythonw.exe 图标资源是 Python 默认图标, 任务栏对
        "无快捷方式的裸进程" 会回落用 exe 图标 —— 仅 WM_SETICON (窗口图标)
        不够, 还要设窗口类图标 (SetClassLongPtrW) 并给窗口本身显式绑定
        AppUserModelID (SHGetPropertyStoreForWindow + SetValue), Explorer
        才会改用窗口自己的图标渲染任务栏按钮。
        """
        ico_path = os.path.join(os.path.dirname(BASE), 'pic', 'open-ai.ico')
        if not os.path.exists(ico_path):
            return
        try:
            root.iconbitmap(default=ico_path)
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            # 明确 64/32 位下指针参数类型, 避免 SetClassLongPtrW 截断
            user32.SetClassLongPtrW.restype = ctypes.c_void_p
            user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                ctypes.c_void_p]
            shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT
            shell32.SHGetPropertyStoreForWindow.argtypes = [
                wintypes.HWND, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p)]

            WM_SETICON = 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            GCLP_HICON, GCLP_HICONSM = -14, -34
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            root.update_idletasks()
            # tkinter 顶层窗口 HWND = GetParent(root.winfo_id())
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()

            # 1) 给顶层窗口显式绑定 AppUserModelID (任务栏按窗口 AUMID 归组)
            #    无快捷方式时 Explorer 回落用窗口自身图标而非 exe 图标
            try:
                class _GUID(ctypes.Structure):
                    _fields_ = [('Data1', ctypes.c_ulong),
                                ('Data2', ctypes.c_ushort),
                                ('Data3', ctypes.c_ushort),
                                ('Data4', ctypes.c_ubyte * 8)]
                # IID_IPropertyStore = {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}
                iid = _GUID()
                iid.Data1 = 0x886D8EEB
                iid.Data2 = 0x8CF2
                iid.Data3 = 0x4446
                iid.Data4 = (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA,
                                                 0x1D, 0xBD, 0xCF, 0x99)
                ppv = ctypes.c_void_p()
                if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid),
                                                       ctypes.byref(ppv)) == 0 and ppv:
                    # IPropertyStore 手工 vtable 调用: 只需 SetValue + Commit
                    # PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
                    class _PROPERTYKEY(ctypes.Structure):
                        _fields_ = [('fmtid', _GUID), ('pid', ctypes.c_ulong)]
                    key = _PROPERTYKEY()
                    key.fmtid.Data1 = 0x9F4C2855
                    key.fmtid.Data2 = 0x9F79
                    key.fmtid.Data3 = 0x4B39
                    key.fmtid.Data4 = (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1,
                                                           0xD4, 0x2D, 0xE1,
                                                           0xD5, 0xF3)
                    key.pid = 5
                    # PROPVARIANT (VT_LPWSTR)
                    class _PROPVARIANT(ctypes.Structure):
                        class U(ctypes.Union):
                            _fields_ = [('pwszVal', ctypes.c_wchar_p),
                                        ('padding', ctypes.c_ubyte * 16)]
                        _anonymous_ = ('u',)
                        _fields_ = [('vt', ctypes.c_ushort),
                                    ('wReserved1', ctypes.c_ushort),
                                    ('wReserved2', ctypes.c_ushort),
                                    ('wReserved3', ctypes.c_ushort),
                                    ('u', U)]
                    pv = _PROPVARIANT()
                    pv.vt = 31  # VT_LPWSTR
                    pv.pwszVal = ctypes.c_wchar_p('openai.account-manager')
                    # vtbl: 对 **ppv 取下标 → POINTER(c_void_p), 其内容即函数指针数组
                    vtbl = ctypes.cast(
                        ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p)).contents,
                        ctypes.POINTER(ctypes.c_void_p))
                    # IUnknown: QueryInterface(0), AddRef(1), Release(2)
                    # IPropertyStore: GetCount(3), GetAt(4), GetValue(5),
                    #                 SetValue(6), Commit(7)
                    setvalue = ctypes.WINFUNCTYPE(
                        ctypes.HRESULT, ctypes.c_void_p, _PROPERTYKEY,
                        _PROPVARIANT)(vtbl[6])
                    commit = ctypes.WINFUNCTYPE(ctypes.HRESULT,
                                                ctypes.c_void_p)(vtbl[7])
                    release = ctypes.WINFUNCTYPE(ctypes.HRESULT,
                                                 ctypes.c_void_p)(vtbl[2])
                    setvalue(ppv.value, key, pv)
                    commit(ppv.value)
                    release(ppv.value)
            except Exception:
                pass

            # 2) 窗口图标 (标题栏 + Alt-Tab)
            for size in (ICON_SMALL, ICON_BIG):
                dim = 16 if size == ICON_SMALL else 32
                hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, dim, dim,
                                          LR_LOADFROMFILE)
                if hicon:
                    user32.SendMessageW(hwnd, WM_SETICON, size, hicon)

            # 3) 窗口类图标: 任务栏回落渲染用 (pythonw.exe 类默认继承 exe 图标)
            hicon32 = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32,
                                        LR_LOADFROMFILE)
            hicon16 = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16,
                                        LR_LOADFROMFILE)
            try:
                if hicon32:
                    user32.SetClassLongPtrW(hwnd, GCLP_HICON, hicon32)
                if hicon16:
                    user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon16)
            except Exception:
                pass
        except Exception:
            pass

    # ---------- 界面搭建 ----------
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        # 标题
        ttk.Label(self.root, text='账号管理', font=('Microsoft YaHei UI', 14, 'bold')) \
            .pack(pady=(10, 2))

        # 表格样式
        self._setup_table_style()

        # 三个页签: 已添加账号 / API 管理 / 操作日志
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, **pad)

        # ---- 页面1: 已添加账号 ----
        page_acct = ttk.Frame(self.notebook)
        self.notebook.add(page_acct, text='已添加账号')

        # TRAE 通道 (上)
        self.trae_frame = ttk.LabelFrame(page_acct, text='TRAE 通道')
        self.trae_frame.pack(fill='both', expand=True, **pad)
        self.tree_trae = self._make_tree(self.trae_frame)
        self.tree_trae.bind('<ButtonRelease-1>', self._on_account_click)
        # WorkBuddy 通道 (下)
        self.wb_frame = ttk.LabelFrame(page_acct, text='WorkBuddy 通道')
        self.wb_frame.pack(fill='both', expand=True, **pad)
        self.tree_wb = self._make_tree(self.wb_frame)
        self.tree_wb.bind('<ButtonRelease-1>', self._on_account_click)

        # 按钮区 —— 4 个功能按键
        btn_frame = ttk.Frame(page_acct)
        btn_frame.pack(fill='x', **pad)
        self.btn_refresh = ttk.Button(btn_frame, text='① 刷新积分', command=self.on_refresh)
        self.btn_add_trae = ttk.Button(btn_frame, text='② 添加 TRAE 账号', command=self.on_add_trae)
        self.btn_add_wb = ttk.Button(btn_frame, text='③ 添加 WorkBuddy 账号', command=self.on_add_wb)
        self.btn_reconnect = ttk.Button(btn_frame, text='④ 重新连接', command=self.on_reconnect)
        for b in (self.btn_refresh, self.btn_add_trae, self.btn_add_wb, self.btn_reconnect):
            b.pack(side='left', fill='x', expand=True, padx=3)

        # ---- 页面2: API 管理 ----
        page_api = ttk.Frame(self.notebook)
        self.notebook.add(page_api, text='API 管理')

        api_pad = {'padx': 6, 'pady': 3}

        # 网关地址区: 展示 OpenAI / Anthropic 兼容端点, 一键复制, 方便接入客户端
        gw_frame = ttk.LabelFrame(page_api, text='网关地址（接口接入信息）')
        gw_frame.pack(fill='x', **api_pad)
        self.gw_url_vars = []
        for i, (label, url) in enumerate(self._gateway_urls()):
            row = ttk.Frame(gw_frame)
            row.pack(fill='x', padx=8, pady=2)
            ttk.Label(row, text=label, width=22, anchor='w').pack(side='left')
            var = tk.StringVar(value=url)
            self.gw_url_vars.append(var)
            ent = ttk.Entry(row, textvariable=var, state='readonly')
            ent.pack(side='left', fill='x', expand=True, padx=(0, 6))
            ttk.Button(row, text='⧉ 复制',
                       command=lambda u=url, l=label: self._copy_gateway_url(u, l)) \
                .pack(side='left')
        self.btn_gw_copy_all = ttk.Button(gw_frame, text='⧉ 复制地址+密钥',
                                          command=self._copy_gateway_with_key)
        self.btn_gw_copy_all.pack(anchor='e', padx=8, pady=(0, 2))
        ttk.Label(gw_frame,
                  text='Claude Code / CC Switch 把 ANTHROPIC_BASE_URL 指向 Anthropic 兼容地址，'
                       'OpenAI 客户端把 Base URL 指向 OpenAI 兼容地址即可。',
                  foreground='#666666', wraplength=680, justify='left') \
            .pack(anchor='w', padx=8, pady=(0, 6))

        # API 表格 (可滚动)
        api_frame = ttk.LabelFrame(page_api, text='API 列表')
        api_frame.pack(fill='both', expand=True, **api_pad)
        self.api_tree = self._make_api_tree(api_frame)

        # API 操作按钮区
        api_btn = ttk.Frame(page_api)
        api_btn.pack(fill='x', **api_pad)
        self.btn_api_refresh = ttk.Button(api_btn, text='↻ 刷新', command=self.refresh_api_list)
        self.btn_api_create = ttk.Button(api_btn, text='＋ 创建 API', command=self.on_api_create)
        self.btn_api_rename = ttk.Button(api_btn, text='✎ 命名/改名', command=self.on_api_rename)
        self.btn_api_copy = ttk.Button(api_btn, text='⧉ 复制密钥', command=self.on_api_copy)
        self.btn_api_delete = ttk.Button(api_btn, text='🗑 删除', command=self.on_api_delete)
        for b in (self.btn_api_refresh, self.btn_api_create, self.btn_api_rename,
                  self.btn_api_copy, self.btn_api_delete):
            b.pack(side='left', fill='x', expand=True, padx=3)

        # ---- 页面3: 模型列表（含积分消耗倍率）----
        page_model = ttk.Frame(self.notebook)
        self.notebook.add(page_model, text='模型列表')

        # TRAE 模型 (上)
        self.trae_model_frame = ttk.LabelFrame(page_model, text='TRAE 模型')
        self.trae_model_frame.pack(fill='both', expand=True, **pad)
        self.model_tree_trae = self._make_model_tree(self.trae_model_frame)
        self.model_tree_trae.bind('<Button-3>', self._on_model_menu_trae)

        # WorkBuddy 模型 (下)
        self.wb_model_frame = ttk.LabelFrame(page_model, text='WorkBuddy 模型')
        self.wb_model_frame.pack(fill='both', expand=True, **pad)
        self.model_tree_wb = self._make_model_tree(self.wb_model_frame)
        self.model_tree_wb.bind('<Button-3>', self._on_model_menu_workbuddy)

        # 刷新按钮 + 显示未知倍率勾选框 + 显示已隐藏勾选框
        model_btn = ttk.Frame(page_model)
        model_btn.pack(fill='x', **pad)
        self.btn_model_refresh = ttk.Button(model_btn, text='刷新模型列表',
                                            command=self.on_model_refresh)
        self.btn_model_refresh.pack(side='left', fill='x', expand=True, padx=3)
        self.show_unknown_rate_var = tk.BooleanVar(value=False)
        self.chk_show_unknown = tk.Checkbutton(
            model_btn, text='显示未知倍率模型', variable=self.show_unknown_rate_var,
            command=self.on_model_refresh, anchor='w',
            bg='#f0f0f0', activebackground='#f0f0f0')
        self.chk_show_unknown.pack(side='right', padx=5)
        # 显示已隐藏模型: 勾选后隐藏行灰显展示, 供右键取消隐藏恢复 (不删除任何数据)
        self.show_hidden_var = tk.BooleanVar(value=False)
        self.chk_show_hidden = tk.Checkbutton(
            model_btn, text='显示已隐藏模型', variable=self.show_hidden_var,
            command=self.on_show_hidden_toggle, anchor='w',
            bg='#f0f0f0', activebackground='#f0f0f0')
        self.chk_show_hidden.pack(side='right', padx=5)

        # ---- 页面4: 设置 ----
        page_set = ttk.Frame(self.notebook)
        self.notebook.add(page_set, text='设置')

        self._build_settings_page(page_set, pad)

        # ---- 页面4: 操作日志 ----
        page_log = ttk.Frame(self.notebook)
        self.notebook.add(page_log, text='操作日志')
        self.log = scrolledtext.ScrolledText(page_log, wrap='word',
                                             state='disabled', font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, **pad)
        self.refresh_api_list()

    # ---------- 设置页 ----------
    def _build_settings_page(self, page, pad):
        # 开机自动运行 (自启开关)
        self.autostart_var = tk.BooleanVar(value=False)
        ast_frame = ttk.LabelFrame(page, text='启动')
        ast_frame.pack(fill='x', **pad)

        # 自启勾选框 (选中时方框内显示绿色钩子)
        self._setup_autostart_style()
        self.chk_autostart = tk.Checkbutton(
            ast_frame, text='开机自动运行（未开启）', variable=self.autostart_var,
            command=self._on_autostart_toggle, anchor='w',
            font=('Microsoft YaHei UI', 11, 'bold'),
            bg='#ffffff', activebackground='#ffffff', bd=0, highlightthickness=0,
            **self._autostart_chk_colors(False))
        self.chk_autostart.pack(anchor='w', padx=10, pady=6)
        ttk.Label(ast_frame,
                  text='勾选后: 每次登录 Windows 自动启动网关和守护进程(无需人工打开)。',
                  foreground='#666666').pack(anchor='w', padx=20, pady=(0, 6))

        # 卸载区
        uni_frame = ttk.LabelFrame(page, text='卸载')
        uni_frame.pack(fill='x', **pad)
        ttk.Label(uni_frame,
                  text='一键卸载将彻底删除 open-ai（含插件文件、配置、账号与 API 密钥）。',
                  foreground='#666666').pack(anchor='w', padx=10, pady=6)
        # 红色卸载按钮
        self.btn_uninstall = self._make_red_button(uni_frame, '一键卸载',
                                                   self._on_uninstall)
        self.btn_uninstall.pack(anchor='w', padx=10, pady=(0, 10))

        # 版本与更新区
        ver_frame = ttk.LabelFrame(page, text='版本')
        ver_frame.pack(fill='x', **pad)
        ver_row = ttk.Frame(ver_frame)
        ver_row.pack(fill='x', padx=10, pady=(6, 2))
        ttk.Label(ver_row, text=f'当前版本: {self._current_version()}',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        self.btn_update = ttk.Button(ver_row, text='一键更新',
                                     command=self.on_check_update)
        self.btn_update.pack(side='right')
        ttk.Label(ver_frame,
                  text='检查并更新到 GitHub 最新发布版本 (github.com/BOY-Chinese/open-ai/releases)。',
                  foreground='#666666').pack(anchor='w', padx=10, pady=(0, 8))

    # ---------- 版本与一键更新 ----------
    UPDATE_REPO_API = 'https://api.github.com/repos/BOY-Chinese/open-ai/releases/latest'
    UPDATE_ASSET_KEYWORD = 'installer'  # release 资产名关键字 (open-ai-installer.exe)

    @staticmethod
    def _current_version():
        """当前版本号 (根目录 version.py 的 APP_VERSION, 读取失败回退未知)。
        GUI 以 scripts/gui_account_manager.py 启动时根目录不在 sys.path, 需按路径加载。"""
        try:
            from version import APP_VERSION
            return APP_VERSION
        except Exception:
            pass
        try:
            import importlib.util
            p = os.path.join(os.path.dirname(BASE), 'version.py')
            spec = importlib.util.spec_from_file_location('openai_version', p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m.APP_VERSION
        except Exception:
            return '(未知)'

    @staticmethod
    def _version_tuple(v):
        """'v2.3.1' -> (2, 3, 1), 便于比较。"""
        import re
        nums = re.findall(r'\d+', str(v or ''))
        return tuple(int(n) for n in nums) if nums else (0,)

    def on_check_update(self):
        """一键更新入口: 后台线程查最新 release, 主线程弹窗确认后下载安装。"""
        if getattr(self, '_updating', False):
            return
        self._updating = True
        self.btn_update.configure(state='disabled', text='检查中…')

        def work():
            import urllib.request
            try:
                req = urllib.request.Request(
                    self.UPDATE_REPO_API,
                    headers={'User-Agent': 'open-ai-updater',
                             'Accept': 'application/vnd.github+json'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    rel = json.load(resp)
                tag = rel.get('tag_name') or ''
                asset_url, asset_name = '', ''
                for a in rel.get('assets') or []:
                    if self.UPDATE_ASSET_KEYWORD in (a.get('name') or '').lower():
                        asset_url = a.get('browser_download_url') or ''
                        asset_name = a.get('name') or ''
                        break
                self.root.after(0, lambda: self._after_check_update(
                    tag, asset_url, asset_name, None))
            except Exception as e:
                err = f'{type(e).__name__}: {e}'
                self.root.after(0, lambda: self._after_check_update(
                    '', '', '', err))

        threading.Thread(target=work, daemon=True).start()

    def _after_check_update(self, tag, asset_url, asset_name, err):
        """检查完成 (主线程): 比较版本并确认。"""
        self.btn_update.configure(state='normal', text='一键更新')
        self._updating = False
        if err:
            self._write_log(f'[更新] 检查失败: {err}')
            # 检查阶段失败多为网络不通, 按网络错误统一提示
            messagebox.showerror('一键更新', '网络环境错误，无法下载！')
            return
        self._write_log(f'[更新] 最新 release: {tag or "(未获取到)"}')
        cur = self._current_version()
        if not tag:
            messagebox.showerror('一键更新', '网络环境错误，无法下载！')
            return
        if self._version_tuple(tag) <= self._version_tuple(cur):
            messagebox.showinfo('一键更新',
                                f'已是最新版本。\n\n当前版本: {cur}\n线上版本: {tag}')
            return
        if not messagebox.askyesno(
                '一键更新',
                f'发现新版本 {tag}（当前 {cur}）。\n\n'
                f'将下载「{asset_name or "安装包"}」并启动安装程序。\n'
                f'安装程序会保留当前目录的 config.json（账号/密钥不受影响）。\n\n是否继续？'):
            self._write_log('[更新] 用户取消更新')
            return
        self._start_download(tag, asset_url)

    def _start_download(self, tag, asset_url):
        """下载安装包 (后台线程), 进度写日志, 完成后主线程启动安装。"""
        self.btn_update.configure(state='normal', text='下载中…')
        self._updating = True

        def work():
            import urllib.request
            import tempfile
            ok, dest, err = False, '', ''
            if not asset_url:
                err = 'release 未找到安装包资产'
            else:
                try:
                    tmpdir = tempfile.mkdtemp(prefix='openai_update_')
                    dest = os.path.join(tmpdir, asset_url.rsplit('/', 1)[-1] or
                                        'open-ai-installer.exe')
                    req = urllib.request.Request(
                        asset_url, headers={'User-Agent': 'open-ai-updater'})
                    self.root.after(0, lambda: self._write_log(
                        f'[更新] 开始下载: {asset_url}'))
                    downloaded = [0]

                    def report(n):
                        mb = n / 1048576
                        if mb - downloaded[0] >= 2 or n == 0:  # 每 2MB 记一次
                            downloaded[0] = mb
                            self.root.after(0, lambda m=mb: self._write_log(
                                f'[更新] 已下载 {m:.1f} MB …'))

                    with urllib.request.urlopen(req, timeout=60) as resp, \
                            open(dest, 'wb') as f:
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            report(f.tell())
                    size = os.path.getsize(dest)
                    if size <= 0:
                        err = '下载内容为空'
                        try:
                            os.remove(dest)
                        except Exception:
                            pass
                    else:
                        ok = True
                except Exception as e:
                    err = f'{type(e).__name__}: {e}'
                    try:
                        if os.path.isfile(dest):
                            os.remove(dest)
                    except Exception:
                        pass
            self.root.after(0, lambda: self._after_download(
                ok, dest, tag, err))

        threading.Thread(target=work, daemon=True).start()

    def _after_download(self, ok, dest, tag, err):
        """下载完成 (主线程): 启动安装程序或报网络错误。"""
        self.btn_update.configure(state='normal', text='一键更新')
        self._updating = False
        if not ok:
            self._write_log(f'[更新] 下载失败: {err}')
            # 下载阶段任何失败 (网络中断/超时/资产缺失) 都按网络错误提示
            messagebox.showerror('一键更新', '网络环境错误，无法下载！')
            return
        self._write_log(f'[更新] 下载完成: {dest} ({os.path.getsize(dest)} bytes), 启动安装程序…')
        try:
            import subprocess as sp
            # --dir 传入当前安装目录: 安装器预填路径, 且保留该目录已有 config.json
            sp.Popen([dest, '--dir', os.path.dirname(BASE)],
                     cwd=os.path.dirname(dest),
                     creationflags=getattr(sp, 'CREATE_NO_WINDOW', 0))
            messagebox.showinfo(
                '一键更新',
                f'安装包 {tag} 已下载并启动安装程序。\n\n'
                f'按安装向导完成后, 重新打开「账号管理」即可。')
            self._write_log('[更新] 安装程序已启动')
        except Exception as e:
            self._write_log(f'[更新] 安装程序启动失败: {e}')
            messagebox.showerror('一键更新',
                                 f'安装程序启动失败:\n{e}')

    def _autostart_chk_colors(self, checked):
        """返回选中/未选中时钩子的颜色配置 (钩子画在方框内)。
        tk.Checkbutton: selectcolor=方框底色, fg=方框内钩子颜色。"""
        if checked:
            return {'fg': '#2e7d32', 'activeforeground': '#2e7d32',
                    'selectcolor': '#ffffff'}
        return {'fg': '#333333', 'activeforeground': '#333333',
                'selectcolor': '#ffffff'}

    def _setup_autostart_style(self):
        """自启勾选框样式: 选中时方框内显示绿色钩子。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('Autostart.TCheckbutton',
                        font=('Microsoft YaHei UI', 11, 'bold'),
                        background='#ffffff')
        style.map('Autostart.TCheckbutton',
                  background=[('active', '#ffffff')],
                  indicatorcolor=[('selected', '#2e7d32'),
                                  ('!selected', '#ffffff')],
                  foreground=[('selected', '#2e7d32'),
                              ('!selected', '#333333')])

    def _make_red_button(self, master, text, command):
        """创建红色样式的按钮。"""
        style = ttk.Style(self.root)
        try:
            style.configure('Danger.TButton',
                            background='#d32f2f', foreground='white',
                            font=('Microsoft YaHei UI', 11, 'bold'),
                            padding=(14, 8), borderwidth=0, relief='flat')
            style.map('Danger.TButton',
                      background=[('active', '#b71c1c'), ('pressed', '#9a1b1b')],
                      foreground=[('active', 'white'), ('pressed', 'white')])
        except Exception:
            pass
        return ttk.Button(master, text=text, command=command, style='Danger.TButton')

    # ---------- 设置操作 ----------
    def _run_ps(self, script, args):
        """后台运行 PowerShell 脚本并实时输出, 返回 (成功否)。"""
        import subprocess as sp
        py_dir = BASE
        cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', os.path.join(os.path.dirname(BASE), script)] + args
        self._write_log(f'[设置] 执行: {script} {args}')
        try:
            p = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.STDOUT,
                         text=True, encoding='utf-8', errors='replace',
                         creationflags=getattr(sp, 'CREATE_NO_WINDOW', 0))
            for line in p.stdout:
                self.result_q.put(line.rstrip())
            p.wait()
            return p.returncode == 0
        except Exception as e:
            self.result_q.put(f'[设置] 执行失败: {e}')
            return False

    def _on_autostart_toggle(self):
        """自启勾选框变化: 同步到开机自启, 并更新文字与颜色。"""
        want_on = bool(self.autostart_var.get())

        def work():
            ok, msg = self._set_autostart(want_on)
            # 回读最终状态 (直接查文件, 不依赖 PowerShell 输出)
            final = self._read_autostart_status()
            self.result_q.put(f'[设置] 开机自启 = {"已开启" if final else "已关闭"}（{msg}）')
            self.root.after(0, lambda: self._set_autostart_display(final))

        # 触发后台执行, 不锁界面
        self.busy = True
        self.root.after(0, lambda: self._set_busy(False))  # 自启切换不锁按钮
        threading.Thread(target=self._thread_wrap_simple, args=(work,), daemon=True).start()

    def _set_autostart_display(self, status):
        """设置自启勾选框的勾选状态, 文字与颜色 (钩子画在方框内)。"""
        self.autostart_var.set(status)
        if status:
            txt = '开机自动运行（已开启）'
        else:
            txt = '开机自动运行（未开启）'
        self.chk_autostart.configure(text=txt,
                                     **self._autostart_chk_colors(status))

    def _thread_wrap_simple(self, fn):
        try:
            fn()
        except Exception as e:
            self.result_q.put(f'[错误] {type(e).__name__}: {e}')

    # ---------- 开机自启: 纯 Python 实现 (不依赖 PowerShell 输出, 避免编码问题) ----------
    def _startup_dir(self):
        """当前用户启动文件夹路径。"""
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            return os.path.join(appdata, 'Microsoft', 'Windows',
                                'Start Menu', 'Programs', 'Startup')
        return None

    def _autostart_src(self):
        return os.path.join(os.path.dirname(BASE), 'open-ai-autostart.bat')

    def _autostart_dst(self):
        sd = self._startup_dir()
        return os.path.join(sd, 'open-ai-autostart.bat') if sd else None

    def _read_autostart_status(self):
        """直接检查启动文件夹里是否存在自启文件。"""
        dst = self._autostart_dst()
        return bool(dst) and os.path.isfile(dst)

    def _set_autostart(self, enable):
        """启用/关闭开机自启。返回 (成功否, 消息)。"""
        import shutil
        src = self._autostart_src()
        dst = self._autostart_dst()
        if not dst:
            return False, '无法定位启动文件夹'
        try:
            if enable:
                if not os.path.isfile(src):
                    return False, '找不到 open-ai-autostart.bat'
                shutil.copy2(src, dst)
                return True, '已写入启动文件夹'
            else:
                if os.path.isfile(dst):
                    os.remove(dst)
                return True, '已从启动文件夹移除'
        except Exception as e:
            return False, str(e)

    def _refresh_autostart_state(self):
        """启动时回读自启状态填充勾选框。"""
        def work():
            status = self._read_autostart_status()
            self.root.after(0, lambda: self._set_autostart_display(status))
        threading.Thread(target=work, daemon=True).start()

    def _on_uninstall(self):
        """一键卸载 (彻底删除): 调用独立的 uninstall.exe。"""
        if not messagebox.askyesno(
                '一键卸载',
                '⚠ 确认要彻底卸载 open-ai 吗？\n\n'
                '将删除：\n'
                '· 全部插件文件（含代码、脚本、日志）\n'
                '· 全部配置（config.json）\n'
                '· 全部账号与 API 密钥\n'
                '· 开机自启、计划任务、桌面快捷方式\n\n'
                '此操作不可恢复！'):
            return

        # 调用安装目录内的独立卸载程序 uninstall.exe
        self._write_log('正在启动卸载程序 (uninstall.exe) ...')
        import subprocess as sp
        uninstall_exe = os.path.join(os.path.dirname(BASE), 'uninstall.exe')
        if not os.path.exists(uninstall_exe):
            messagebox.showerror('一键卸载',
                                 '未找到 uninstall.exe\n卸载程序缺失, 无法自动卸载。')
            return
        try:
            # 以独立进程启动 uninstall.exe (GUI 已确认, 传 --silent 免二次询问)
            sp.Popen([uninstall_exe, '--silent'],
                     cwd=os.path.dirname(BASE),
                     creationflags=getattr(sp, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            messagebox.showerror('一键卸载', f'卸载程序启动失败: {e}')
            return
        # 提示后关闭本窗口 (uninstall.exe 负责停进程并删除目录)
        self._write_log('卸载程序已启动, 本窗口即将关闭。')
        self.root.destroy()

    def _setup_table_style(self):
        """给表格加上明显的网格线(基于 ttk clam 主题)。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('Grid.Treeview',
                        rowheight=28,
                        fieldbackground='#ffffff',
                        background='#ffffff',
                        foreground='#222222',
                        bordercolor='#9aa3ad',
                        lightcolor='#9aa3ad',
                        darkcolor='#9aa3ad',
                        borderwidth=1,
                        relief='solid')
        style.configure('Grid.Treeview.Heading',
                        font=('Microsoft YaHei UI', 10, 'bold'),
                        background='#dde3ea',
                        foreground='#222222',
                        relief='solid',
                        borderwidth=1)
        style.map('Grid.Treeview',
                  background=[('selected', '#cce4ff')],
                  foreground=[('selected', '#000000')])
        # 交替行背景, 让网格感更明显
        for tree in (getattr(self, 'tree_trae', None), getattr(self, 'tree_wb', None)):
            if tree is not None:
                tree.tag_configure('odd', background='#f2f5f9')
                tree.tag_configure('even', background='#ffffff')

    def _make_tree(self, parent):
        """创建带上下+左右滚动条的列表, 返回 ttk.Treeview。"""
        cols = ('enabled', 'account', 'detail')
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, padx=6, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show='headings', height=4,
                            style='Grid.Treeview')
        tree.heading('enabled', text='启用')
        tree.heading('account', text='账号')
        tree.heading('detail', text='积分 / 状态')
        tree.column('enabled', width=50, minwidth=40, anchor='center')
        tree.column('account', width=380, minwidth=180, anchor='center')
        tree.column('detail', width=300, minwidth=160, anchor='center')
        vs = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        hs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        # 应用交替行标签
        tree.tag_configure('odd', background='#f2f5f9')
        tree.tag_configure('even', background='#ffffff')
        return tree

    def _make_api_tree(self, parent):
        """API 列表 (可滚动)。"""
        cols = ('name', 'key')
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, padx=4, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show='headings', height=6,
                            style='Grid.Treeview', selectmode='browse')
        tree.heading('name', text='名称')
        tree.heading('key', text='API 密钥')
        tree.column('name', width=160, minwidth=100, anchor='w')
        tree.column('key', width=500, minwidth=300, anchor='w')
        vs = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        hs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.tag_configure('odd', background='#f2f5f9')
        tree.tag_configure('even', background='#ffffff')
        tree.tag_configure('legacy', background='#fff6e5')
        return tree

    def refresh_api_list(self):
        """刷新 API 列表。"""
        self.api_tree.delete(*self.api_tree.get_children())
        try:
            apis = api_store.list_apis()
        except Exception as e:
            messagebox.showerror('API 管理', f'读取失败: {e}')
            return
        for idx, (name, key, legacy) in enumerate(apis):
            tag = 'even' if idx % 2 == 0 else 'odd'
            if legacy:
                tag = 'legacy'
            name_disp = name if name else '（未命名）'
            self.api_tree.insert('', 'end', values=(name_disp, key), tags=(tag,))
        if hasattr(self, 'gw_url_vars'):
            self.refresh_gateway_info()

    def _selected_api(self):
        """返回当前选中行的 (display_name, key, legacy)，未选择返回 None。"""
        sel = self.api_tree.selection()
        if not sel:
            return None
        row = self.api_tree.item(sel[0])
        name, key = row['values']
        tags = row['tags']
        is_legacy = 'legacy' in tags
        return name, key, is_legacy

    # ---------- 网关地址 ----------
    def _gateway_base_url(self):
        """从 config.json 读取网关地址 (host + port)。
        监听 0.0.0.0 / 空时按本机回环展示, 保证复制出去的地址可直接使用。"""
        host, port = '127.0.0.1', 8000
        try:
            with open(OPENAI_CFG, encoding='utf-8') as f:
                cfg = json.load(f)
            host = (cfg.get('host') or '').strip() or '127.0.0.1'
            port = int(cfg.get('port') or 8000)
        except Exception:
            pass
        if host in ('0.0.0.0', '::', ''):
            host = '127.0.0.1'
        return f'http://{host}:{port}'

    def _gateway_urls(self):
        """网关对外地址清单: [(说明, 完整地址), ...]。"""
        base = self._gateway_base_url()
        return [
            ('OpenAI 兼容地址', base + '/v1'),
            ('对话端点', base + '/v1/chat/completions'),
            ('Anthropic 兼容地址 (Claude/CC Switch)', base),
        ]

    def _copy_to_clipboard(self, text, log_msg):
        """复制文本到剪贴板并写操作日志 (tk 主线程调用)。"""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self._write_log(log_msg)

    def _copy_gateway_url(self, text, label):
        self._copy_to_clipboard(text, f'[API] 已复制{label}: {text}')
        messagebox.showinfo('复制成功', f'{label}已复制到剪贴板：\n{text}')

    def _copy_gateway_with_key(self):
        """复制「地址 + API 密钥」组合 (未选中密钥时只复制地址)。"""
        base = self._gateway_base_url()
        sel = self._selected_api()
        if not sel:
            self._copy_to_clipboard(
                base, f'[API] 已复制网关地址(未选密钥): {base}')
            messagebox.showinfo(
                '复制成功',
                '网关地址已复制到剪贴板（未选择 API，未含密钥）:\n' + base)
            return
        name, key, legacy = sel
        disp = name if name else '（未命名）'
        text = f'地址: {base}\nAPI Key: {key}'
        self._copy_to_clipboard(
            text, f'[API] 已复制网关地址+密钥: {base}  key={key[:12]}… ({disp})')
        messagebox.showinfo(
            '复制成功',
            f'已复制到剪贴板:\n\n地址: {base}\nAPI Key: {key}（{disp}）')

    # ---------- API 操作 ----------
    def refresh_gateway_info(self):
        """回读 config.json 的 host/port, 刷新网关地址区显示。"""
        for var, (label, url) in zip(self.gw_url_vars, self._gateway_urls()):
            var.set(url)

    def on_api_create(self):
        try:
            name, key = api_store.create_api()
        except Exception as e:
            self._write_log(f'[API] 创建失败: {e}')
            messagebox.showerror('API 管理', f'创建失败: {e}')
            return
        self._write_log(f'[API] 已创建: {name}  key={key}')
        self.refresh_api_list()
        self.refresh_gateway_info()
        messagebox.showinfo('API 管理', f'已创建 API：{name}\n请在客户端使用该密钥连接。')

    def on_api_rename(self):
        from tkinter import simpledialog
        sel = self._selected_api()
        if not sel:
            messagebox.showwarning('API 管理', '请先在列表中选择一个 API')
            return
        name, key, legacy = sel
        # 旧版若从未起名, 当前显示"（未命名）"
        cur = '' if (legacy and name == '（未命名）') else (name if name != '（未命名）' else '')
        new_name = simpledialog.askstring(
            '命名/改名', f'请输入新名称（当前：{cur or "（未命名）"}）\n'
                         f'这个名字只是给你看的备注，不影响 API 使用。',
            parent=self.root, initialvalue=cur)
        if new_name is None:
            return
        new_name = (new_name or '').strip()
        try:
            cfg = api_store.load_config()
            if legacy:
                # 旧版顶层 api_key 也能改名（名字存为备注，不影响运行）
                api_store.rename_legacy_api(cfg, new_name)
            else:
                api_store.rename_api(cfg, key, new_name)
                api_store.normalize_names(cfg)
        except Exception as e:
            self._write_log(f'[API] 改名失败: {e}')
            messagebox.showerror('API 管理', f'改名失败: {e}')
            return
        label = new_name if new_name else '（未命名）'
        self._write_log(f'[API] 已重命名: {key[:12]}… →「{label}」')
        self.refresh_api_list()

    def on_api_copy(self):
        sel = self._selected_api()
        if not sel:
            messagebox.showwarning('API 管理', '请先在列表中选择一个 API')
            return
        _, key, _ = sel
        self.root.clipboard_clear()
        self.root.clipboard_append(key)
        self.root.update()
        self._write_log(f'[API] 已复制密钥: {key[:12]}…')
        messagebox.showinfo('API 管理', '密钥已复制到剪贴板。')

    def on_api_delete(self):
        sel = self._selected_api()
        if not sel:
            messagebox.showwarning('API 管理', '请先在列表中选择一个 API')
            return
        name, key, legacy = sel
        if legacy:
            messagebox.showinfo('API 管理', '旧版顶层 api_key 不可删除。\n如需移除，请编辑 config.json。')
            return
        if not messagebox.askyesno('API 管理',
                                   f'确定删除 API「{name}」吗？\n删除后该密钥将无法使用。'):
            return
        try:
            cfg = api_store.load_config()
            api_store.delete_api(cfg, key)
        except Exception as e:
            self._write_log(f'[API] 删除失败: {e}')
            messagebox.showerror('API 管理', f'删除失败: {e}')
            return
        self._write_log(f'[API] 已删除: {name}')
        self.refresh_api_list()

    # ---------- 工具 ----------
    def _write_log(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        self.root.update_idletasks()

    def _make_model_tree(self, parent):
        """模型列表表格: 模型名 / 倍率 / 上游模型id (可滚动)。"""
        cols = ('name', 'rate', 'upstream')
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, padx=6, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show='headings', height=6,
                            style='Grid.Treeview')
        tree.heading('name', text='模型名称')
        tree.heading('rate', text='积分倍率')
        tree.heading('upstream', text='上游模型 id')
        tree.column('name', width=220, minwidth=120, anchor='w')
        tree.column('rate', width=90, minwidth=70, anchor='center')
        tree.column('upstream', width=320, minwidth=180, anchor='w')
        vs = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        hs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.tag_configure('odd', background='#f2f5f9')
        tree.tag_configure('even', background='#ffffff')
        # 已隐藏模型灰显样式 (勾选"显示已隐藏模型"时使用)
        tree.tag_configure('hidden', background='#ececec',
                           foreground='#9e9e9e')
        return tree

    def _set_busy(self, busy):
        self.busy = busy
        for b in (self.btn_refresh, self.btn_add_trae, self.btn_add_wb, self.btn_reconnect):
            b.configure(state='disabled' if busy else 'normal')

    def _poll_result_q(self):
        try:
            while True:
                msg = self.result_q.get_nowait()
                self._write_log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_result_q)

    def _run(self, fn):
        """在后台线程执行耗时操作, 结果经队列回主线程。"""
        if self.busy:
            messagebox.showinfo('提示', '正在执行操作, 请稍候...')
            return
        self._set_busy(True)
        self.result_q.put('─' * 60)
        threading.Thread(target=lambda: self._thread_wrap(fn), daemon=True).start()

    def _thread_wrap(self, fn):
        try:
            fn()
        except Exception as e:
            self.result_q.put(f'[错误] {type(e).__name__}: {e}')
        finally:
            self.result_q.put(f'[完成] {time.strftime("%H:%M:%S")}')
            self.root.after(0, lambda: self._set_busy(False))

    def _subprocess_stream(self, py, script, title):
        """后台运行 login_*.py 并实时显示输出 (隐藏控制台窗口)。"""
        self._write_log(title)
        try:
            p = subprocess.Popen([py, script], cwd=os.path.dirname(BASE),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding='utf-8', errors='replace',
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for line in p.stdout:
                self.result_q.put(line.rstrip())
            p.wait()
        except Exception as e:
            self.result_q.put(f'[错误] 运行失败: {e}')

    # ---------- 账号列表 ----------
    def refresh_account_list(self):
        trae_info, wb_info, err = load_account_groups()
        if err:
            messagebox.showwarning('提示', err)
            return
        items_t, items_w = [], []
        trae, trae_accs, device_id, invalid_map = trae_info
        for i, a in enumerate(trae_accs, 1):
            enabled = '✓' if a.get('enabled', True) else '✗'
            items_t.append((enabled, trae_row_name(trae, a, invalid_map, i), ''))
        wb, wb_accs, domain, product = wb_info
        for i, a in enumerate(wb_accs, 1):
            enabled = '✓' if a.get('enabled', True) else '✗'
            items_w.append((enabled, wb_row_name(wb, a, i), ''))
        self._update_trees(items_t, items_w)

    def _build_items_with_credits(self):
        """读取账号并计算每一行的积分(会发网络请求)。
        返回 (trae_items, wb_items, err), 每项为 (启用状态, 账号名, 积分/状态)。"""
        trae_info, wb_info, err = load_account_groups()
        if err:
            return [], [], err
        items_t, items_w = [], []
        trae, trae_accs, device_id, invalid_map = trae_info
        for i, a in enumerate(trae_accs, 1):
            enabled = '✓' if a.get('enabled', True) else '✗'
            items_t.append((enabled, trae_row_name(trae, a, invalid_map, i),
                            trae_row_detail(a, device_id)))
        wb, wb_accs, domain, product = wb_info
        for i, a in enumerate(wb_accs, 1):
            enabled = '✓' if a.get('enabled', True) else '✗'
            items_w.append((enabled, wb_row_name(wb, a, i), wb_row_detail(a, domain, product)))
        return items_t, items_w, None

    def fill_credits(self):
        """后台线程调用的刷积分逻辑: 计算带积分的行, 再由主线程刷新列表。"""
        items_t, items_w, err = self._build_items_with_credits()
        if err:
            self.result_q.put(f'[错误] {err}')
        self.result_q.put(f'[完成] 共 {len(items_t) + len(items_w)} 个账号')
        self.root.after(0, lambda: self._update_trees(items_t, items_w))

    def _update_trees(self, items_t, items_w):
        for tree, items in ((self.tree_trae, items_t), (self.tree_wb, items_w)):
            for i in tree.get_children():
                tree.delete(i)
            for idx, (enabled, acct, det) in enumerate(items):
                tag = 'even' if idx % 2 == 0 else 'odd'
                tree.insert('', 'end', values=(enabled, acct, det), tags=(tag,))

    # ---------- 账号启用/禁用 ----------
    def _on_account_click(self, event):
        """点击表格行时, 若点在"启用"列则切换启用状态, 并通知后端重新加载。"""
        tree = event.widget
        col = tree.identify_column(event.x)
        if col != '#0':  # 非树形列, 转换为列索引
            col_idx = int(col.replace('#', '')) - 1
        else:
            return
        if col_idx != 0:  # 只处理"启用"列
            return
        region = tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        item = tree.identify_row(event.y)
        if not item:
            return
        values = tree.item(item, 'values')
        # 判断该行属于 trae 还是 wb
        is_trae = tree == self.tree_trae
        is_wb = tree == self.tree_wb
        if not is_trae and not is_wb:
            return
        # 根据行索引找账号
        all_items = tree.get_children()
        row_idx = list(all_items).index(item)
        enabled = values[0]
        new_enabled = enabled == '✗'  # 切换
        # 保存到 config.json
        ok, msg = self._save_account_enabled('trae' if is_trae else 'workbuddy', row_idx, new_enabled)
        if ok:
            tree.set(item, 'enabled', '✓' if new_enabled else '✗')
            # 通知后端重新加载账号
            self._trigger_reload(is_trae)

    def _trigger_reload(self, is_trae):
        """通知对应后端重新加载账号配置。"""
        import urllib.request
        if is_trae:
            try:
                req = urllib.request.Request('http://127.0.0.1:18787/v1/admin/reconnect', method='POST')
                urllib.request.urlopen(req, timeout=5)
                self.result_q.put('[设置] TRAE 后端已重载账号状态')
            except Exception as e:
                self.result_q.put(f'[设置] TRAE 后端重载失败: {e}')
        else:
            try:
                req = urllib.request.Request('http://127.0.0.1:8000/v1/admin/reload-providers', method='POST')
                urllib.request.urlopen(req, timeout=5)
                self.result_q.put('[设置] WorkBuddy 后端已重载账号状态')
            except Exception as e:
                self.result_q.put(f'[设置] WorkBuddy 后端重载失败: {e}')

    def _save_account_enabled(self, provider_key, row_idx, enabled):
        """保存账号启用状态到 config.json。provider_key: 'trae' 或 'workbuddy'。返回 (成功否, 消息)。"""
        try:
            with open(OPENAI_CFG, encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception as e:
            return False, str(e)
        prov = cfg.get('providers', {}).get(provider_key, {})
        accs = prov.get('accounts', [])
        if row_idx < 0 or row_idx >= len(accs):
            return False, f'索引越界: {row_idx}/{len(accs)}'
        accs[row_idx]['enabled'] = enabled
        try:
            with open(OPENAI_CFG, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return True, 'ok'
        except Exception as e:
            return False, str(e)

    # ---------- 4 个功能 ----------
    # ---------- 模型列表 (右键: 复制 / 固定 / 隐藏) ----------

    def _model_visible_rows(self, prov, items, show_unknown):
        """把上游模型行整理为渲染行 (应用隐藏/固定/未知倍率过滤与排序)。
        返回 [(key, (name, rate_txt, upstream), is_hidden)]。
        key: TRAE=config_name, WorkBuddy=model_id。
        排序: 固定的在前, 其余保持原始顺序 —— 取消固定即自然回到原位;
        隐藏行始终参与排序 (取消隐藏后, 之前固定的行仍在顶部, 两状态互不干扰)。"""
        st = self.model_state
        pinned = st['pinned'].get(prov) or set()
        hidden = st['hidden'].get(prov) or set()
        rows = []
        for it in items:
            if prov == 'trae':
                name, disp, mdl, rate, _e = it
                key, upstream = name, mdl
                row_name = disp or name  # 展示名优先 (与旧版一致)
            else:
                mid, name, rate, _e = it
                key, upstream = mid, mid
                row_name = name
            is_hidden = key in hidden
            if rate is None and not show_unknown:
                continue  # 未知倍率过滤 (隐藏行同样受控)
            r = '未知' if rate is None else (f'{rate:.2f}x' if prov == 'trae' else str(rate))
            rows.append((key, (row_name, r, upstream), is_hidden))
        rows.sort(key=lambda t: 0 if t[0] in pinned else 1)  # 稳定排序, 组内保持原序
        return rows

    def _fill_model_tree(self, tree, prov, items, err, show_unknown):
        """渲染单个模型表格。"""
        tree.delete(*tree.get_children())
        if err:
            tree.insert('', 'end', values=('', f'获取失败: {err}', ''), tags=('even',))
            return
        if not items:
            tree.insert('', 'end', values=('', '无模型', ''), tags=('even',))
            return
        show_hidden = self.show_hidden_var.get()
        rows = self._model_visible_rows(prov, items, show_unknown)
        unknown_rows = self._model_visible_rows(prov, items, True)  # 含未知倍率的全量行
        if show_hidden:
            visible = rows
        else:
            visible = [row for row in rows if not row[2]]
        if not unknown_rows:
            tree.insert('', 'end', values=('', '无模型', ''), tags=('even',))
            return
        if not rows:
            tree.insert('', 'end', values=('', '无模型（全部未知倍率，勾选"显示未知倍率模型"查看）', ''),
                        tags=('even',))
            return
        if not visible:
            tree.insert('', 'end',
                        values=('', '所有模型已隐藏，勾选"显示已隐藏模型"可恢复', ''),
                        tags=('even',))
            return
        for i, (_key, values, is_hidden) in enumerate(visible):
            tag = 'hidden' if is_hidden else ('even' if i % 2 == 0 else 'odd')
            tree.insert('', 'end', values=values, tags=(tag,))

    def _fill_model_trees(self, trae_items, trae_err, wb_items, wb_err):
        """渲染两表并缓存成功数据 (勾选框切换时用缓存重渲染, 免重复网络请求)。"""
        show_unknown = self.show_unknown_rate_var.get() if hasattr(self, 'show_unknown_rate_var') else False
        self._update_hidden_checkbox()
        # TRAE: (config_name, display, model_name, rate, err)
        if trae_err:
            self._model_errs['trae'] = trae_err
        else:
            self._model_cache['trae'] = trae_items
        self._fill_model_tree(self.model_tree_trae, 'trae',
                              self._model_cache['trae'] or [], self._model_errs['trae'],
                              show_unknown)
        # WorkBuddy: (id, name, credits, err)
        if wb_err:
            self._model_errs['workbuddy'] = wb_err
        else:
            self._model_cache['workbuddy'] = wb_items
        self._fill_model_tree(self.model_tree_wb, 'workbuddy',
                              self._model_cache['workbuddy'] or [], self._model_errs['workbuddy'],
                              show_unknown)

    def _update_hidden_checkbox(self):
        """「显示已隐藏模型」勾选框文案: hidden 数量 > 0 时追加计数。"""
        n = sum(len(self.model_state['hidden'].get(p) or set()) for p in MODEL_PROVIDERS)
        try:
            self.chk_show_hidden.configure(
                text=f'显示已隐藏模型 ({n})' if n > 0 else '显示已隐藏模型')
        except Exception:
            pass

    def on_show_hidden_toggle(self):
        """勾选「显示已隐藏模型」: 仅用缓存重渲染, 不发网络请求。"""
        self._render_model_views()

    def _render_model_views(self):
        """按当前勾选状态重渲染两表 (仅用缓存)。"""
        show_unknown = self.show_unknown_rate_var.get()
        self._update_hidden_checkbox()
        self._fill_model_tree(self.model_tree_trae, 'trae',
                              self._model_cache['trae'] or [],
                              self._model_errs['trae'], show_unknown)
        self._fill_model_tree(self.model_tree_wb, 'workbuddy',
                              self._model_cache['workbuddy'] or [],
                              self._model_errs['workbuddy'], show_unknown)

    def _on_model_menu_trae(self, event):
        self._show_model_menu(event, 'trae', self.model_tree_trae)

    def _on_model_menu_workbuddy(self, event):
        self._show_model_menu(event, 'workbuddy', self.model_tree_wb)

    def _show_model_menu(self, event, prov, tree):
        """模型行右键菜单: 复制模型名称 / 复制上游模型 id / 固定到顶部 / 隐藏。"""
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        values = tree.item(iid, 'values')
        if len(values) < 3:
            return
        name, upstream = values[0], values[2]
        # 用渲染行反查唯一 key (树可能把值规范化为 int 等, 按字符串比较)
        rows = self._model_visible_rows(prov, self._model_cache.get(prov) or [],
                                        self.show_unknown_rate_var.get())
        key = None
        for k, vals, _h in rows:
            if str(vals[0]) == str(values[0]) and str(vals[2]) == str(values[2]):
                key = k
                break
        if key is None:
            return  # 提示行/失败行不给菜单
        st = self.model_state
        pinned = st['pinned'].get(prov) or set()
        hidden = st['hidden'].get(prov) or set()
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='复制模型名称',
                         command=lambda: self._model_copy(name, '模型名称'))
        menu.add_command(label='复制上游模型 id',
                         command=lambda: self._model_copy(upstream, '上游模型 id'))
        menu.add_separator()
        menu.add_command(label='取消固定' if key in pinned else '固定到顶部',
                         command=lambda: self._toggle_pin(prov, key, key not in pinned))
        menu.add_command(label='取消隐藏' if key in hidden else '隐藏',
                         command=lambda: self._toggle_hide(prov, key, key not in hidden))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _model_copy(self, text, label):
        """复制到剪贴板, toast 提示 (非模态, 不打断操作)。"""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self._write_log(f'[模型] 已复制{label}: {text}')
        Toast(self.root, f'已复制{label}')

    def _toggle_pin(self, prov, key, pin):
        """固定/取消固定。取消固定 = 从 pinned 移除, 行回到数据原始顺序位置。"""
        st = self.model_state
        st['pinned'].setdefault(prov, set())
        if pin:
            st['pinned'][prov].add(key)
            Toast(self.root, '已固定到顶部')
        else:
            st['pinned'][prov].discard(key)
            Toast(self.root, '已取消固定')
        save_model_state(st)
        self._render_model_views()

    def _toggle_hide(self, prov, key, hide):
        """隐藏/取消隐藏。固定与隐藏相互独立: 取消隐藏不改动固定状态。"""
        st = self.model_state
        st['hidden'].setdefault(prov, set())
        if hide:
            st['hidden'][prov].add(key)
            Toast(self.root, '已隐藏')
        else:
            st['hidden'][prov].discard(key)
            Toast(self.root, '已取消隐藏')
        save_model_state(st)
        self._update_hidden_checkbox()
        self._render_model_views()

    def _render_models(self):
        """后台线程拉取两个平台模型+倍率, 主线程填充表格。
        上游更新后: 新模型按原始顺序进入列表; 状态里已消失的 key 自动惰性清理。"""
        def work():
            self.result_q.put('正在拉取模型列表及积分倍率 ...')
            trae_items, trae_err = am.trae_model_rates()
            wb_items, wb_err = am.wb_model_rates()
            self.root.after(0, lambda: self._after_models_fetched(
                trae_items, trae_err, wb_items, wb_err))
        # 用独立线程, 不占用账号操作的 busy 锁
        threading.Thread(target=work, daemon=True).start()

    def _after_models_fetched(self, trae_items, trae_err, wb_items, wb_err):
        """拉取完成 (主线程): 先清理上游已消失的 key, 再渲染。"""
        if not trae_err and trae_items:
            self._prune_model_state('trae', [it[0] for it in trae_items])
        if not wb_err and wb_items:
            self._prune_model_state('workbuddy', [it[0] for it in wb_items])
        self._fill_model_trees(trae_items, trae_err, wb_items, wb_err)

    def _prune_model_state(self, prov, current_keys):
        """清理 pinned/hidden 中上游已不存在的 key (只清状态引用, 不动模型数据)。"""
        st = self.model_state
        cur = set(str(k) for k in current_keys)
        changed = False
        for grp in ('pinned', 'hidden'):
            s = st[grp].get(prov) or set()
            dead = {k for k in s if k not in cur}
            if dead:
                s.difference_update(dead)
                changed = True
        if changed:
            self._write_log(f'[模型] {prov} 上游已更新, 已清理失效的固定/隐藏项')
            save_model_state(st)
        return changed

    def on_model_refresh(self):
        # 独立线程加载, 不占用账号操作的 busy 锁
        self.result_q.put('正在拉取模型列表及积分倍率 ...')
        self._render_models()

    def on_refresh(self):
        def work():
            self.fill_credits()
        self._run(work)

    def on_add_trae(self):
        def work():
            self.result_q.put('>>> 即将打开 TRAE 网页登录, 请在浏览器中完成登录 <<<')
            self._subprocess_stream(VENV_PY, os.path.join(BASE, 'login_trae.py'),
                                    'TRAE 登录助手 (login_trae.py)')
            # 添加后强制 server.js 重新加载账号池, 并刷新列表
            self.result_q.put('[设置] 正在让后端重新加载账号...')
            self._reload_trae_server()
            self.refresh_account_list()   # 立即从 config 刷新账号列表(无网络)
            self.fill_credits()           # 再补积分
        self._run(work)

    def _reload_trae_server(self):
        """请求运行中的 trae server.js 重新加载账号池。"""
        import urllib.request
        try:
            req = urllib.request.Request('http://127.0.0.1:18787/v1/admin/reconnect',
                                         method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.result_q.put(f'[设置] 后端重新加载完成 (HTTP {resp.status})')
        except Exception as e:
            self.result_q.put(f'[设置] 后端重载失败(可忽略): {type(e).__name__}')

    def on_add_wb(self):
        def work():
            self.result_q.put('>>> 即将打开 WorkBuddy 网页登录, 请在浏览器中完成登录 <<<')
            self._subprocess_stream(VENV_PY, os.path.join(BASE, 'login_workbuddy.py'),
                                    'WorkBuddy 登录助手 (login_workbuddy.py)')
            self.result_q.put('[提示] 登录成功后新账号即可参与轮询')
            self.fill_credits()
        self._run(work)

    def on_reconnect(self):
        def work():
            self.result_q.put('正在重新连接全部 provider ...')
            old = sys.stdout
            buf = []
            class Cap:
                def write(self, s):
                    buf.append(s)
                def flush(self):
                    pass
            sys.stdout = Cap()
            try:
                am.reconnect_all()
            finally:
                sys.stdout = old
            for line in ''.join(buf).splitlines():
                if line.strip():
                    self.result_q.put(line)
            self.result_q.put('重新连接结束')
        self._run(work)


def _set_windows_app_id():
    """设置 Windows AppUserModelID, 让任务栏显示自定义图标而非 Python 默认图标。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'openai.account-manager')
    except Exception:
        pass


def main():
    _set_windows_app_id()
    root = tk.Tk()
    app = AccountManagerApp(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # pythonw 无控制台, 崩溃时写日志便于排查
        import traceback
        try:
            logs_dir = os.path.join(os.path.dirname(BASE), 'logs')
            os.makedirs(logs_dir, exist_ok=True)
            with open(os.path.join(logs_dir, 'gui_account_manager.err.log'), 'w',
                      encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        try:
            messagebox.showerror('账号管理启动失败', str(e))
        except Exception:
            pass
        sys.exit(1)