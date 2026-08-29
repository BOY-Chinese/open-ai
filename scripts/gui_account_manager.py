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
页面2「操作日志」: 点击该页签才显示操作日志
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

        self._build_ui()
        self._poll_result_q()
        self.refresh_account_list()
        # 启动后自动拉取一次积分/状态
        self.root.after(300, self.on_refresh)
        # 启动后自动加载模型列表(含积分倍率), 独立线程不锁界面
        self.root.after(800, self.on_model_refresh)
        # 回读开机自启状态
        self._refresh_autostart_state()

    def _set_icon(self, root):
        """设置窗口/任务栏图标为软件 logo。
        对 Microsoft Store 版 Python, 额外用 Win32 WM_SETICON 强制设置, 确保任务栏显示正确图标。"""
        ico_path = os.path.join(os.path.dirname(BASE), 'pic', 'open-ai.ico')
        if not os.path.exists(ico_path):
            return
        try:
            root.iconbitmap(default=ico_path)
        except Exception:
            pass
        # Win32 级别强制设置图标 (兼容 Store 版 Python 的任务栏)
        try:
            import ctypes
            user32 = ctypes.windll.user32
            WM_SETICON = 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            root.update_idletasks()
            # tkinter 顶层窗口 HWND = GetParent(root.winfo_id())
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            for size in (ICON_SMALL, ICON_BIG):
                dim = 16 if size == ICON_SMALL else 32
                hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, dim, dim,
                                          LR_LOADFROMFILE)
                if hicon:
                    user32.SendMessageW(hwnd, WM_SETICON, size, hicon)
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

        # WorkBuddy 模型 (下)
        self.wb_model_frame = ttk.LabelFrame(page_model, text='WorkBuddy 模型')
        self.wb_model_frame.pack(fill='both', expand=True, **pad)
        self.model_tree_wb = self._make_model_tree(self.wb_model_frame)

        # 刷新按钮 + 显示未知倍率勾选框
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

    # ---------- API 操作 ----------
    def on_api_create(self):
        try:
            name, key = api_store.create_api()
        except Exception as e:
            self._write_log(f'[API] 创建失败: {e}')
            messagebox.showerror('API 管理', f'创建失败: {e}')
            return
        self._write_log(f'[API] 已创建: {name}  key={key}')
        self.refresh_api_list()
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
    # ---------- 模型列表 ----------
    def _render_models(self):
        """后台线程拉取两个平台模型+倍率, 主线程填充表格。"""
        def work():
            self.result_q.put('正在拉取模型列表及积分倍率 ...')
            trae_items, trae_err = am.trae_model_rates()
            wb_items, wb_err = am.wb_model_rates()
            self.root.after(0, lambda: self._fill_model_trees(
                trae_items, trae_err, wb_items, wb_err))
        # 用独立线程, 不占用账号操作的 busy 锁
        threading.Thread(target=work, daemon=True).start()

    def _fill_model_trees(self, trae_items, trae_err, wb_items, wb_err):
        show_unknown = self.show_unknown_rate_var.get() if hasattr(self, 'show_unknown_rate_var') else False

        def fill(tree, rows, empty_msg):
            tree.delete(*tree.get_children())
            if empty_msg:
                tree.insert('', 'end', values=('', empty_msg, ''), tags=('even',))
                return
            for idx, row in enumerate(rows):
                tag = 'even' if idx % 2 == 0 else 'odd'
                tree.insert('', 'end', values=row, tags=(tag,))
        # TRAE: (config_name, display, model_name, rate, err)
        if trae_err:
            fill(self.model_tree_trae, [], f'获取失败: {trae_err}')
        else:
            rows = []
            for name, disp, mdl, rate, e in trae_items:
                r = '未知' if rate is None else f'{rate:.2f}x'
                if not show_unknown and rate is None:
                    continue  # 不显示未知倍率
                rows.append((disp, r, mdl))
            fill(self.model_tree_trae, rows, '' if rows else '无模型（全部未知倍率，勾选"显示未知倍率模型"查看）')
        # WorkBuddy: (id, name, credits, err)
        if wb_err:
            fill(self.model_tree_wb, [], f'获取失败: {wb_err}')
        else:
            rows = []
            for mid, name, credits, e in wb_items:
                r = '未知' if credits is None else str(credits)
                if not show_unknown and credits is None:
                    continue  # 不显示未知倍率
                rows.append((name, r, mid))
            fill(self.model_tree_wb, rows, '' if rows else '无模型（全部未知倍率，勾选"显示未知倍率模型"查看）')

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