/// <reference types="vite/client" />

/**
 * 环境变量类型（供 import.meta.env 使用）
 *
 * VITE_GATEWAY_BASE  — 管理面基址。开发态留空走 Vite 代理；
 *                      打包态由 Tauri 注入（默认 http://127.0.0.1:8000）
 * VITE_DATA_SOURCE   — 'mock' 时强制使用演示数据源（默认走真实后端）
 */
interface ImportMetaEnv {
  readonly VITE_GATEWAY_BASE?: string
  readonly VITE_DATA_SOURCE?: 'mock' | 'http'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
