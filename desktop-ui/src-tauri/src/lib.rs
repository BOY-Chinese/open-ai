//! open-ai 桌面端 — Tauri 2 后端入口
//!
//! 职责边界：
//!   - 本层只做「进程生命周期 + 窗口 + 托盘 + 本地系统能力（自启/日志）」，
//!     以及「把网关的地址与密钥交给前端」这一件事
//!   - 业务数据（账号/密钥/模型/积分）由既有 Python 网关 (127.0.0.1:8000)
//!     提供，前端经 `src/lib/httpBackend.ts` 直接对接，本层不重复实现业务逻辑
//!
//! v3.0 修复的三件事（对应虚拟机/本机实测反馈）：
//!   1. **托盘图标**：v3.0 起桌面端取代 Python GUI 成为默认界面，而托盘原先只在
//!      Python GUI 里创建 → 桌面端必须自带托盘，否则「后端在跑但托盘空着」。
//!   2. **前后端链路**：打包态前端跑在 `tauri://localhost`，相对路径 `/v1/*`
//!      会打到内嵌资源而不是网关；密钥又只由开发态 Vite 代理注入。
//!      故新增 `gateway_config` 命令，把 config.json 里的地址与密钥在**运行期**
//!      交给前端（产物中依然不含任何密钥明文）。
//!   3. **后端未运行**：新增 `start_backend` 命令，双击图标即可拉起 Broker。
//!
//! 线程与阻塞说明：本文件不发起任何网络请求（探活由前端 fetch 完成），
//! 因此命令均为同步且瞬时返回，不会卡住主线程。

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Runtime, WindowEvent};

/// 托盘「退出」时置位：让 CloseRequested 的「关闭即隐藏」拦截放行，
/// 否则 `app.exit()` 会被自己的拦截逻辑压回托盘，表现为「点了退出没反应」。
static QUITTING: AtomicBool = AtomicBool::new(false);

/// 网关默认端口（config.json 不可读时的兜底）
const DEFAULT_PORT: u16 = 8000;

/// 主窗口 label（tauri.conf.json 中未显式指定时的默认值）
const MAIN_WINDOW: &str = "main";

// ─────────────────────────── 安装根目录定位 ───────────────────────────

/// 判断目录是否是 open-ai 根目录：同时存在后端入口脚本与网关配置
fn is_root(p: &Path) -> bool {
    p.join("bootstrap.py").is_file() && p.join("config.json").is_file()
}

/// 定位 open-ai 根目录。
///
/// 为什么要「上溯」而不是写死路径：
///   - 打包态：`<安装根>\desktop\open-ai-desktop.exe` → 上一级即根
///   - 开发态：`<项目>\desktop-ui\src-tauri\target\release\...` → 上溯 4~5 级
///   两种布局用同一套逻辑，且用户可把软件装到任意盘符（原 tkinter 版
///   依赖硬编码的 D:\app\... 路径，换机器即失效）。
fn find_root() -> Option<PathBuf> {
    for key in ["OPEN_AI_ROOT", "OPENAI_HOME"] {
        if let Ok(v) = std::env::var(key) {
            let p = PathBuf::from(v);
            if is_root(&p) {
                return Some(p);
            }
        }
    }

    let mut cur = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))?;
    for _ in 0..6 {
        if is_root(&cur) {
            return Some(cur);
        }
        match cur.parent() {
            Some(parent) => cur = parent.to_path_buf(),
            None => break,
        }
    }
    None
}

/// 读取 config.json（失败一律返回 None，由调用方走兜底值）
fn read_config(root: &Path) -> Option<serde_json::Value> {
    let raw = std::fs::read_to_string(root.join("config.json")).ok()?;
    serde_json::from_str(&raw).ok()
}

/// 网关监听端口：读 config.json 的 `port`，缺失/非法则用 8000
fn gateway_port(root: &Path) -> u16 {
    read_config(root)
        .and_then(|v| v.get("port").and_then(|p| p.as_u64()))
        .filter(|p| *p > 0 && *p < 65536)
        .map(|p| p as u16)
        .unwrap_or(DEFAULT_PORT)
}

/// 网关 API 密钥（前端凭它访问 /v1/admin/*）
fn gateway_key(root: &Path) -> String {
    read_config(root)
        .and_then(|v| v.get("api_key").and_then(|k| k.as_str()).map(String::from))
        .unwrap_or_default()
}

/// 后端启动入口优先级：
///   1. 品牌化 shim（runtime\Scripts\open-ai-daemon.exe，任务管理器显示 open-ai）
///   2. venv 里的 python.exe（全新安装、尚未生成 shim 时）
///   3. venv 里的 pythonw.exe（无控制台版本，最后兜底）
fn backend_entry(root: &Path) -> Option<PathBuf> {
    let candidates = [
        root.join("runtime").join("Scripts").join("open-ai-daemon.exe"),
        root.join(".venv").join("Scripts").join("python.exe"),
        root.join(".venv").join("Scripts").join("pythonw.exe"),
    ];
    candidates.into_iter().find(|p| p.is_file())
}

/// Windows 下隐藏子进程窗口（桌面端自身无控制台，子进程默认会闪黑框）
#[cfg(windows)]
fn hide_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    // DETACHED_PROCESS：后端不随桌面端退出而退出（网关需常驻）
    cmd.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS);
}

#[cfg(not(windows))]
fn hide_window(_cmd: &mut Command) {}

// ────────────────────────────── 命令 ──────────────────────────────

/// 前端运行期所需的网关信息（不含任何业务数据）
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GatewayConfig {
    /// 网关基址，如 http://127.0.0.1:8000
    pub base_url: String,
    /// 网关密钥（前端放在 Authorization: Bearer）
    pub api_key: String,
    /// 安装根目录（诊断用，出错面板会展示）
    pub root: String,
    /// 是否具备「一键拉起后端」的能力（找不到入口则为 false）
    pub can_start: bool,
}

/// 把网关地址与密钥交给前端 —— 取代开发态 Vite 代理的角色。
///
/// 安全边界：密钥只留在本机进程内存与磁盘上的 config.json，不写入前端产物；
/// 页面仅加载内嵌资源（无远程内容），与开发态经代理注入的信任级别一致。
#[tauri::command]
fn gateway_config() -> GatewayConfig {
    match find_root() {
        Some(root) => GatewayConfig {
            base_url: format!("http://127.0.0.1:{}", gateway_port(&root)),
            api_key: gateway_key(&root),
            root: root.to_string_lossy().into_owned(),
            can_start: backend_entry(&root).is_some(),
        },
        None => GatewayConfig {
            base_url: format!("http://127.0.0.1:{DEFAULT_PORT}"),
            api_key: String::new(),
            root: String::new(),
            can_start: false,
        },
    }
}

/// 拉起后端进程树（Broker → gateway :8000 / trae node / 定时任务）。
///
/// 幂等：Broker 自身有单实例锁，已在运行时本命令是空操作，可直接重复调用。
/// 立即返回，不等待就绪 —— 就绪判定由前端轮询 /v1/admin/health 完成
/// （避免命令阻塞主线程，也避免把超时策略写死在 Rust 侧）。
#[tauri::command]
fn start_backend() -> Result<String, String> {
    let root = find_root()
        .ok_or_else(|| "未找到 open-ai 根目录（需同时存在 bootstrap.py 与 config.json）".to_string())?;
    let entry = backend_entry(&root)
        .ok_or_else(|| "未找到后端入口（runtime\\Scripts\\open-ai-daemon.exe 或 .venv）".to_string())?;

    let mut cmd = Command::new(&entry);
    cmd.arg(root.join("bootstrap.py"))
        .arg("start")
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_window(&mut cmd);

    cmd.spawn()
        .map_err(|e| format!("拉起后端失败：{e}"))?;
    Ok(format!("已请求启动后端：{}", entry.display()))
}

/// 在资源管理器中打开日志目录（错误面板的「打开日志」入口）
///
/// 泛型 R 不可省：`#[tauri::command]` 里裸写 `AppHandle` 会被解析成默认运行时
/// 的 `AppHandle<Wry>`，而调用方（托盘泛型函数）持有的是 `AppHandle<R>`，
/// 二者无法统一 → E0308。
#[tauri::command]
fn open_logs_dir<R: Runtime>(app: AppHandle<R>) -> Result<String, String> {
    let dir = find_root()
        .map(|r| r.join("logs"))
        .ok_or_else(|| "未找到 open-ai 根目录，无法定位 logs/".to_string())?;
    let _ = std::fs::create_dir_all(&dir);
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(dir.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|e| format!("打开日志目录失败：{e}"))?;
    Ok(dir.to_string_lossy().into_owned())
}

/// 返回应用版本号（系统设置页「版本信息」卡片使用）
#[tauri::command]
fn app_version() -> String {
    format!("v{}-dev", env!("CARGO_PKG_VERSION"))
}

// ────────────────────────── 窗口 / 托盘 ──────────────────────────

/// 恢复主窗口：显示 + 取消最小化 + 置前
fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(w) = app.get_webview_window(MAIN_WINDOW) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// 创建系统托盘图标。
///
/// 行为对齐原 tkinter 版（用户已习惯）：
///   - 左键单击 / 菜单「显示主界面」→ 恢复窗口
///   - 窗口 X → 隐藏到托盘（见 on_window_event）
///   - 菜单「退出界面」→ 退出桌面端进程；**后端保持运行**
///     （原版「退出」会连同 Broker 一起关掉，此处改为只退界面并在菜单文案里
///      明写，避免用户以为关掉界面就等于停掉网关）
fn build_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", "打开日志目录", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出界面（后端继续运行）", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&show, &logs, &sep, &quit])?;

    // 显式给 TrayIconBuilder 指定运行时 R：
    // `with_id` 不带 manager 参数，类型推断会落到默认运行时上，
    // 导致 `.build(app)` 报 `AppHandle<R>: Manager<Wry>` 不成立。
    let tray = TrayIconBuilder::<R>::with_id("open-ai-tray")
        .icon(tauri::include_image!("icons/32x32.png"))
        .tooltip("open-ai 账号管理")
        // 右键才出菜单：左键留给「显示主界面」，与旧版一致
        .show_menu_on_left_click(false)
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main_window(app),
            "logs" => {
                if let Err(e) = open_logs_dir(app.clone()) {
                    eprintln!("[open-ai] 打开日志目录失败: {e}");
                }
            }
            "quit" => {
                QUITTING.store(true, Ordering::SeqCst);
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    // 托盘图标在部分 Windows 上首次创建后不刷新，重建一次可稳定显示
    let _ = tray.set_visible(true);
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 单实例必须最先注册：第二次双击快捷方式时唤醒已有窗口，
        // 否则会出现「两个托盘图标 + 两个窗口」。
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            gateway_config,
            start_backend,
            open_logs_dir,
            app_version
        ])
        .setup(|app| {
            build_tray(app.handle())?;

            // --minimized / --tray：开机自启场景，静默驻留托盘不弹窗
            let minimized = std::env::args().any(|a| a == "--minimized" || a == "--tray");
            if let Some(w) = app.get_webview_window(MAIN_WINDOW) {
                if minimized {
                    let _ = w.hide();
                } else {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // 窗口 X = 最小化到托盘（不销毁窗口、不退出进程、后端不受影响）
            if let WindowEvent::CloseRequested { api, .. } = event {
                if QUITTING.load(Ordering::SeqCst) {
                    return;
                }
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running open-ai desktop application");
}
