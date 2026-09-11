import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import fs from 'node:fs'

// Tauri 期望固定端口，且失败即退出（避免端口漂移导致白屏）
const host = process.env.TAURI_DEV_HOST

/**
 * 开发代理注入的网关密钥：
 * 从上一级（Python 工程根）的 config.json 读取 api_key。
 * 这样前端代码里**不出现任何密钥**，也无需手工配置开发环境；
 * 生产态由 Tauri 的 Rust 命令做同样的事（读同一个 config.json）。
 * 读取失败则不带鉴权头，请求会得到 401，便于立即发现配置问题。
 */
function readGatewayKey(): string {
  const candidates = [
    process.env.OPENAI_API_KEY,
    path.resolve(__dirname, '../config.json'),
    path.resolve(__dirname, 'config.json'),
  ].filter(Boolean) as string[]

  for (const c of candidates) {
    if (!c.endsWith('.json')) return c
    try {
      const cfg = JSON.parse(fs.readFileSync(c, 'utf-8'))
      if (cfg?.api_key) return String(cfg.api_key)
    } catch {
      /* 换下一个候选 */
    }
  }
  return ''
}

const GATEWAY_URL = process.env.GATEWAY_URL || 'http://127.0.0.1:8000'
const GATEWAY_KEY = readGatewayKey()

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || '127.0.0.1',
    hmr: host ? { protocol: 'ws', host, port: 1421 } : undefined,
    watch: { ignored: ['**/src-tauri/**'] },
    /**
     * 开发态把 /v1 代理到本地网关：
     *  - 规避浏览器 CORS（请求同源）
     *  - 密钥由代理注入，前端产物中不含凭据
     */
    proxy: {
      '/v1': {
        target: GATEWAY_URL,
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            if (GATEWAY_KEY) {
              proxyReq.setHeader('Authorization', `Bearer ${GATEWAY_KEY}`)
            }
          })
        },
      },
    },
  },
  build: {
    target: 'chrome110',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
})
