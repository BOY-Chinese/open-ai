/**
 * 数据源选择器 —— 页面统一从此处取 `backend`
 *
 * 两种实现同契约（函数签名一致），故页面代码无需感知差异：
 *   - httpBackend：真实网关（/v1/admin/*），供 Tauri 应用与开发态使用
 *   - mockBackend：纯前端演示数据，无网关时也能跑通界面
 *
 * 切换方式（按优先级）：
 *   1. URL 参数 ?mock=1        —— 临时演示 / 视觉回归
 *   2. 环境变量 VITE_DATA_SOURCE=mock
 *   3. 默认：真实后端
 *
 * 注意：真实模式下若网关未启动，页面会显示明确的错误态
 * （由各页面的 useAsync 捕获 ApiError 渲染），而非静默回落 mock ——
 * 这样「连不上」不会被误认为「数据是空的」。
 *
 * v3.0 增补：导出的 `backend` 外面还包了一层**操作日志代理**
 * （见 `./oplogBackend`），凡是会改状态的调用都会自动写进「操作日志」页。
 * 页面代码不需要、也不应该为记日志做任何改动。
 */
import { httpBackend } from './httpBackend'
import { mockBackend } from './backend'
import { warnUnloggedMutations, withOpLog } from './oplogBackend'

function pickSource() {
  const fromUrl =
    typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).get('mock') === '1'
  const fromEnv = import.meta.env.VITE_DATA_SOURCE === 'mock'
  return fromUrl || fromEnv ? mockBackend : httpBackend
}

/** 未经包装的数据源（用于判定模式；包装后是 Proxy，不能直接比引用） */
const rawSource = pickSource()

export const backend = withOpLog(rawSource)

/** 当前是否运行在演示（Mock）数据模式 */
export const isMockMode = rawSource === mockBackend

/* 开发期自检：新增的写操作若忘了登记操作日志，在控制台提醒（详见 oplogBackend） */
warnUnloggedMutations(rawSource)
