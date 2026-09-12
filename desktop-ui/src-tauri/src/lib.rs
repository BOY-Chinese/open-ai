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
    // 判据只要求 bootstrap.py + main.py（后端入口）。
    //
    // ★ 不要把 config.json 列为必需：它是**可缺**的 —— 用户可能在手动换配置、
    //   卸载残留、或正处在「没有账号/没有密钥」的测试场景里。一旦要求它存在，
    //   find_root() 会返回 None，界面直接报「未找到 open-ai 安装目录」，
    //   连网关都连不上，把一个「缺配置文件」的小问题放大成「应用不可用」。
    //   （本机实测踩到：用户删掉 config.json 后，桌面端整个失效。）
    p.join("bootstrap.py").is_file() && p.join("main.py").is_file()
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
///
/// ★ v3.0 起密钥统一存放在 `api_keys` 数组里（顶层 `api_key` 已取消）。
/// 这里先取 api_keys[0]，再回落顶层 `api_key` —— 后者只覆盖
/// 「网关尚未跑完迁移」的极短窗口，保证桌面端不会因为结构切换而 401。
fn gateway_key(root: &Path) -> String {
    let cfg = match read_config(root) {
        Some(v) => v,
        None => return String::new(),
    };
    let from_list = cfg
        .get("api_keys")
        .and_then(|v| v.as_array())
        .and_then(|arr| {
            arr.iter()
                .find_map(|a| a.get("key").and_then(|k| k.as_str()))
        })
        .unwrap_or("")
        .trim()
        .to_string();
    if !from_list.is_empty() {
        return from_list;
    }
    cfg.get("api_key")
        .and_then(|k| k.as_str())
        .unwrap_or("")
        .trim()
        .to_string()
}

/// 停止全部 open-ai 后端进程（托盘「退出」用）。
///
/// 复用 `bootstrap.py stop`，而不是在 Rust 里重写一遍：
///   - 它是唯一实现了「IPC 优雅广播 → leaf Job → root Job → 兜底 kill」的入口，
///     重复实现必然与 Python 侧行为漂移；
///   - 品牌化 CLI shim `open-ai.exe` 保证任务管理器里显示的是 open-ai 而不是 python。
///
/// 另外写 `data/.gui_exit_suppress`（时间戳）：计划任务 `watchdog_boot.py`
/// 在 10 分钟窗口内不复活 Broker —— 否则用户刚点「退出」，后端几秒后又被拉起来。
/// 这与旧版 Python 托盘「彻底退出」的行为一致。
fn stop_all_backends(root: &Path) {
    let data = root.join("data");
    let _ = std::fs::create_dir_all(&data);
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let _ = std::fs::write(data.join(".gui_exit_suppress"), format!("{ts}"));

    let cli = root.join("runtime").join("Scripts").join("open-ai.exe");
    let py = root.join(".venv").join("Scripts").join("python.exe");
    let entry = if cli.is_file() {
        cli
    } else if py.is_file() {
        py
    } else {
        return;
    };

    let mut cmd = Command::new(entry);
    cmd.arg(root.join("bootstrap.py"))
        .arg("stop")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_window(&mut cmd);
    let _ = cmd.spawn();
}

/// 启动安装目录下的卸载程序（系统设置页「一键卸载」）。
///
/// `--silent`：桌面端已经弹过一次二次确认，卸载器不再重复询问，
/// 但仍会显示自己的进度窗口（用户能看见它在删什么）。
fn launch_uninstaller(root: &Path) -> Result<PathBuf, String> {
    let exe = root.join("uninstall.exe");
    if !exe.is_file() {
        return Err(format!("未找到卸载程序：{}", exe.display()));
    }
    let mut cmd = Command::new(&exe);
    // 不加任何提权标志：以普通进程启动可免 UAC（安装目录在用户可写位置时足够）
    cmd.arg("--silent")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_window(&mut cmd);
    cmd.spawn()
        .map_err(|e| format!("启动卸载程序失败：{e}"))?;
    Ok(exe)
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

/// 一键卸载：拉起安装目录下的 uninstall.exe，随后退出桌面端。
///
/// 为什么由 Rust 侧做而不是走 HTTP：
///   uninstall.exe 会删掉 `<安装根>\desktop\open-ai-desktop.exe` 自身。
///   如果只是通知网关去拉起它、界面继续跑，exe 被占用会导致删除失败
///   （用户实测：点了「一键卸载」什么都没发生 —— 原实现其实是个空壳，
///   只弹了个 toast，压根没调用卸载程序）。
///   所以这里：启动卸载器 → 等它起来 → 自己退出，把文件占用让出来。
#[tauri::command]
fn uninstall_app<R: Runtime>(app: AppHandle<R>) -> Result<String, String> {
    let root = find_root()
        .ok_or_else(|| "未找到 open-ai 安装目录（缺少 bootstrap.py / main.py）".to_string())?;
    let exe = launch_uninstaller(&root)?;

    // 给卸载器一点时间把窗口画出来，再让出 exe 占用
    QUITTING.store(true, Ordering::SeqCst);
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(1500));
        app.exit(0);
    });
    Ok(format!("已启动卸载程序：{}", exe.display()))
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
/// 行为：
///   - 左键单击 / 菜单「显示主界面」→ 恢复窗口
///   - 窗口 X → 隐藏到托盘（见 on_window_event），后端不受影响
///   - 菜单「退出」→ **退出全部 open-ai 进程**：先把 Broker 优雅停掉
///     （bootstrap.py stop，含 trae node 与定时任务），再退出桌面端。
///     这样任务管理器里不会残留任何 open-ai 进程。
fn build_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", "打开日志目录", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
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
                // 「退出」= 退出**全部** open-ai 进程（界面 + 网关 + trae node + 定时任务）。
                // 用户明确要求：托盘退出就该是一次干净的全退，而不是只关界面把
                // 后端留在后台 ——「我点了退出，为什么任务管理器里还有一堆 open-ai」
                // 是旧版最容易引起困惑的地方。
                QUITTING.store(true, Ordering::SeqCst);
                if let Some(root) = find_root() {
                    stop_all_backends(&root);
                }
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
            uninstall_app,
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
