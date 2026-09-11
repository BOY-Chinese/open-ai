//! open-ai 桌面端 — Tauri 2 后端入口
//!
//! 职责边界：
//!   - 本层只做「进程生命周期 + 窗口 + 本地系统能力（自启/卸载/日志）」
//!   - 业务数据（账号/密钥/模型/积分）由既有 Python 网关 (127.0.0.1:8000)
//!     提供，前端经 `src/lib/backend.ts` 对接，本层不重复实现业务逻辑
//!
//! 当前阶段：命令以 mock 数据返回，前端可独立跑通；
//! 后续在 `backend-http` feature 下替换为对网关的 HTTP 调用。

use serde::Serialize;

/// 网关探活结果 — 对应侧边栏「网关运行中 / 已断开」指示
#[derive(Serialize)]
pub struct GatewayStatus {
    pub online: bool,
    pub endpoint: String,
    pub version: String,
}

/// 探测本地网关是否在线
///
/// 现阶段返回固定值；接入真实后端时改为对 `http://127.0.0.1:8000/`
/// 发起短超时请求（`backend-http` feature）。
#[tauri::command]
fn gateway_status() -> GatewayStatus {
    GatewayStatus {
        online: true,
        endpoint: "http://127.0.0.1:8000/v1".into(),
        version: env!("CARGO_PKG_VERSION").into(),
    }
}

/// 返回应用版本号（系统设置页「版本信息」卡片使用）
#[tauri::command]
fn app_version() -> String {
    format!("v{}-dev", env!("CARGO_PKG_VERSION"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![gateway_status, app_version])
        .run(tauri::generate_context!())
        .expect("error while running open-ai desktop application");
}
