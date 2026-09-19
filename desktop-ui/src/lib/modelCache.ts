/**
 * 模型列表本地缓存 + 视图过滤
 *
 * 为什么需要
 * ----------
 * 模型列表页要显示「积分倍率」，而倍率必须逐个通道向上游查询，**耗时秒级**
 * （见后端 `/v1/admin/models/rates` 的注释：列表本体是内存读取毫秒级，
 * 倍率才需要联网）。于是每次进入模型列表页都要空等一次网络往返，
 * 期间只有骨架屏 —— 这就是「每次打开都要等一会儿」的来源。
 *
 * 做法：把**合并后的全量模型列表**（三通道 + 含隐藏项 + 已叠加视图偏好）
 * 落一份到 localStorage。页面首帧直接拿它渲染，随后在后台拉取最新数据并
 * 覆盖缓存；因此第二次之后的打开是「瞬间有内容」，而不是「空白等待」。
 *
 * 设计取舍
 * ----------
 * 1. **缓存全量，不按筛选条件分片**。通道/隐藏筛选改由 {@link filterModelView}
 *    在本地完成 —— 切换筛选不再触发网络请求，也就不会再有第二次骨架屏。
 * 2. **同步读写**。`readModelCache` 是同步的，页面可以在首次 render 就拿到
 *    数据，不需要 useEffect 中转（否则仍会闪一帧骨架屏）。
 * 3. **只读式校验**。localStorage 可能被用户/旧版本写脏，读到结构不符的数据
 *    一律当作「无缓存」，宁可从零加载也不要让页面崩在半个对象上。
 * 4. **过期不丢弃**。超过 {@link MODEL_CACHE_TTL_MS} 只把 `stale` 标为 true
 *    （界面提示「缓存较旧」），数据照常先渲染 —— 陈旧列表也比空屏有用。
 */
import type { ChannelFilter, ModelEntry } from '@/types/domain'

/** 缓存键（带版本号：结构变更时旧键自然失效，无需迁移代码） */
const CACHE_KEY = 'open-ai.models-cache.v1'

/**
 * 缓存「新鲜度」阈值：30 分钟。
 *
 * 取值理由：倍率由上游定价决定，实际变动频率远低于此；超过阈值只是提示用户
 * 可以点「刷新」，不阻止渲染。
 */
export const MODEL_CACHE_TTL_MS = 30 * 60 * 1000

export interface ModelCacheSnapshot {
  /** 全量模型（三通道、含隐藏项、已按置顶排序、已叠加本地视图偏好） */
  list: ModelEntry[]
  /** 写入时刻（epoch 毫秒） */
  savedAt: number
  /** 距写入已超过 {@link MODEL_CACHE_TTL_MS} */
  stale: boolean
}

/**
 * 单条模型记录的最小形状校验（缺一即认为整份缓存不可信）。
 *
 * 注意：**不校验 `ratioKnown`**。旧版本缓存没有这个字段，若在此判为非法，
 * 用户升级后第一次打开会被整份丢弃 → 又回到「每次都空等网络」的老问题。
 * 缺失时由 {@link withRatioKnown} 补一个合理默认。
 */
function isModelEntry(v: unknown): v is ModelEntry {
  if (!v || typeof v !== 'object') return false
  const m = v as Record<string, unknown>
  return (
    typeof m.id === 'string' &&
    typeof m.name === 'string' &&
    typeof m.channel === 'string' &&
    typeof m.routeModelId === 'string' &&
    typeof m.ratio === 'number' &&
    typeof m.hidden === 'boolean' &&
    typeof m.pinned === 'boolean'
  )
}

/**
 * 补齐旧缓存缺失的 `ratioKnown`（结构演进兼容）。
 *
 * 旧版本行为等价于「倍率非 0 即已知」：那时 0 既可能是真 0 也可能是未知，
 * 无从分辨，只能按旧口径还原 —— 至少不会把原本正常显示的数字变掉。
 * 注意这里**不写 undefined**：显式落一个 boolean，避免下游反复判空。
 */
function withRatioKnown(m: ModelEntry): ModelEntry {
  if (typeof m.ratioKnown === 'boolean') return m
  return { ...m, ratioKnown: typeof m.ratio === 'number' && m.ratio > 0 }
}

/**
 * 读取缓存（同步）。
 *
 * 返回 null 表示「没有可用的缓存」—— 首次运行、缓存被清、结构不符都会走到这里，
 * 调用方按「无缓存」处理即可，不需要区分原因。
 */
export function readModelCache(): ModelCacheSnapshot | null {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { list, savedAt } = parsed as { list?: unknown; savedAt?: unknown }
    if (!Array.isArray(list) || list.length === 0) return null
    if (!list.every(isModelEntry)) return null
    const ts = typeof savedAt === 'number' && Number.isFinite(savedAt) ? savedAt : 0
    return {
      // 补齐旧缓存缺失的 ratioKnown（结构演进兼容，见函数注释）
      list: list.map(withRatioKnown),
      savedAt: ts,
      stale: ts > 0 && Date.now() - ts > MODEL_CACHE_TTL_MS,
    }
  } catch {
    // JSON 损坏 / localStorage 被禁用（隐私模式）：当作无缓存，不抛给页面
    return null
  }
}

/** 写入缓存（失败静默：缓存是加速手段，写不进去不该影响主流程） */
export function writeModelCache(list: ModelEntry[]): void {
  if (typeof localStorage === 'undefined') return
  if (list.length === 0) return // 空列表不覆盖已有缓存，避免一次异常响应把缓存清空
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ list, savedAt: Date.now() }))
  } catch {
    /* 配额满 / 被禁用：忽略 */
  }
}

/** 清空缓存（调试与「强制重新拉取」兜底用） */
export function clearModelCache(): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.removeItem(CACHE_KEY)
  } catch {
    /* 忽略 */
  }
}

/**
 * 由全量列表派生当前视图。
 *
 * 与后端 `/v1/admin/models?channel=` 的语义保持一致（先在服务端筛通道、
 * 再由前端剔除隐藏项），只是把两步都搬到本地 —— 这样缓存与网络结果
 * 走同一条过滤路径，不会出现「缓存显示的数量和刷新后不一样」。
 */
export function filterModelView(
  all: ModelEntry[],
  channel: ChannelFilter,
  showHidden: boolean
): ModelEntry[] {
  return all.filter(
    (m) => (channel === 'all' || m.channel === channel) && (showHidden || !m.hidden)
  )
}
